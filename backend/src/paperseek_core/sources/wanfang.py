"""Wanfang Data (万方数据) open search API: Chinese journals, theses and conference papers.

POST https://api.wfdata.com/openwanfang/getQuery with header ``X-Ca-AppKey``. The query is a
Wanfang PQ expression (``(数字治理 OR 智慧治理) AND 乡村振兴``, ``标题:… AND 摘要:…``). Each
document carries its record under its resource type (``"Periodical": {...}``); the documented
``fields`` map with protobuf-style wrappers is also read. Multi-valued text fields put the
Chinese value first.
"""

from __future__ import annotations

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

# Chinese journal articles, theses and conference papers; patents, standards and laws are left out.
COLLECTIONS = ("OpenPeriodicalChi", "OpenThesis", "OpenConference")
RETURNED_FIELDS = (
    "Id", "Type", "Title", "Creator", "Abstract", "Keywords", "PublishYear", "PeriodicalTitle", "MeetingTitle",
    "OriginalOrganization", "Degree", "DOI", "ISSN", "Volum", "Issue", "Page", "CitedCount", "CorePeriodical",
)
_PAGE_TYPES = {"Periodical": "periodical", "Thesis": "thesis", "Conference": "conference"}
_DOCUMENT_TYPES = {"Periodical": "journal-article", "Thesis": "dissertation", "Conference": "proceedings-article"}


def field_value(value: Any) -> Any:
    """Unwrap ``{"stringValue": …}``-style values; a list value becomes a Python list."""
    if not isinstance(value, dict):
        return value
    if "listValue" in value:
        return [field_value(item) for item in (value["listValue"] or {}).get("values") or []]
    for key in ("stringValue", "numberValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _texts(value: Any) -> list[str]:
    value = field_value(value)
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _first(value: Any) -> str:
    texts = _texts(value)
    return texts[0] if texts else ""


def _authors(value: Any) -> list[str]:
    # Chinese names are followed by their pinyin forms in the same order; keep the Chinese ones.
    names = _texts(value)
    chinese = [name for name in names if re.search(r"[\u4e00-\u9fff]", name)]
    return chinese or names


class WanfangSource:
    name = "wanfang"
    BASE_URL = "https://api.wfdata.com/openwanfang/getQuery"

    def __init__(self, *, transport: AsyncHttpTransport, api_key: str = "") -> None:
        self.transport = transport
        self.api_key = str(api_key or "").strip()
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "impact", "recent"),
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "citations", "keywords"),
            supports_abstracts=True,
            supports_citations=True,
            api_key="required",
            max_page_size=100,
            max_lane_limit=5000,
            lane_concurrency=2,
            backoff_policy="3 attempts for transport, 429, and transient 5xx responses",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "Wanfang search query is empty.")
        if not self.api_key:
            raise SourceError(self.name, "Wanfang requires an AppKey.", status=401, query=query)
        page_size = max(1, min(int(request.limit or 20), 100))
        start = (max(1, int(request.page or 1)) - 1) * page_size
        if start >= 5000:
            return SourceSearchResult(metadata=SourceSearchMetadata(total=0, page=max(1, request.page), limit=page_size))
        sorts = {"impact": [{"by": "CitedCount", "order": "DESC"}],
                 "recent": [{"by": "PublishYear", "order": "DESC"}, {"by": "score", "order": "DESC"}]}
        body = {
            "collections": list(COLLECTIONS), "query": query, "returned_fields": list(RETURNED_FIELDS),
            "start": start, "rows": page_size,
            "sort": {"sorts": sorts.get(request.lane, [{"by": "score", "order": "DESC"}])},
        }
        response, self.last_response_info = await self.transport.request(
            self.name, "POST", self.BASE_URL, json=body, timeout=45, query=query,
            headers={"Content-Type": "application/json", "Accept": "application/json", "X-Ca-AppKey": self.api_key},
        )
        if response.status_code >= 400:
            raise SourceError(self.name, f"Wanfang returned HTTP {response.status_code}.", status=response.status_code,
                              retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "Wanfang returned a non-JSON response.", body=response.text[:1000], query=query) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("documents", []), list):
            raise SourceError(self.name, "Wanfang returned an invalid search payload.", body=str(payload)[:1000], query=query)
        try:
            total = int(payload.get("numFound") or payload.get("num_found") or 0)
        except (TypeError, ValueError):
            total = 0
        hits = [record for item in payload.get("documents") or [] if isinstance(item, dict) and (record := self._to_record(item))]
        feedback = [str(payload["msg"])] if payload.get("msg") else []
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=total, page=max(1, request.page), limit=page_size),
            hits=hits, query_feedback=feedback,
        )

    @staticmethod
    def _to_record(document: dict[str, Any]) -> PaperRecord | None:
        # The live API returns plain values under the resource type ("Periodical": {...}) and an
        # empty "fields"; the documented MAP form wraps each value in "fields".
        record = document.get(str(document.get("resourceType") or ""))
        source = record if isinstance(record, dict) and record else document.get("fields") or {}
        fields = {key: field_value(value) for key, value in source.items()}
        identity = _first(fields.get("Id"))
        title = _first(fields.get("Title"))
        if not identity or not title:
            return None
        kind = _first(fields.get("Type")) or str(document.get("resourceType") or "")
        venue = _first(fields.get("PeriodicalTitle")) or _first(fields.get("MeetingTitle"))
        if kind == "Thesis":
            venue = " ".join(part for part in (_first(fields.get("OriginalOrganization")), _first(fields.get("Degree"))) if part)
        try:
            year = int(float(fields.get("PublishYear"))) if fields.get("PublishYear") not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        try:
            cited = int(float(fields["CitedCount"])) if fields.get("CitedCount") not in (None, "") else None
        except (TypeError, ValueError):
            cited = None
        url = f"https://d.wanfangdata.com.cn/{_PAGE_TYPES.get(kind, 'periodical')}/{identity}"
        keywords = list(dict.fromkeys(_texts(fields.get("Keywords"))))
        return PaperRecord(
            uid=f"wanfang:{identity}",
            title=title,
            types=[_DOCUMENT_TYPES.get(kind, "journal-article")],
            source_types=[kind] if kind else [],
            source=PaperSource(source_title=venue, publish_year=year, volume=_first(fields.get("Volum")),
                               issue=_first(fields.get("Issue")), pages=_first(fields.get("Page")) or None),
            names=PaperNames(authors=[PaperAuthor(display_name=name) for name in _authors(fields.get("Creator"))]),
            links=PaperLinks(record=url, landing_page=url),
            citations=[PaperCitation(db="Wanfang", count=cited)] if cited else [],
            identifiers=PaperIdentifiers(doi=normalize_doi(_first(fields.get("DOI"))), issn=_first(fields.get("ISSN"))),
            keywords=PaperKeywords(author_keywords=keywords),
            abstract=_first(fields.get("Abstract")),
            provider="wanfang",
            raw={**fields, "cited_by_count": cited, "core_periodical": _texts(fields.get("CorePeriodical"))},
        )


__all__ = ["WanfangSource", "field_value"]
