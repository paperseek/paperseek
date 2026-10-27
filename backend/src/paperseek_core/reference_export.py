"""BibTeX and RIS files that reference managers (Zotero, EndNote, JabRef) import directly."""

from __future__ import annotations

import re
import unicodedata

from paperseek_core.models import FinalPaperResult

_BIBTEX_ESCAPES = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&",
                   "_": r"\_", "#": r"\#", "$": r"\$", "~": r"\~{}", "^": r"\^{}"}
# Lower-cased source types mapped to (BibTeX entry, RIS type).
_KINDS = {
    "article": ("article", "JOUR"), "journal-article": ("article", "JOUR"), "journal article": ("article", "JOUR"),
    "review": ("article", "JOUR"), "letter": ("article", "JOUR"), "editorial": ("article", "JOUR"),
    "conference-paper": ("inproceedings", "CPAPER"), "proceedings-article": ("inproceedings", "CPAPER"),
    "proceedings paper": ("inproceedings", "CPAPER"), "conference": ("inproceedings", "CPAPER"),
    "inproceedings": ("inproceedings", "CPAPER"),
    "book": ("book", "BOOK"), "monograph": ("book", "BOOK"),
    "book-chapter": ("incollection", "CHAP"), "chapter": ("incollection", "CHAP"),
    "dissertation": ("phdthesis", "THES"), "thesis": ("phdthesis", "THES"),
    "report": ("techreport", "RPRT"), "preprint": ("misc", "UNPB"), "posted-content": ("misc", "UNPB"),
}


def _kind(item: FinalPaperResult) -> tuple[str, str]:
    for value in [*item.document_types, item.publication_type]:
        key = value.strip().casefold()
        if key in _KINDS:
            return _KINDS[key]
        if "journal" in key:
            return _KINDS["article"]
        if "conference" in key or "proceeding" in key:
            return _KINDS["conference-paper"]
    if item.provider == "arxiv" or item.arxiv_id:
        return _KINDS["preprint"]
    return ("article", "JOUR") if item.venue else ("misc", "GEN")


def _keywords(item: FinalPaperResult) -> list[str]:
    return [value.strip() for value in re.split(r";", item.keywords_text or item.keywords) if value.strip()]


def _note(item: FinalPaperResult) -> str:
    return str(item.acceptance.get("label") or "")


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()


def _citation_key(item: FinalPaperResult, used: set[str]) -> str:
    author = item.authors[0] if item.authors else ""
    family = author.split(",")[0] if "," in author else (author.split() or [""])[-1]
    title_word = next((word for word in re.findall(r"[A-Za-z0-9]+", _ascii(item.title))
                       if word.casefold() not in {"a", "an", "the", "on", "of", "for", "and", "in", "to"}), "")
    base = re.sub(r"[^A-Za-z0-9]", "", _ascii(family)).lower() + str(item.year or item.publish_year or "")
    base += title_word.lower()
    if not base or base.isdigit():
        base = f"paperseek{item.rank}{base}"
    key, suffix = base, 0
    while key in used:
        suffix += 1
        key = base + "abcdefghijklmnopqrstuvwxyz"[(suffix - 1) % 26] * (1 + (suffix - 1) // 26)
    used.add(key)
    return key


def _bib(value: str) -> str:
    return "".join(_BIBTEX_ESCAPES.get(char, char) for char in re.sub(r"\s+", " ", value).strip())


_VERBATIM = frozenset({"doi", "url", "eprint"})


def _verbatim(value: str) -> str:
    return re.sub(r"[{}\s]", "", value)


def to_bibtex(items: list[FinalPaperResult]) -> str:
    used: set[str] = set()
    entries = []
    for item in items:
        entry, _ = _kind(item)
        venue_field = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle",
                       "phdthesis": "school", "techreport": "institution"}.get(entry, "howpublished")
        arxiv = item.arxiv_id
        fields = {
            "title": item.title, "author": " and ".join(item.authors), "year": str(item.year or item.publish_year or ""),
            venue_field: item.venue, "volume": item.volume, "number": item.issue, "pages": item.pages.replace("-", "--"),
            "issn": item.issn, "doi": item.doi, "url": item.url, "eprint": arxiv,
            "archiveprefix": "arXiv" if arxiv else "", "abstract": item.abstract,
            "keywords": ", ".join(_keywords(item)), "note": _note(item),
        }
        # DOI, URL and eprint are verbatim in BibTeX and biblatex; escaping would change them.
        body = ",\n".join(f"  {key} = {{{_verbatim(value) if key in _VERBATIM else _bib(value)}}}"
                          for key, value in fields.items() if value and value.strip())
        entries.append(f"@{entry}{{{_citation_key(item, used)},\n{body}\n}}")
    return "\n\n".join(entries) + "\n" if entries else ""


def to_ris(items: list[FinalPaperResult]) -> str:
    records = []
    for item in items:
        _, kind = _kind(item)
        lines = [("TY", kind), ("TI", item.title), *(("AU", author) for author in item.authors)]
        start, _, end = item.pages.partition("-")
        lines += [
            ("PY", str(item.year or item.publish_year or "")), ("T2", item.venue), ("VL", item.volume),
            ("IS", item.issue), ("SP", start), ("EP", end), ("SN", item.issn), ("DO", item.doi), ("UR", item.url),
            ("L1", item.pdf_url), ("AB", item.abstract), *(("KW", keyword) for keyword in _keywords(item)),
            ("N1", _note(item)),
        ]
        records.append("\r\n".join(f"{tag}  - {re.sub(r'\s+', ' ', value).strip()}"
                                   for tag, value in lines if value and value.strip()) + "\r\nER  - \r\n")
    return "\r\n".join(records)


__all__ = ["to_bibtex", "to_ris"]
