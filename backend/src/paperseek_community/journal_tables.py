"""Journal and conference ranking tables from ShowJCR, kept as files beside the database."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

import httpx

from paperseek_core.journal_metrics import PARSERS, PLATFORMS, SHOWJCR_FILES, JournalMetricsTable, showjcr_url

MAX_TABLE_BYTES = 64 * 1024 * 1024


class JournalTableError(ValueError):
    pass


def _decode(content: bytes) -> str:
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
        """Fetch the current ShowJCR edition of each table; a table that fails keeps its previous copy."""
        saved = []
        for platform in platforms or list(PLATFORMS):
            name, year = SHOWJCR_FILES[platform]
            try:
                # Named as a source so the data-source proxy scope applies, as for PaperHub's GitHub files.
                response = await client.get(showjcr_url(platform), timeout=120, follow_redirects=True,
                                            extensions={"paperseek_source": "showjcr"})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise JournalTableError("journal_table_download") from exc
            saved.append(await asyncio.to_thread(self.import_table, platform, response.content, filename=name,
                                                 year=year, source="showjcr"))
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
