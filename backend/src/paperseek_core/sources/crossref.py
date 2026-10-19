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
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi


class CrossrefSource:
    name = "crossref"
    BASE_URL = "https://api.crossref.org/works"
    SELECT = ",".join(("DOI", "title", "author", "published-print", "published-online", "issued", "container-title", "type", "is-referenced-by-count", "URL", "abstract", "ISSN", "ISBN"))

    def __init__(self, *, transport: AsyncHttpTransport, email: str = "") -> None:
        self.transport = transport
        self.email = str(email or "").strip()
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "impact", "recent"),
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "citations"),
            supports_abstracts=True,
            supports_citations=True,
            api_key="not_required",
            max_page_size=100,
            lane_concurrency=2,
            backoff_policy="3 attempts for transport, 429, and transient 5xx responses",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "Crossref search query is empty.")
        page_size = max(1, min(int(request.limit or 10), 100))
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": page_size,
            "offset": max(0, (int(request.page or 1) - 1) * page_size),
            "order": "desc",
            "select": self.SELECT,
        }
        if request.lane == "impact":
            params["sort"] = "is-referenced-by-count"
        elif request.lane == "recent":
            params["sort"] = "published"
        if self.email:
            params["mailto"] = self.email
        headers = {"Accept": "application/json", "User-Agent": self._user_agent()}
        response, self.last_response_info = await self.transport.request(self.name, "GET", self.BASE_URL, params=params, headers=headers, timeout=45, query=query)
        if response.status_code >= 400:
            raise SourceError(self.name, f"Crossref returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "Crossref returned a non-JSON response.", body=response.text[:1000], query=query) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('message'), dict) or not isinstance(payload['message'].get('items'), list):
            raise SourceError(self.name, "Crossref returned an invalid search payload.", query=query)
        message = payload['message']
        hits = [self._to_record(item) for item in message.get("items") or [] if isinstance(item, dict)]
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=int(message.get("total-results") or 0), page=max(1, request.page), limit=page_size),
            hits=hits,
        )


    def _user_agent(self) -> str:
        return f"paperseek/1.0 (mailto:{self.email})" if self.email else "paperseek/1.0"

    @staticmethod
    def _first(value: Any) -> str:
        if isinstance(value, list) and value:
            return str(value[0] or "")
        return str(value or "")

    @staticmethod
    def _clean_abstract(value: Any) -> str:
        abstract = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(value or ""))).strip()
        return abstract.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')

    @classmethod
    def _to_record(cls, item: dict[str, Any]) -> PaperRecord:
        doi = normalize_doi(item.get("DOI") or "")
        title = cls._first(item.get("title")) or doi
        authors = []
        for author in item.get("author") or []:
            name = " ".join(part for part in (author.get("given") or "", author.get("family") or "") if part).strip() or author.get("name") or ""
            if name:
                authors.append(PaperAuthor(display_name=name, wos_standard=name))
        year = None
        for key in ("published-print", "published-online", "issued"):
            parts = ((item.get(key) or {}).get("date-parts") or [])
            if parts and parts[0]:
                try:
                    year = int(parts[0][0])
                    break
                except (TypeError, ValueError):
                    pass
        work_type = item.get("type") or ""
        abstract = cls._clean_abstract(item.get("abstract") or "")
        url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        citation_count = int(item.get("is-referenced-by-count") or 0)
        return PaperRecord(
            uid=f"DOI:{doi}" if doi else url or title,
            title=title,
            types=[work_type] if work_type else [],
            source_types=[work_type] if work_type else [],
            source=PaperSource(source_title=cls._first(item.get("container-title")), publish_year=year),
            names=PaperNames(authors=authors),
            links=PaperLinks(record=url, landing_page=url),
            citations=[PaperCitation(db="Crossref", count=citation_count)] if citation_count else [],
            identifiers=PaperIdentifiers(doi=doi, issn=cls._first(item.get("ISSN")), isbn=cls._first(item.get("ISBN"))),
            keywords=PaperKeywords(author_keywords=[]),
            abstract=abstract,
            provider="crossref",
            raw=item,
        )
