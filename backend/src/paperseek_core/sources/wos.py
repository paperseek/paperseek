from __future__ import annotations

from typing import Any
from urllib.parse import quote

from paperseek_core.models import PaperAuthor, PaperCitation, PaperIdentifiers, PaperKeywords, PaperLinks, PaperNames, PaperRecord, PaperSource
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _to_int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        return next((text for item in value if (text := _first_text(item))), "")
    if isinstance(value, dict):
        return next((text for key in ("value", "name", "displayName", "display_name", "title") if (text := _first_text(value.get(key)))), "")
    return str(value or "").strip()


def record_from_json(item: dict[str, Any]) -> PaperRecord:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    names = item.get("names") if isinstance(item.get("names"), dict) else {}
    identifiers = item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {}
    links = item.get("links") if isinstance(item.get("links"), dict) else {}
    keywords = item.get("keywords") if isinstance(item.get("keywords"), dict) else {}
    authors = []
    for author in _as_list(names.get("authors")):
        if isinstance(author, dict):
            authors.append(PaperAuthor(
                display_name=_first_text(author.get("displayName") or author.get("display_name") or author.get("name")),
                wos_standard=_first_text(author.get("wosStandard") or author.get("wos_standard")),
                researcher_id=_first_text(author.get("researcherId") or author.get("researcher_id")),
            ))
        else:
            authors.append(PaperAuthor(display_name=_first_text(author)))
    citations = [PaperCitation(db=_first_text(citation.get("db")), count=_to_int(citation.get("count")) or 0) for citation in _as_list(item.get("citations")) if isinstance(citation, dict)]
    return PaperRecord(
        uid=_first_text(item.get("uid") or item.get("id") or identifiers.get("wosuid") or item.get("title")),
        title=_first_text(item.get("title")),
        types=[_first_text(value) for value in _as_list(item.get("types")) if _first_text(value)],
        source_types=[_first_text(value) for value in _as_list(item.get("sourceTypes")) if _first_text(value)],
        source=PaperSource(
            source_title=_first_text(source.get("sourceTitle") or source.get("source_title")),
            publish_year=_to_int(source.get("publishYear") or source.get("publish_year")),
            publish_month=_first_text(source.get("publishMonth") or source.get("publish_month")),
            volume=_first_text(source.get("volume")),
            issue=_first_text(source.get("issue")),
        ),
        names=PaperNames(authors=authors),
        links=PaperLinks(
            record=_first_text(links.get("record")),
            citing_articles=_first_text(links.get("citingArticles") or links.get("citing_articles")),
            references=_first_text(links.get("references")),
            related=_first_text(links.get("related")),
            landing_page=_first_text(links.get("record") or links.get("landingPage") or links.get("landing_page")),
        ),
        citations=citations,
        identifiers=PaperIdentifiers(
            doi=_first_text(identifiers.get("doi")),
            issn=_first_text(identifiers.get("issn")),
            eissn=_first_text(identifiers.get("eissn")),
            isbn=_first_text(identifiers.get("isbn")),
            eisbn=_first_text(identifiers.get("eisbn")),
            pmid=_first_text(identifiers.get("pmid")),
        ),
        keywords=PaperKeywords(author_keywords=[_first_text(value) for value in _as_list(keywords.get("authorKeywords") or keywords.get("author_keywords")) if _first_text(value)]),
        provider="wos",
        raw=item,
    )


def result_from_json(payload: dict[str, Any], limit: int) -> SourceSearchResult:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    return SourceSearchResult(
        metadata=SourceSearchMetadata(total=_to_int(metadata.get("total")) or 0, page=_to_int(metadata.get("page")) or 1, limit=_to_int(metadata.get("limit")) or limit),
        hits=[record_from_json(item) for item in hits if isinstance(item, dict)],
    )


class WosSource:
    name = "wos"
    BASE_URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"

    def __init__(self, *, transport: AsyncHttpTransport, api_key: str, db: str = "WOS") -> None:
        self.transport = transport
        self.api_key = str(api_key or "").strip()
        self.db = str(db or "WOS").strip() or "WOS"
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "impact", "recent"),
            metadata_fields=("title", "names", "source", "identifiers", "citations", "keywords"),
            supports_citations=True,
            api_key="required",
            max_page_size=50,
            max_lane_limit=100,
            single_page_lanes=("relevance", "impact", "recent"),
            backoff_policy="3 attempts for transport, rate limiting, and transient HTTP failures",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "WoS search query is empty.")
        if not self.api_key:
            raise SourceError(self.name, "WoS API key is required.", query=query)
        limit = max(1, min(int(request.limit or 10), 50))
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            self.BASE_URL,
            params={"q": query, "db": self.db, "limit": limit, "sortField": self.sort_field_for_lane(request.lane)},
            headers={"Accept": "application/json", "User-Agent": "paperseek-wos-client/0.3", "X-ApiKey": self.api_key},
            timeout=45.0,
            query=query,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceError(self.name, f"WoS returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "WoS returned a non-JSON response.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('metadata'), dict) or not isinstance(payload.get('hits'), list):
            raise SourceError(self.name, "WoS returned an unexpected JSON structure.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        return result_from_json(payload, limit)

    async def get_document(self, uid: str, *, detail: str | None = None) -> PaperRecord:
        normalized_uid = str(uid or "").strip()
        if not normalized_uid:
            raise SourceError(self.name, "WoS document UID is empty.")
        params = {"db": self.db}
        if detail is not None:
            params["detail"] = detail
        url = f"{self.BASE_URL}/{quote(normalized_uid, safe=':')}"
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            url,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "paperseek-wos-client/0.3", "X-ApiKey": self.api_key},
            timeout=45.0,
            query=normalized_uid,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceError(self.name, f"WoS returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=normalized_uid)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "WoS returned a non-JSON response.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=normalized_uid) from exc
        if not isinstance(payload, dict):
            raise SourceError(self.name, "WoS returned an unexpected JSON structure.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=normalized_uid)
        return record_from_json(payload)

    @staticmethod
    def sort_field_for_lane(lane: str) -> str:
        if lane == "impact":
            return "TC+D"
        if lane == "recent":
            return "PY+D"
        return "RS+D"
