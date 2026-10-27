"""Journal and conference ranking tables from ShowJCR and PaperSeek, kept as files beside the database."""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
import json
import io
import os
import re
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree
import zipfile

import httpx

from paperseek_core.journal_metrics import PARSERS, PLATFORMS, JournalMetricsTable, table_source

MAX_TABLE_BYTES = 64 * 1024 * 1024


class JournalTableError(ValueError):
    pass


_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _texts(element: ElementTree.Element) -> str:
    return "".join(text.text or "" for text in element.iter(f"{_SHEET}t"))


def _xlsx_text(content: bytes) -> str:
    """The first worksheet of an .xlsx file as CSV text, so it is kept and parsed like a CSV table."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as book:
            names = book.namelist()
            sheets = sorted((name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
                            key=lambda name: int(re.sub(r"\D", "", name)))
            parts = [name for name in ("xl/sharedStrings.xml", *sheets[:1]) if name in names]
            if not sheets or sum(book.getinfo(name).file_size for name in parts) > MAX_TABLE_BYTES:
                raise JournalTableError("journal_table_size")
            shared = ([_texts(item) for item in ElementTree.fromstring(book.read(parts[0])).iter(f"{_SHEET}si")]
                      if parts[0] == "xl/sharedStrings.xml" else [])
            sheet = ElementTree.fromstring(book.read(sheets[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise JournalTableError("journal_table_encoding") from exc
    output = io.StringIO()
    writer = csv.writer(output)
    for row in sheet.iter(f"{_SHEET}row"):
        cells: dict[int, str] = {}
        for cell in row.iter(f"{_SHEET}c"):
            letters = re.match(r"[A-Z]+", cell.get("r") or "")
            column = (sum((ord(char) - 64) * 26 ** power for power, char in enumerate(reversed(letters.group(0)))) - 1
                      if letters else len(cells))
            value = cell.find(f"{_SHEET}v")
            if cell.get("t") == "inlineStr":
                cells[column] = _texts(cell)
            elif value is not None and value.text:
                shared_index = int(value.text) if cell.get("t") == "s" and value.text.isdigit() else -1
                cells[column] = shared[shared_index] if 0 <= shared_index < len(shared) else value.text
        writer.writerow([cells.get(index, "") for index in range(max(cells, default=-1) + 1)])
    return output.getvalue()


def _decode(content: bytes) -> str:
    if content.startswith(b"PK\x03\x04"):
        return _xlsx_text(content)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise JournalTableError("journal_table_encoding")


class JournalTableStore:
    _last_attempt: dict[Path, float] = {}

    def __init__(self, data_dir: Path | str) -> None:
        self.folder = Path(data_dir).expanduser().resolve() / "journal-metrics"
        self._lock = threading.Lock()
        self._cached: tuple[tuple[Any, ...], JournalMetricsTable] | None = None

    def _paths(self, platform: str) -> tuple[Path, Path]:
        if platform not in PLATFORMS:
            raise JournalTableError("journal_table_platform")
        return self.folder / f"{platform}.csv", self.folder / f"{platform}.json"

    def status(self) -> list[dict[str, Any]]:
        rows = []
        for platform in PLATFORMS:
            table, meta = self._paths(platform)
            info: dict[str, Any] = {}
            if table.exists() and meta.exists():
                try:
                    info = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    info = {}
            rows.append({"platform": platform, "imported": bool(info), "journals": int(info.get("journals") or 0),
                         "year": info.get("year"), "filename": str(info.get("filename") or ""),
                         "source": str(info.get("source") or "file"), "imported_at": info.get("imported_at")})
        return rows

    async def ensure(self, client: httpx.AsyncClient) -> JournalMetricsTable | None:
        """The loaded tables, downloading missing ones first; a failed download is retried after an hour."""
        missing = await asyncio.to_thread(self.missing)
        if missing and time.monotonic() - self._last_attempt.get(self.folder, -3600.0) >= 3600:
            self._last_attempt[self.folder] = time.monotonic()
            for platform in missing:
                try:
                    await self.download(client, [platform])
                except JournalTableError:
                    continue
        return await asyncio.to_thread(self.table)

    def missing(self) -> list[str]:
        return [platform for platform, row in zip(PLATFORMS, self.status(), strict=True) if not row["imported"]]

    async def download(self, client: httpx.AsyncClient, platforms: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch the current edition of each table; a table that fails keeps its previous copy."""
        saved = []
        for platform in platforms or list(PLATFORMS):
            url, name, year, source = table_source(platform)
            try:
                # Named as a source so the data-source proxy scope applies, as for PaperHub's GitHub files.
                response = await client.get(url, timeout=120, follow_redirects=True,
                                            extensions={"paperseek_source": "showjcr"})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise JournalTableError("journal_table_download") from exc
            saved.append(await asyncio.to_thread(self.import_table, platform, response.content, filename=name,
                                                 year=year, source=source))
        return saved

    def import_table(self, platform: str, content: bytes, *, filename: str = "", year: int | None = None,
                     source: str = "file") -> dict[str, Any]:
        table, meta = self._paths(platform)
        if not content or len(content) > MAX_TABLE_BYTES:
            raise JournalTableError("journal_table_size")
        text = _decode(content)
        try:
            entries = PARSERS[platform](text, year=year)
        except ValueError as exc:
            raise JournalTableError("journal_table_columns") from exc
        if not entries:
            raise JournalTableError("journal_table_empty")
        self.folder.mkdir(parents=True, exist_ok=True)
        named_year = re.search(r"(?:19|20)\d{2}", Path(filename).name)
        year = year or (int(named_year.group(0)) if named_year else None)
        info = {"journals": len(entries), "year": year or next((e["metrics"].get("year") for e in entries if e["metrics"].get("year")), None),
                "filename": Path(filename).name[:200], "source": source, "imported_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            for path, body in ((table, text.encode("utf-8")), (meta, json.dumps(info, ensure_ascii=False).encode("utf-8"))):
                temporary = path.with_name(f".{uuid4()}.tmp")
                temporary.write_bytes(body)
                os.replace(temporary, path)
            self._cached = None
        return {"platform": platform, "imported": True, **info}

    def table(self) -> JournalMetricsTable | None:
        """The imported tables as one lookup, reloaded only when a file changes."""
        present = []
        for platform in PLATFORMS:
            table, meta = self._paths(platform)
            if table.exists() and meta.exists():
                stat = table.stat()
                present.append((platform, stat.st_mtime_ns, stat.st_size))
        if not present:
            return None
        key = tuple(present)
        with self._lock:
            if self._cached and self._cached[0] == key:
                return self._cached[1]
            entries = {}
            for platform, *_ in present:
                path, meta = self._paths(platform)
                try:
                    year = json.loads(meta.read_text(encoding="utf-8")).get("year")
                    entries[platform] = PARSERS[platform](path.read_text(encoding="utf-8"), year=year)
                except (OSError, ValueError):
                    continue
            loaded = JournalMetricsTable(entries)
            self._cached = (key, loaded)
            return loaded
