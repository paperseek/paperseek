"""Conference acceptance (venue, year, Oral / Spotlight / Poster) from Paper Hub and OpenReview.

OpenReview decides status by ``content.venueid``, never by the free-text ``venue``: a
rejected ICLR paper's venue reads "Submitted to ICLR 2026". Only ids of the forms
``<org>/<YYYY>/Conference``, ``TMLR`` and NeurIPS's Datasets & Benchmarks track count
as accepted; workshops, rejected, withdrawn and under-review submissions do not.

Adapted from paper-search-pro (https://github.com/O0000-code/paper-search-pro), Apache-2.0.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

_CONFERENCE = re.compile(r"[^/]+(?:/[^/]+)*/(\d{4})/Conference")
_DATASETS_TRACK = re.compile(r"NeurIPS\.cc/(\d{4})/(?:Datasets_and_Benchmarks_Track|Track/Datasets_and_Benchmarks)")
_VENUE_LEAD = re.compile(r"\s*([A-Za-z][A-Za-z0-9&+\-]*)\s+(\d{4})\b")
# Whole lower-cased words: "ShortOralPoster" is one word and is not an Oral. ICML writes
# "spotlightposter" for a spotlight. A Findings paper is never shown as a main-track one.
_TIERS = {"findings": "Findings", "oral": "Oral", "spotlight": "Spotlight", "spotlightposter": "Spotlight",
          "poster": "Poster"}
TIER_ORDER = ("Findings", "Oral", "Spotlight", "Poster")


def title_key(title: Any) -> str:
    return re.sub(r"[\W_]+", " ", str(title or "").casefold()).strip()


def tier(text: Any) -> str:
    found = {_TIERS[word] for word in re.findall(r"[a-z]+", str(text or "").lower()) if word in _TIERS}
    return next((name for name in TIER_ORDER if name in found), "")


def _info(venue: str, year: int | None, presentation: str, *, track: str = "", source: str, url: str = "") -> dict[str, Any]:
    label = " ".join(part for part in (venue, str(year or ""), track, presentation) if part)
    return {"venue": venue, "year": year, "track": track, "tier": presentation, "label": label,
            "sources": [source], "url": url}


def from_paperhub(paper: dict[str, Any]) -> dict[str, Any]:
    venue = str(paper.get("conference") or "").strip()
    if not venue:
        return {}
    try:
        year = int(paper.get("year") or 0) or None
    except (TypeError, ValueError):
        year = None
    url = str(paper.get("url") or "")
    return _info(venue, year, tier(paper.get("presentation")), source="paperhub", url=url)


def content_value(content: dict[str, Any], key: str) -> Any:
    value = content.get(key)
    return value.get("value") if isinstance(value, dict) else value


def from_openreview(note: dict[str, Any]) -> dict[str, Any]:
    """Acceptance for an accepted OpenReview note, or {} for anything else."""
    content = note.get("content")
    if not isinstance(content, dict):
        return {}
    venue_id = content_value(content, "venueid")
    venue_text = str(content_value(content, "venue") or "")
    forum = str(note.get("forum") or note.get("id") or "")
    url = f"https://openreview.net/forum?id={forum}" if forum else ""
    if not isinstance(venue_id, str):
        return {}
    if venue_id == "TMLR":
        stamp = note.get("pdate") or note.get("cdate")
        year = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).year if isinstance(stamp, (int, float)) else None
        return _info("TMLR", year, "", source="openreview", url=url)
    if match := _DATASETS_TRACK.fullmatch(venue_id):
        return _info("NeurIPS", int(match.group(1)), tier(venue_text), track="Datasets & Benchmarks",
                     source="openreview", url=url)
    if match := _CONFERENCE.fullmatch(venue_id):
        year = int(match.group(1))
        lead = _VENUE_LEAD.match(venue_text)
        if lead and int(lead.group(2)) == year:
            name = lead.group(1)
        else:
            parts = venue_id.split("/")
            name = re.sub(r"\.[A-Za-z]{2,6}$", "", parts[-3] if len(parts) >= 3 else parts[0])
        return _info(name, year, tier(venue_text), source="openreview", url=url)
    return {}


def merge(*items: dict[str, Any]) -> dict[str, Any]:
    """One record for a paper both sources know; the first non-empty value wins, sources are united."""
    present = [item for item in items if item]
    if not present:
        return {}
    merged = dict(present[0])
    for item in present[1:]:
        for key in ("venue", "year", "track", "tier", "url"):
            if not merged.get(key) and item.get(key):
                merged[key] = item[key]
        merged["sources"] = list(dict.fromkeys([*merged.get("sources", []), *item.get("sources", [])]))
    merged["label"] = " ".join(part for part in (merged["venue"], str(merged.get("year") or ""), merged.get("track", ""),
                                                 merged.get("tier", "")) if part)
    return merged


ACCEPTANCE_FILTERS = {"accepted": TIER_ORDER + ("",), "oral": ("Oral",), "spotlight": ("Oral", "Spotlight")}


def matches_filter(acceptance: dict[str, Any], name: str) -> bool:
    return bool(acceptance.get("venue")) and str(acceptance.get("tier") or "") in ACCEPTANCE_FILTERS[name]


__all__ = ["ACCEPTANCE_FILTERS", "TIER_ORDER", "content_value", "from_openreview", "from_paperhub", "matches_filter", "merge",
           "tier", "title_key"]
