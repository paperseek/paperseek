"""Chinese Medical Journals full-text database (中华医学期刊全文数据库, yiigle.com): titles and abstracts only.

POST https://www.yiigle.com/apiVue/search/searchList with JSON; no key. ``isAggregations`` must be
"N", otherwise only facets come back. The endpoint has no field codes or Boolean operators: it
ANDs the words of ``queryString`` across title, abstract and keywords, and treats "OR" as one more
word. So a Boolean query keeps only the first term of each group, and NOT terms are dropped.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from paperseek_core.models import (
    PaperAuthor,
    PaperCitation,
    PaperIdentifiers,
    PaperKeywords,
    PaperLinks,
    PaperNames,
    PaperRecord,
    PaperSource,
)
from paperseek_core.sources.contracts import (
    SourceCapabilities,
    SourceError,
    SourceSearchMetadata,
    SourceSearchRequest,
    SourceSearchResult,
)
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi

_TOKEN = re.compile(r'"([^"]*)"|“([^”]*)”|(\()|(\))|([^\s()"“”]+)')


def yiigle_terms(query: str) -> str:
    """The words Yiigle should AND: the first alternative of each group, without excluded terms."""
    terms: list[str] = []
    depth, group_taken, negate = 0, False, False
    for match in _TOKEN.finditer(query or ""):
        phrase = match.group(1) if match.group(1) is not None else match.group(2)
        word = match.group(5)
        if match.group(3):
            depth += 1
            group_taken = False
            continue
        if match.group(4):
            depth = max(0, depth - 1)
            negate = negate and depth > 0
            continue
        if phrase is None and word and word.upper() in {"AND", "OR", "NOT"}:
            negate = word.upper() == "NOT" or (negate and word.upper() == "OR")
            continue
        term = (phrase if phrase is not None else word or "").strip()
        if not term or negate or (depth and group_taken):
            if depth == 0:
                negate = False
            continue
        terms.append(term)
        group_taken = depth > 0
    return " ".join(dict.fromkeys(terms))


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [part for part in re.split(r"[;；,，]", value)]
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


class YiigleSource:
    name = "yiigle"
    BASE_URL = "https://www.yiigle.com/apiVue/search/searchList"

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self.transport = transport
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance",),
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "citations", "keywords"),
            supports_abstracts=True,
            supports_citations=True,
            api_key="not_required",
            max_page_size=50,
            max_lane_limit=500,
            backoff_policy="3 attempts for transport, 429, and transient 5xx responses",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        terms = yiigle_terms(query)
        if not terms:
            raise SourceError(self.name, "Yiigle search query is empty.")
        page_size = max(1, min(int(request.limit or 20), 50))
        body = {"searchType": "pt", "page": max(1, int(request.page or 1)), "pageSize": page_size,
                "queryString": terms, "searchText": terms, "isAggregations": "N"}
        response, self.last_response_info = await self.transport.request(
            self.name, "POST", self.BASE_URL, json=body, timeout=45, query=query,
            headers={"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        if response.status_code >= 400:
            raise SourceError(self.name, f"Yiigle returned HTTP {response.status_code}.", status=response.status_code,
                              retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "Yiigle returned a non-JSON response.", body=response.text[:1000], query=query) from exc
        result = ((payload.get("data") or {}).get("result") if isinstance(payload, dict) else None) or {}
        if not isinstance(result, dict) or not isinstance(result.get("infos") or [], list):
            raise SourceError(self.name, "Yiigle returned an invalid search payload.", body=str(payload)[:1000], query=query)
        hits = [record for row in result.get("infos") or [] if isinstance(row, dict) and (record := self._to_record(row))]
        feedback = [f"Yiigle searched for: {terms}"] if terms != query else []
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=int(result.get("searchTotal") or 0), page=max(1, request.page), limit=page_size),
            hits=hits, query_feedback=feedback,
        )

    @staticmethod
    def _to_record(row: dict[str, Any]) -> PaperRecord | None:
        identity, title = str(row.get("artId") or row.get("id") or "").strip(), _text(row.get("artTitle"))
        if not identity or not title:
            return None
        year_text = str(row.get("artPubYear") or row.get("artPubDate") or "")
        year = int(year_text[:4]) if year_text[:4].isdigit() else None
        start, end = str(row.get("startPage") or "").strip(), str(row.get("endPage") or "").strip()
        try:
            cited = int(row["citationNum"]) if row.get("citationNum") not in (None, "") else None
        except (TypeError, ValueError):
            cited = None
        url = f"https://rs.yiigle.com/cmaid/{identity}"
        authors = _list(row.get("authorNames")) or [name.split("|")[0] for name in _list(row.get("authors"))]
        issn = str(row.get("issn") or "").strip()
        return PaperRecord(
            uid=f"yiigle:{identity}",
            title=title,
            types=["journal-article"],
            source_types=[str(row.get("docType") or "")] if row.get("docType") else [],
            source=PaperSource(source_title=_text(row.get("journalCn")), publish_year=year, volume=str(row.get("vol") or ""),
                               issue=str(row.get("issue") or ""), pages=f"{start}-{end}" if start and end and start != end else start or None),
            names=PaperNames(authors=[PaperAuthor(display_name=name) for name in dict.fromkeys(authors)]),
            links=PaperLinks(record=url, landing_page=url),
            citations=[PaperCitation(db="Yiigle", count=cited)] if cited else [],
            identifiers=PaperIdentifiers(doi=normalize_doi(str(row.get("artDoi") or "")), issn=issn if issn != "None" else ""),
            keywords=PaperKeywords(author_keywords=list(dict.fromkeys(_list(row.get("artificialKeywords"))))),
            abstract=_text(row.get("artAbstract")),
            provider="yiigle",
            raw={key: row.get(key) for key in ("artId", "artTitle", "journalCn", "artPubYear", "artDoi", "issn", "citationNum",
                                                "artificialKeywords", "authorNames")} | {"cited_by_count": cited},
        )


__all__ = ["YiigleSource", "yiigle_terms"]
