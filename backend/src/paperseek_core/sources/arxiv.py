from __future__ import annotations

from html import unescape
import re
from typing import Any
import xml.etree.ElementTree as ET

from paperseek_core.models import PaperAuthor, PaperIdentifiers, PaperKeywords, PaperLinks, PaperNames, PaperRecord, PaperSource
from paperseek_core.disciplines import apply_arxiv_category_filter
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi


class ArxivSource:
    name = "arxiv"
    BASE_URL = "https://export.arxiv.org/api/query"
    ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    def __init__(self, *, transport: AsyncHttpTransport, throttle_seconds: float = 3.1) -> None:
        self.transport = transport
        self.throttle_seconds = max(0.0, throttle_seconds)
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "recent"),
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "keywords"),
            supports_abstracts=True,
            supports_pdf_links=True,
            supports_hard_field_filter=True,
            api_key="not_required",
            max_page_size=100,
            max_lane_limit=200,
            backoff_policy="3.1 second global throttle; 3 attempts for transient HTTP failures",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "arXiv search query is empty.")
        page_size = max(1, min(int(request.limit or 10), 100))
        params = {
            "search_query": apply_arxiv_category_filter(
                self._search_query(query),
                request.field_ids,
            ),
            "start": max(0, (int(request.page or 1) - 1) * page_size),
            "max_results": page_size,
            "sortBy": "submittedDate" if request.lane == "recent" else "relevance",
            "sortOrder": "descending",
        }
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            self.BASE_URL,
            params=params,
            headers={"Accept": "application/atom+xml", "User-Agent": "paperseek/1.0"},
            timeout=45,
            min_interval=self.throttle_seconds,
            query=query,
        )
        if response.status_code >= 400:
            raise SourceError(self.name, f"arXiv returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise SourceError(self.name, "arXiv returned invalid Atom XML.", body=response.text[:1000], query=query) from exc
        if root.tag != '{http://www.w3.org/2005/Atom}feed' or any(
            '/api/errors' in (entry.findtext('atom:id', default='', namespaces=self.ATOM_NS))
            for entry in root.findall('atom:entry', self.ATOM_NS)
        ):
            raise SourceError(self.name, "arXiv returned an error feed.", status=400, query=query)
        total_text = root.findtext("opensearch:totalResults", default="", namespaces={"opensearch": "http://a9.com/-/spec/opensearch/1.1/"})
        try:
            total = int((total_text or "0").strip())
        except ValueError:
            total = 0
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=total, page=max(1, request.page), limit=page_size),
            hits=[self._to_record(entry) for entry in root.findall("atom:entry", self.ATOM_NS)],
        )


    @staticmethod
    def _search_query(query: str) -> str:
        if re.search(r"\b(?:all|ti|au|abs|cat|id|jr):", query, flags=re.I):
            return query
        cleaned = re.sub(r"\s+", " ", query).strip()
        if re.search(r"\b(AND|OR|ANDNOT)\b", cleaned, flags=re.I):
            return cleaned
        return f'all:"{cleaned.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'

    def _text(self, entry: ET.Element, path: str) -> str:
        return entry.findtext(path, default="", namespaces=self.ATOM_NS)

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", unescape(text or "")).strip()

    def _to_record(self, entry: ET.Element) -> PaperRecord:
        url = self._text(entry, "atom:id")
        arxiv_id = url.rstrip("/").split("/")[-1]
        authors = [PaperAuthor(display_name=self._clean(node.findtext("atom:name", default="", namespaces=self.ATOM_NS))) for node in entry.findall("atom:author", self.ATOM_NS)]
        authors = [author for author in authors if author.display_name]
        pdf = ""
        landing = url
        for link in entry.findall("atom:link", self.ATOM_NS):
            href = link.attrib.get("href") or ""
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf = href
            elif link.attrib.get("rel") == "alternate":
                landing = href
        categories: list[str] = []
        primary = entry.find("arxiv:primary_category", self.ATOM_NS)
        if primary is not None and primary.attrib.get("term"):
            categories.append(primary.attrib["term"])
        for category in entry.findall("atom:category", self.ATOM_NS):
            term = category.attrib.get("term")
            if term and term not in categories:
                categories.append(term)
        date = self._text(entry, "atom:published") or self._text(entry, "atom:updated")
        match = re.match(r"^(\d{4})", date or "")
        return PaperRecord(
            uid=f"arxiv:{arxiv_id}" if arxiv_id else url or self._clean(self._text(entry, "atom:title")),
            title=self._clean(self._text(entry, "atom:title")),
            types=["preprint"],
            source_types=["preprint"],
            source=PaperSource(source_title="arXiv", publish_year=int(match.group(1)) if match else None),
            names=PaperNames(authors=authors),
            links=PaperLinks(record=landing, landing_page=landing, pdf=pdf),
            identifiers=PaperIdentifiers(doi=normalize_doi(self._text(entry, "arxiv:doi")), arxiv=arxiv_id),
            keywords=PaperKeywords(author_keywords=categories),
            abstract=self._clean(self._text(entry, "atom:summary")),
            provider=self.name,
            raw={"id": arxiv_id, "categories": categories, "url": url},
        )
