"""Journal and conference rankings from ShowJCR, joined to papers by ISSN, then by venue name.

The tables come from ShowJCR (https://github.com/hitfyd/ShowJCR), downloaded into the
workspace at run time; none ship with PaperSeek. Columns are found by name, not position,
because yearly releases add and reorder columns:

- ``cas``: 中科院分区表. ``Journal``, ``ISSN/EISSN`` (``2053-1583/2053-1583``), ``大类``,
  ``大类分区`` (``3 [168/495]``), ``Top`` (是/否).
- ``jcr``: Journal Citation Reports. ``Journal``, ``ISSN``, ``EISSN``, ``IF(year)``, and one
  ``Category_n`` / ``IF Quartile(year)_n`` / ``IF Rank(year)_n`` group per category.
- ``ccf``: CCF 推荐国际学术会议和期刊目录. ``刊物名称`` (abbreviation), ``Journal`` (full
  name), ``领域``, ``CCF推荐类别`` (刊物/会议), ``CCF推荐类型`` (A类/B类/C类). No ISSN.

``cn_core`` (北大核心 and CSSCI) is PaperSeek's own sheet, not ShowJCR's: 刊名, 收录
(``CSSCI来源期刊（2025-2026）；北大中文核心（2023版）``) and 曾用名, by position without a header
row, or by those names when one is present. It has no ISSN, so it is matched by Chinese title only.

A CAS 区, a JCR quartile and a CCF class are rankings; only the JCR IF is an Impact Factor.

Adapted from paper-search-pro (https://github.com/O0000-code/paper-search-pro), Apache-2.0.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any, Iterable, Literal

PLATFORMS = ("cas", "jcr", "ccf", "cn_core")
Platform = Literal["cas", "jcr", "ccf", "cn_core"]

_SHOWJCR = ("https://raw.githubusercontent.com/hitfyd/ShowJCR/master/"
            "%E4%B8%AD%E7%A7%91%E9%99%A2%E5%88%86%E5%8C%BA%E8%A1%A8%E5%8F%8AJCR%E5%8E%9F%E5%A7%8B%E6%95%B0%E6%8D%AE%E6%96%87%E4%BB%B6/")
# platform -> (file name, edition). Newer editions only need these names changed.
SHOWJCR_FILES: dict[str, tuple[str, int]] = {
    "cas": ("FQBJCR2025-UTF8.csv", 2025), "jcr": ("JCR2025-UTF8.csv", 2025), "ccf": ("CCF2026-UTF8.csv", 2026),
}


CN_CORE_URL = "https://xhsjson-1311978040.cos.ap-beijing.myqcloud.com/cn_core.xlsx"


def showjcr_url(platform: str) -> str:
    return _SHOWJCR + SHOWJCR_FILES[platform][0]


def table_source(platform: str) -> tuple[str, str, int | None, str]:
    """(url, file name, edition, source) a table downloads from."""
    if platform == "cn_core":
        return CN_CORE_URL, "cn_core.xlsx", None, "paperseek"
    name, year = SHOWJCR_FILES[platform]
    return showjcr_url(platform), name, year, "showjcr"


# Result filters: name -> (platform, field, best allowed value). A field in JOURNAL_FLAGS must be true.
JOURNAL_FILTERS: dict[str, tuple[str, str, int]] = {
    "cas1": ("cas", "tier", 1), "cas2": ("cas", "tier", 2), "cas3": ("cas", "tier", 3),
    "castop": ("cas", "top", 1),
    "jcr1": ("jcr", "quartile_number", 1), "jcr2": ("jcr", "quartile_number", 2),
    "ccfa": ("ccf", "rank_number", 1), "ccfb": ("ccf", "rank_number", 2), "ccfc": ("ccf", "rank_number", 3),
    "pku": ("cn_core", "pku", 1), "cssci": ("cn_core", "cssci", 1),
    "cssci_c": ("cn_core", "cssci_collection", 1), "cssci_e": ("cn_core", "cssci_extended", 1),
    "ranked": ("", "", 0),
}
JOURNAL_FLAGS = frozenset({"top", "pku", "cssci", "cssci_collection", "cssci_extended"})

_ISSN = re.compile(r"[^0-9Xx]")
_CAS_PARTITION = re.compile(r"^\s*([1-4])\s*(?:区)?\s*(?:\[\s*([^\]]+)\])?")
_QUARTILE = re.compile(r"Q([1-4])", re.I)
_CCF_RANK = re.compile(r"([ABC])\s*类?", re.I)
_YEAR = re.compile(r"(?:19|20)\d{2}")
_EDITION = re.compile(r"(?:19|20)\d{2}(?:\s*[-–—~至]\s*(?:19|20)\d{2})?")
# Where a Chinese title's own name ends and its edition (学报.自然科学版, 学报（社会科学版）) begins.
_CN_TITLE_PART = re.compile(r"[（(.．:：·]")
# Words that differ between how a source and the CCF list name the same conference.
_VENUE_FILLER = frozenset({"proceedings", "proc", "of", "the", "advances", "in", "annual", "international", "intl",
                           "conference", "conf", "on", "ieee", "acm", "cvf", "and", "joint", "meeting", "symposium"})


def normalize_issn(value: Any) -> str:
    cleaned = _ISSN.sub("", str(value or "")).upper()
    return f"{cleaned[:4]}-{cleaned[4:]}" if len(cleaned) == 8 else ""


def normalize_issns(*values: Any) -> list[str]:
    found = (normalize_issn(part) for value in values for part in re.split(r"[,/;\s]+", str(value or "")))
    return list(dict.fromkeys(issn for issn in found if issn))


def _ascii_words(text: Any) -> list[str]:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().casefold()
    return re.findall(r"[a-z0-9]+", text.replace("&", " and "))


def journal_key(title: Any) -> str:
    """Match key for journal titles: case, accents, punctuation, '&' and a leading 'The' do not count."""
    words = _ascii_words(re.sub(r"\s*[(\[].*$", "", str(title or "")))
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(words)


def cn_title_key(title: Any) -> str:
    """Match key for Chinese journal titles: width, punctuation and a "(中英文)" marker do not count."""
    text = unicodedata.normalize("NFKC", str(title or "")).casefold().replace("中英文", "")
    key = re.sub(r"[\W_]+", "", text)
    return key if re.search(r"[\u4e00-\u9fff]", key) else ""


def venue_core(name: Any) -> str:
    """Conference name without years, ordinals and filler words, for containment matching."""
    return " ".join(word for word in _ascii_words(name)
                    if word not in _VENUE_FILLER and not re.fullmatch(r"\d+(st|nd|rd|th)?", word))


def _columns(header: list[str]) -> dict[str, int]:
    return {name.strip().lstrip("﻿").casefold(): index for index, name in enumerate(header)}


def _find(columns: dict[str, int], *names: str, prefix: bool = False) -> int | None:
    for name in names:
        if name in columns:
            return columns[name]
    if prefix:
        for name in names:
            for column, index in columns.items():
                if column.startswith(name):
                    return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    return row[index].strip() if index is not None and index < len(row) else ""


def _float(value: str) -> float | None:
    text = value.strip().replace(",", "")
    try:
        return float(text) if text and text.upper() not in {"N/A", "NA", "-"} else None
    except ValueError:
        return None


def _year(header: list[str], fallback: int | None) -> int | None:
    years = [int(match.group(0)) for name in header if (match := _YEAR.search(name))]
    return max(years) if years else fallback


def _rows(text: str) -> tuple[list[str], Iterable[list[str]]]:
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    return next(reader, []), reader


def parse_cas(text: str, *, year: int | None = None) -> list[dict[str, Any]]:
    header, rows = _rows(text)
    columns = _columns(header)
    issn = _find(columns, "issn/eissn", "issn", prefix=True)
    if issn is None:
        raise ValueError("The CAS table has no ISSN/EISSN column.")
    title, category = _find(columns, "journal", "期刊名称", "刊名"), _find(columns, "大类")
    partition, top = _find(columns, "大类分区"), _find(columns, "top")
    year_column = _find(columns, "年份", "year")
    entries = []
    for row in rows:
        match = _CAS_PARTITION.match(_cell(row, partition))
        issns = normalize_issns(_cell(row, issn))
        if not match or not (issns or _cell(row, title)):
            continue
        row_year = _cell(row, year_column)
        entries.append({
            "title": _cell(row, title), "issns": issns,
            "metrics": {"tier": int(match.group(1)), "rank": (match.group(2) or "").strip(),
                        "top": _cell(row, top).casefold() in {"是", "y", "yes", "true", "1", "top"},
                        "category": _cell(row, category),
                        "year": int(row_year) if row_year.isdigit() else year},
        })
    return entries


def parse_jcr(text: str, *, year: int | None = None) -> list[dict[str, Any]]:
    header, rows = _rows(text)
    columns = _columns(header)
    issn, eissn = _find(columns, "issn"), _find(columns, "eissn", "e-issn")
    if issn is None and eissn is None:
        raise ValueError("The JCR table has no ISSN or eISSN column.")
    title = _find(columns, "journal", "journal name", "full journal title")
    impact = next((index for name, index in columns.items() if re.match(r"(if|jif|journal impact factor|impact factor)(\b|\(|$)", name)
                   and not re.search(r"quartile|rank|without|5[- ]?year", name)), None)
    # One column per category, numbered ("IF Quartile(2025)_1") or single ("IF Quartile(2024)").
    groups = []
    for name, index in columns.items():
        if re.match(r"(if |jif )?quartile", name) or name.startswith(("if quartile", "jif quartile")):
            suffix = re.search(r"_(\d+)$", name)
            number = suffix.group(1) if suffix else ""
            category = next((i for n, i in columns.items() if n.startswith("category") and (n.endswith(f"_{number}") if number else True)), None)
            rank = next((i for n, i in columns.items() if re.match(r"(if |jif )?rank", n) and (n.endswith(f"_{number}") if number else True)), None)
            groups.append((index, category, rank))
    table_year = _year(header, year)
    entries = []
    for row in rows:
        issns = normalize_issns(_cell(row, issn), _cell(row, eissn))
        categories = [(_cell(row, q).upper(), _cell(row, c), _cell(row, r)) for q, c, r in groups if _QUARTILE.fullmatch(_cell(row, q))]
        factor = _float(_cell(row, impact))
        if not (issns or _cell(row, title)) or not (categories or factor is not None):
            continue
        best = min(categories, default=("", "", ""))
        entries.append({
            "title": _cell(row, title), "issns": issns,
            "metrics": {"quartile": best[0], "quartile_number": int(best[0][1]) if best[0] else None,
                        "impact_factor": factor, "rank": best[2],
                        "category": "; ".join(dict.fromkeys(re.sub(r"\(\w+\)$", "", c).strip() for _, c, _ in categories if c)),
                        "year": table_year},
        })
    return entries


def parse_ccf(text: str, *, year: int | None = None) -> list[dict[str, Any]]:
    header, rows = _rows(text)
    columns = _columns(header)
    abbreviation, name = _find(columns, "刊物名称", "简称"), _find(columns, "journal", "全称")
    rank = _find(columns, "ccf推荐类型", prefix=True)
    kind, field, year_column = _find(columns, "ccf推荐类别", prefix=True), _find(columns, "领域"), _find(columns, "年份")
    if rank is None or name is None:
        raise ValueError("The CCF table has no Journal or CCF推荐类型 column.")
    entries = []
    for row in rows:
        match = _CCF_RANK.match(_cell(row, rank))
        if not match or not (_cell(row, name) or _cell(row, abbreviation)):
            continue
        row_year = _cell(row, year_column)
        entries.append({
            "title": _cell(row, name), "abbreviation": _cell(row, abbreviation), "issns": [],
            "metrics": {"rank": match.group(1).upper(), "rank_number": "ABC".index(match.group(1).upper()) + 1,
                        "type": "conference" if "会议" in _cell(row, kind) else "journal",
                        "abbreviation": _cell(row, abbreviation), "field": _cell(row, field),
                        "year": int(row_year) if row_year.isdigit() else year},
        })
    return entries


def cn_core_listing(text: str) -> dict[str, Any]:
    """北大核心 and CSSCI from a 收录 cell. CSSCI 来源期刊 (``cssci``), 来源集刊 and 扩展版 are separate lists."""
    metrics: dict[str, Any] = {"pku": False, "cssci": False, "cssci_collection": False, "cssci_extended": False}
    for item in re.split(r"[；;，,、\n]+", unicodedata.normalize("NFKC", text or "")):
        match = _EDITION.search(item)
        edition = re.sub(r"\s*[-–—~至]\s*", "-", match.group(0)) if match else ""
        if "CSSCI" in item.upper() or "南大核心" in item:
            field = "cssci_extended" if "扩展" in item else "cssci_collection" if "集刊" in item else "cssci"
        elif "北大" in item or "中文核心" in item:
            field = "pku"
        else:
            continue
        metrics[field], metrics[f"{field}_edition"] = True, edition
    return metrics


def parse_cn_core(text: str, *, year: int | None = None) -> list[dict[str, Any]]:
    header, rows = _rows(text)
    columns = _columns(header)
    title = _find(columns, "刊名", "期刊名称", "期刊", "journal")
    if title is None:
        title, listing, former, rows = 0, 1, 2, [header, *rows]
    else:
        listing, former = _find(columns, "收录", "收录情况", "核心", prefix=True), _find(columns, "曾用名", prefix=True)
    entries = []
    for row in rows:
        metrics = cn_core_listing(_cell(row, listing))
        if _cell(row, title) and any(metrics.values()):
            entries.append({"title": _cell(row, title), "issns": [],
                            "metrics": {**metrics, "former_title": _cell(row, former), "year": year}})
    return entries


PARSERS = {"cas": parse_cas, "jcr": parse_jcr, "ccf": parse_ccf, "cn_core": parse_cn_core}


class JournalMetricsTable:
    """One lookup over every loaded table. ISSN wins; an exact venue name is the fallback."""

    def __init__(self, entries_by_platform: dict[str, list[dict[str, Any]]]) -> None:
        self.by_issn: dict[str, dict[str, Any]] = {}
        self.by_title: dict[str, dict[str, Any]] = {}
        self.platforms = [platform for platform in PLATFORMS if entries_by_platform.get(platform)]
        for platform in ("cas", "jcr"):
            for entry in entries_by_platform.get(platform) or []:
                journal = next((self.by_issn[issn] for issn in entry["issns"] if issn in self.by_issn), None)
                key = journal_key(entry["title"])
                if journal is None and not entry["issns"] and key:
                    journal = self.by_title.get(key)
                if journal is None:
                    journal = {"journal": entry["title"], "issns": []}
                journal.setdefault(platform, entry["metrics"])
                for issn in entry["issns"]:
                    if issn not in journal["issns"]:
                        journal["issns"].append(issn)
                    self.by_issn.setdefault(issn, journal)
                if key:
                    self.by_title.setdefault(key, journal)
        self.ccf_by_name: dict[str, dict[str, Any]] = {}
        self.ccf_by_core: dict[str, dict[str, Any]] = {}
        self.ccf_by_abbreviation: dict[str, dict[str, Any]] = {}
        for entry in entries_by_platform.get("ccf") or []:
            ccf = entry["metrics"]
            if name := journal_key(entry["title"]):
                self.ccf_by_name.setdefault(name, ccf)
            if ccf["type"] == "conference" and len(core := venue_core(entry["title"]).split()) >= 2:
                self.ccf_by_core.setdefault(" ".join(core), ccf)
            # Only distinctive abbreviations: "FAST" or "CC" written in a title is not the conference.
            abbreviation = entry["abbreviation"]
            if len(abbreviation) >= 3 and sum(char.isupper() for char in abbreviation) >= 2:
                self.ccf_by_abbreviation.setdefault(abbreviation.casefold(), ccf)
        # Current and former titles, then the name before an edition or subtitle (财经论丛 for
        # 财经论丛（浙江财经大学学报）) when only one journal has it: 北京大学学报 alone is three journals.
        self.cn_core_by_title: dict[str, dict[str, Any]] = {}
        cn_core = entries_by_platform.get("cn_core") or []
        for entry in cn_core:
            for name in (entry["title"], entry["metrics"]["former_title"]):
                if key := cn_title_key(name):
                    self.cn_core_by_title.setdefault(key, entry["metrics"])
        stems: dict[str, list[dict[str, Any]]] = {}
        for entry in cn_core:
            if (stem := cn_title_key(_CN_TITLE_PART.split(entry["title"])[0])) and stem != cn_title_key(entry["title"]):
                stems.setdefault(stem, []).append(entry["metrics"])
        for stem, found in stems.items():
            if len(found) == 1:
                self.cn_core_by_title.setdefault(stem, found[0])

    def __bool__(self) -> bool:
        return bool(self.platforms)

    def ccf(self, venue: str, abbreviations: Iterable[str] = ()) -> dict[str, Any]:
        """The CCF class of a venue: full name, then a conference's name inside the venue, then its abbreviation."""
        if not venue and not abbreviations:
            return {}
        if (key := journal_key(venue)) and (found := self.ccf_by_name.get(key)):
            return found
        if "workshop" not in venue.casefold():
            core = f" {venue_core(venue)} "
            if found := next((ccf for name, ccf in self.ccf_by_core.items()
                              if f" {name} " in core and (len(name.split()) >= 3 or core.strip() == name)), None):
                return found
            # In a venue name the abbreviation must match exactly ("Acta" is not ACTA); a known
            # conference name such as Paper Hub's "NEURIPS" may differ in case.
            for token in re.findall(r"[A-Za-z][A-Za-z0-9&+-]*", venue):
                if (found := self.ccf_by_abbreviation.get(token.casefold())) and found["abbreviation"] == token:
                    return found
            for token in abbreviations:
                if found := self.ccf_by_abbreviation.get(token.casefold()):
                    return found
        return {}

    def lookup(self, issns: Iterable[Any], venue: str, abbreviations: Iterable[str] = ()) -> dict[str, Any]:
        """Rankings for a paper, or {} when no table knows its venue."""
        journal, matched_by = None, ""
        for issn in normalize_issns(*issns):
            if issn in self.by_issn:
                journal, matched_by = self.by_issn[issn], "issn"
                break
        if journal is None and (key := journal_key(venue)) and key in self.by_title:
            journal, matched_by = self.by_title[key], "title"
        labels: dict[str, Any] = {}
        if journal is not None:
            labels = {"journal": journal["journal"], "matched_by": matched_by,
                      **{platform: dict(journal[platform]) for platform in ("cas", "jcr") if platform in journal}}
        if ccf := self.ccf(venue, abbreviations):
            labels = {"journal": venue, "matched_by": "title", **labels, "ccf": dict(ccf)}
        if (key := cn_title_key(venue)) and (cn_core := self.cn_core_by_title.get(key)):
            labels = {"journal": venue, "matched_by": "title", **labels, "cn_core": dict(cn_core)}
        return labels


def matches_filter(metrics: dict[str, Any], name: str) -> bool:
    platform, field, best = JOURNAL_FILTERS[name]
    if not platform:
        return any(platform in metrics for platform in PLATFORMS)
    value = (metrics.get(platform) or {}).get(field)
    if field in JOURNAL_FLAGS:
        return value is True
    return isinstance(value, int) and value <= best


__all__ = ["CN_CORE_URL", "JOURNAL_FILTERS", "JOURNAL_FLAGS", "PARSERS", "PLATFORMS", "SHOWJCR_FILES",
           "JournalMetricsTable", "cn_core_listing", "cn_title_key", "journal_key", "matches_filter", "normalize_issn",
           "normalize_issns", "parse_ccf", "parse_cas", "parse_cn_core", "parse_jcr", "showjcr_url", "table_source",
           "venue_core"]
