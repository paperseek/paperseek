from __future__ import annotations

from html import unescape
import re
import time
from typing import Any

from paperseek_core.key_pool import RequestKeys
from paperseek_core.models import PaperAuthor, PaperCitation, PaperIdentifiers, PaperKeywords, PaperLinks, PaperNames, PaperRecord, PaperSource
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport, split_api_keys


class GoogleScholarSource:
    name = "googlescholar"
    BASE_URL = "https://google.serper.dev/scholar"

    def __init__(self, *, transport: AsyncHttpTransport, api_key: str = "") -> None:
        self.transport = transport
        self.api_keys = split_api_keys(api_key)
        self._index = 0
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance",),
            metadata_fields=("title", "names", "source", "abstract", "citations"),
            supports_abstracts=True,
            supports_citations=True,
            supports_pdf_links=True,
            api_key="required",
            max_page_size=100,
            max_lane_limit=100,
            backoff_policy="rotate configured keys for retryable status or transport failure",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "Google Scholar search query is empty.")
        if not self.api_keys:
            raise SourceError(self.name, "SERPER_API_KEY or SERPER_API_KEYS is required for Google Scholar searches.", query=query)
        requested_limit = max(1, min(int(request.limit or 10), 100))
        page_size = 10
        page_number = max(1, int(request.page or 1))
        hits: list[PaperRecord] = []
        seen: set[str] = set()
        total = 0
        pages_scanned = 0
        empty_pages = 0
        max_pages = min(50, max(10, ((requested_limit + page_size - 1) // page_size) * 3))
        while pages_scanned < max_pages:
            response, info = await self._post_with_key_rotation({"q": query, "page": page_number}, query)
            pages_scanned += 1
            if response.status_code >= 400:
                raise SourceError(self.name, f"Google Scholar via Serper returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceError(self.name, "Google Scholar via Serper returned a non-JSON response.", body=response.text[:1000], query=query) from exc
            if not isinstance(payload, dict) or payload.get('error') or payload.get('message'):
                raise SourceError(self.name, "Google Scholar via Serper returned an unexpected JSON structure.", body=response.text[:1000], query=query)
            items = [item for item in payload.get("organic") or [] if isinstance(item, dict)]
            if items:
                empty_pages = 0
                total = max(total, self._total(payload, items, page_number, page_size))
                for item in items:
                    key = self._record_key(item)
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    if len(hits) < requested_limit:
                        hits.append(self._to_record(item))
                if len(hits) >= requested_limit or len(items) < page_size:
                    break
            else:
                empty_pages += 1
                if empty_pages >= (3 if hits else 5):
                    break
            page_number += 1
            self.last_response_info = info
        self.last_response_info = {**info, "pages_scanned": pages_scanned, "returned_before_truncate": len(hits), "estimated_total": total or len(hits)}
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=max(total, len(hits)), page=max(1, request.page), limit=requested_limit),
            hits=hits[:requested_limit],
        )

    async def _post_with_key_rotation(self, payload: dict[str, Any], query: str):
        keys = RequestKeys(self.api_keys[self._index % len(self.api_keys):] + self.api_keys[:self._index % len(self.api_keys)])
        def headers(_attempt: int) -> dict[str, str]:
            key = keys.next_key()
            self._index += 1
            return {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "paperseek/1.0", "X-API-KEY": key}
        return await self.transport.request(
            self.name, "POST", self.BASE_URL, json=payload, headers=headers,
            timeout=45, query=query, retry_auth=keys.reject_current,
        )

    @staticmethod
    def _record_key(item: dict[str, Any]) -> str:
        for key in ("id", "resultId", "link", "title"):
            value = str(item.get(key) or "").strip().lower()
            if value:
                return value
        return ""

    @staticmethod
    def _total(payload: dict[str, Any], records: list[Any], page: int, page_size: int) -> int:
        for path in (("searchInformation", "totalResults"), ("searchInformation", "total_results"), ("searchParameters", "totalResults")):
            value: Any = payload
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            try:
                if value not in (None, ""):
                    return int(str(value).replace(",", ""))
            except (TypeError, ValueError):
                pass
        return max(len(records), ((page - 1) * page_size) + len(records))

    @classmethod
    def _to_record(cls, item: dict[str, Any]) -> PaperRecord:
        scholar_id = str(item.get("id") or item.get("resultId") or "")
        title = cls._clean_text(item.get("title") or scholar_id or item.get("link") or "")
        link = item.get("link") or ""
        publication = item.get("publicationInfo") or {}
        year = cls._year(item.get("year")) or cls._year(cls._publication_summary(publication))
        authors = cls._authors(publication)
        source_title = cls._source_title(publication)
        cited = item.get("citedBy") or {}
        citation_count = 0
        citing_link = ""
        if isinstance(cited, dict):
            try:
                citation_count = int(cited.get("total") or cited.get("cites") or cited.get("count") or cited.get("value") or 0)
            except (TypeError, ValueError):
                citation_count = 0
            citing_link = cited.get("link") or ""
        elif cited not in (None, ""):
            try:
                citation_count = int(cited)
            except (TypeError, ValueError):
                citation_count = 0
        return PaperRecord(
            uid=f"googlescholar:{scholar_id}" if scholar_id else link or title,
            title=title,
            types=["scholarly result"],
            source_types=["scholarly result"],
            source=PaperSource(source_title=source_title or "Google Scholar", publish_year=year),
            names=PaperNames(authors=authors),
            links=PaperLinks(record=link, landing_page=link, pdf=item.get("pdfUrl") or "", citing_articles=citing_link),
            citations=[PaperCitation(db="Google Scholar", count=citation_count)] if citation_count else [],
            identifiers=PaperIdentifiers(),
            keywords=PaperKeywords(author_keywords=[]),
            abstract=cls._clean_snippet(item.get("snippet") or ""),
            provider="googlescholar",
            raw=item,
        )

    @staticmethod
    def _authors(publication: Any) -> list[PaperAuthor]:
        if not isinstance(publication, dict):
            return GoogleScholarSource._authors_from_summary(publication)
        authors = []
        if isinstance(publication.get("authors"), list):
            for author in publication["authors"]:
                if isinstance(author, dict):
                    name = GoogleScholarSource._clean_author_name(author.get("name") or author.get("title") or "")
                else:
                    name = GoogleScholarSource._clean_author_name(author)
                if name:
                    authors.append(PaperAuthor(display_name=name))
        return authors or GoogleScholarSource._authors_from_summary(publication)

    @staticmethod
    def _authors_from_summary(publication: Any) -> list[PaperAuthor]:
        parts = GoogleScholarSource._summary_parts(publication)
        if len(parts) < 2 or GoogleScholarSource._year(parts[0]):
            return []
        authors = []
        for part in re.split(r"\s*,\s*|;\s*", parts[0]):
            name = GoogleScholarSource._clean_author_name(part)
            if name:
                authors.append(PaperAuthor(display_name=name))
        return authors

    @staticmethod
    def _source_title(publication: Any) -> str:
        if isinstance(publication, dict):
            for key in ("journal", "venue"):
                value = GoogleScholarSource._clean_source_name(publication.get(key))
                if value:
                    return value
        for part in GoogleScholarSource._summary_parts(publication)[1:]:
            value = GoogleScholarSource._clean_source_name(part)
            if value:
                return value
        return "Google Scholar"

    @staticmethod
    def _publication_summary(publication: Any) -> str:
        if isinstance(publication, dict):
            return GoogleScholarSource._clean_text(publication.get("summary") or "")
        return GoogleScholarSource._clean_text(publication)

    @staticmethod
    def _summary_parts(publication: Any) -> list[str]:
        summary = GoogleScholarSource._publication_summary(publication)
        return [part for part in (GoogleScholarSource._clean_text(item) for item in re.split(r"\s+-\s+", summary)) if part]

    @staticmethod
    def _clean_author_name(value: Any) -> str:
        text = GoogleScholarSource._clean_text(value)
        text = re.sub(r"\bet\s+al\.?$", "", text, flags=re.IGNORECASE).strip()
        text = text.replace("...", "").strip(" ,;.-")
        if not text or GoogleScholarSource._year(text):
            return ""
        if re.search(r"://|www\.|\.com\b|\.org\b|\.net\b|\.edu\b", text, flags=re.IGNORECASE):
            return ""
        return text

    @staticmethod
    def _clean_source_name(value: Any) -> str:
        text = GoogleScholarSource._clean_text(value)
        text = re.sub(r"\b(19|20)\d{2}\b", "", text)
        text = re.sub(r"\bAvailable at\s+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\bSSRN\s+\d+\b", "SSRN", text, flags=re.IGNORECASE)
        text = re.sub(r"\bet\s+al\.?\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text.replace("...", " ")).strip(" ,;:-")
        return text

    @staticmethod
    def _clean_snippet(value: Any) -> str:
        text = GoogleScholarSource._clean_text(value)
        text = re.sub(r"^(?:\.\.\.|…)\s*,?\s*", "", text)
        text = re.sub(r"\s*(?:\.\.\.|…)$", "", text)
        return text.strip()

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = unescape(str(value or "")).replace("\u00a0", " ")
        return re.sub(r"\s+", " ", GoogleScholarSource._repair_mojibake(text)).strip()

    @staticmethod
    def _repair_mojibake(text: str) -> str:
        replacements = {
            "â€¦": "...",
            "â€“": "–",
            "â€”": "—",
            "â€œ": "“",
            "â€�": "”",
            "â€˜": "‘",
            "â€™": "’",
            "鈥�": "...",
            "鈥?": "...",
            "\u0431\u043d": "...",
        }
        for bad, good in replacements.items():
            text = text.replace(bad, good)

        def replace_pair(match: re.Match[str]) -> str:
            token = match.group(0)
            try:
                repaired = token.encode("gbk").decode("utf-8")
            except UnicodeError:
                return token
            return repaired if "\ufffd" not in repaired else token

        return re.sub(r"鈥[\u4e00-\u9fff]", replace_pair, text)

    @staticmethod
    def _year(value: Any) -> int | None:
        current_year = time.gmtime().tm_year + 2
        try:
            if value not in (None, ""):
                year = int(value)
                if 1500 <= year <= current_year:
                    return year
        except (TypeError, ValueError):
            pass
        for match in re.finditer(r"\b(19|20)\d{2}\b", str(value or "")):
            year = int(match.group(0))
            if year <= current_year:
                return year
        return None
