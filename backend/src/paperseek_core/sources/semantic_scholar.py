from __future__ import annotations

from typing import Any

from paperseek_core.models import PaperAuthor, PaperCitation, PaperIdentifiers, PaperKeywords, PaperLinks, PaperNames, PaperRecord, PaperSource
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi


class SemanticScholarSource:
    name = "semanticscholar"
    BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    BULK_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
    FULL_FIELDS = (
        "paperId",
        "corpusId",
        "title",
        "abstract",
        "year",
        "publicationDate",
        "venue",
        "publicationVenue",
        "publicationTypes",
        "authors",
        "url",
        "externalIds",
        "citationCount",
        "fieldsOfStudy",
        "openAccessPdf",
    )
    FALLBACK_FIELDS = (
        "paperId",
        "corpusId",
        "title",
        "year",
        "publicationDate",
        "venue",
        "authors",
        "url",
        "externalIds",
        "citationCount",
    )

    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        api_key: str = "",
        throttle_seconds: float = 1.2,
    ) -> None:
        self.transport = transport
        self.api_key = str(api_key or "").strip()
        self.throttle_seconds = max(0.0, throttle_seconds)
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "impact", "recent"),
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "citations", "keywords"),
            supports_abstracts=True,
            supports_citations=True,
            supports_pdf_links=True,
            api_key="optional",
            max_page_size=1000,
            page_size_by_lane={"relevance": 100, "impact": 1000, "recent": 1000},
            single_page_lanes=("impact", "recent"),
            backoff_policy="1.2 second global throttle; reduced-field retry after transient 5xx exhaustion",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "Semantic Scholar search query is empty.")
        bulk = request.lane in ("impact", "recent")
        page_size = max(1, min(int(request.limit or 10), 1000 if bulk else 100))
        params: dict[str, Any] = {
            "query": query,
            "limit": page_size,
            "fields": ",".join(self.FULL_FIELDS),
        }
        if bulk:
            params["sort"] = "publicationDate:desc" if request.lane == "recent" else "citationCount:desc"
        else:
            params["offset"] = max(0, (int(request.page or 1) - 1) * page_size)
        url = self.BULK_URL if bulk else self.BASE_URL
        headers = {"Accept": "application/json", "User-Agent": "paperseek/1.0"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=45,
            min_interval=self.throttle_seconds,
            query=query,
        )
        if response.status_code >= 500:
            fallback = dict(params)
            fallback["fields"] = ",".join(self.FALLBACK_FIELDS)
            response, self.last_response_info = await self.transport.request(
                self.name,
                "GET",
                url,
                params=fallback,
                headers=headers,
                timeout=45,
                min_interval=self.throttle_seconds,
                query=query,
            )
            self.last_response_info["fallback"] = "basic_fields"
        if response.status_code >= 400:
            raise SourceError(
                self.name,
                f"Semantic Scholar returned HTTP {response.status_code}.",
                status=response.status_code, retry_details=response.extensions.get("paperseek_retry"),
                body=response.text[:1000],
                query=query,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "Semantic Scholar returned a non-JSON response.", body=response.text[:1000], query=query) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise SourceError(self.name, "Semantic Scholar returned an invalid search payload.", query=query)
        hits = [self._to_record(item) for item in payload.get("data") or [] if isinstance(item, dict)]
        return SourceSearchResult(
            metadata=SourceSearchMetadata(
                total=int(payload.get("total") or len(hits)),
                page=1 if bulk else max(1, request.page),
                limit=page_size,
            ),
            hits=hits,
        )


    @staticmethod
    def _to_record(item: dict[str, Any]) -> PaperRecord:
        external = item.get("externalIds") or {}
        paper_id = str(item.get("paperId") or "")
        corpus_id = str(item.get("corpusId") or "")
        title = item.get("title") or paper_id or corpus_id
        authors = [PaperAuthor(display_name=str(author.get("name") or "")) for author in item.get("authors") or [] if isinstance(author, dict) and author.get("name")]
        venue_obj = item.get("publicationVenue") or {}
        venue = venue_obj.get("name") if isinstance(venue_obj, dict) else ""
        open_pdf = item.get("openAccessPdf") or {}
        pdf = open_pdf.get("url") or "" if isinstance(open_pdf, dict) else ""
        citation_count = int(item.get("citationCount") or 0)
        publication_types = [str(value) for value in item.get("publicationTypes") or [] if value]
        return PaperRecord(
            uid=paper_id or f"CorpusId:{corpus_id}" or title,
            title=title,
            types=publication_types,
            source_types=publication_types,
            source=PaperSource(source_title=venue or item.get("venue") or "", publish_year=item.get("year")),
            names=PaperNames(authors=authors),
            links=PaperLinks(record=item.get("url") or "", landing_page=item.get("url") or "", pdf=pdf),
            citations=[PaperCitation(db="Semantic Scholar", count=citation_count)] if citation_count else [],
            identifiers=PaperIdentifiers(
                doi=normalize_doi(external.get("DOI") or external.get("DOIUrl") or ""),
                pmid=str(external.get("PubMed") or ""),
                arxiv=str(external.get("ArXiv") or ""),
                semanticscholar=paper_id,
            ),
            keywords=PaperKeywords(author_keywords=[str(value) for value in item.get("fieldsOfStudy") or [] if value]),
            abstract=item.get("abstract") or "",
            provider="semanticscholar",
            raw=item,
        )
