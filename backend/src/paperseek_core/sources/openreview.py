"""Anonymous OpenReview search for accepted papers, used to widen Paper Hub and label acceptance.

Adapted from paper-search-pro (https://github.com/O0000-code/paper-search-pro), Apache-2.0.
"""

from __future__ import annotations

import re
from typing import Any

from paperseek_core.acceptance import content_value, from_openreview, title_key
from paperseek_core.sources.contracts import SourceError
from paperseek_core.sources.http import AsyncHttpTransport

_BOOLEAN = {"AND", "OR", "NOT"}


def plain_terms(query: str) -> str:
    """OpenReview's term search does not parse Boolean syntax; quotes and brackets make it drift off topic."""
    words: list[str] = []
    for phrase, bare in re.findall(r'"([^"]*)"|([^\s"()]+)', query or ""):
        if bare in _BOOLEAN or bare.startswith("-") or re.match(r"^(conference|conf|venue|year|after|before):", bare, re.I):
            continue
        for word in (phrase.split() if phrase else [bare.lstrip("+")]):
            if word and word.casefold() not in {item.casefold() for item in words}:
                words.append(word)
    return " ".join(words)


class OpenReviewClient:
    """Anonymous OpenReview search (API v2). Only accepted papers are returned."""

    name = "openreview"
    SEARCH_URL = "https://api2.openreview.net/notes/search"
    PAGE_SIZE = 25

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self.transport = transport

    async def _page(self, term: str, offset: int) -> list[dict[str, Any]]:
        params = {"term": term, "type": "terms", "content": "all", "group": "all", "source": "forum",
                  "limit": self.PAGE_SIZE, "offset": offset}
        response, _ = await self.transport.request(
            self.name, "GET", self.SEARCH_URL, params=params, headers={"Accept": "application/json"},
            timeout=20, query=term, attempts=2,
        )
        if response.status_code >= 400:
            raise SourceError(self.name, f"OpenReview search returned HTTP {response.status_code}.",
                              status=response.status_code, body=response.text[:500], query=term)
        try:
            notes = response.json().get("notes")
        except (ValueError, AttributeError) as exc:
            raise SourceError(self.name, "OpenReview search returned an unexpected body.", query=term) from exc
        if not isinstance(notes, list):
            raise SourceError(self.name, "OpenReview search returned no note list.", query=term)
        return [note for note in notes if isinstance(note, dict)]

    async def search(self, query: str, *, pages: int = 2) -> list[dict[str, Any]]:
        """Accepted papers as Paper Hub shaped rows, in OpenReview's relevance order."""
        term = plain_terms(query)
        if not term:
            return []
        rows: dict[str, dict[str, Any]] = {}
        for page in range(pages):
            notes = await self._page(term, page * self.PAGE_SIZE)
            for note in notes:
                if (row := self.paper(note)) and (key := title_key(row["title"])) not in rows:
                    rows[key] = row
            if len(notes) < self.PAGE_SIZE:
                break
        return list(rows.values())

    async def find(self, title: str) -> dict[str, Any]:
        """Acceptance of the accepted paper with exactly this title, or {}."""
        key = title_key(title)
        if not key:
            return {}
        for note in await self._page(" ".join(key.split()[:24]), 0):
            content = note.get("content") if isinstance(note.get("content"), dict) else {}
            if title_key(content_value(content, "title")) == key and (acceptance := from_openreview(note)):
                return acceptance
        return {}

    @staticmethod
    def paper(note: dict[str, Any]) -> dict[str, Any]:
        acceptance = from_openreview(note)
        content = note.get("content") if isinstance(note.get("content"), dict) else {}
        title = str(content_value(content, "title") or "").strip()
        if not acceptance or not title:
            return {}
        authors = content_value(content, "authors") or []
        keywords = content_value(content, "keywords") or []
        forum = str(note.get("forum") or note.get("id") or "")
        return {
            "id": f"openreview:{forum}", "title": title, "abstract": str(content_value(content, "abstract") or "").strip(),
            "authors": [str(name.get("fullname") if isinstance(name, dict) else name) for name in authors if name],
            "keywords": [str(word) for word in keywords if isinstance(word, str)],
            "conference": acceptance["venue"], "year": acceptance["year"], "url": acceptance["url"],
            "presentation": acceptance["tier"].lower(), "pdf": f"https://openreview.net/pdf?id={forum}" if content_value(content, "pdf") else "",
            "acceptance": acceptance,
        }


__all__ = ["OpenReviewClient", "plain_terms"]
