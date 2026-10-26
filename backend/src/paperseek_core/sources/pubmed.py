from __future__ import annotations

import re
from typing import Any
import xml.etree.ElementTree as ET

from paperseek_core.models import PaperAuthor, PaperIdentifiers, PaperKeywords, PaperLinks, PaperNames, PaperRecord, PaperSource
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi


class PubMedSource:
    name = "pubmed"
    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    SEARCH_URL = f"{BASE_URL}/esearch.fcgi"
    SUMMARY_URL = f"{BASE_URL}/esummary.fcgi"
    FETCH_URL = f"{BASE_URL}/efetch.fcgi"

    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        api_key: str = "",
        email: str = "",
        tool: str = "paperseek",
    ) -> None:
        self.transport = transport
        self.api_key = str(api_key or "").strip()
        self.email = str(email or "").strip()
        self.tool = str(tool or "paperseek").strip()
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "recent"),
            metadata_fields=("title", "names", "source", "identifiers", "abstract"),
            supports_abstracts=True,
            api_key="optional",
            max_page_size=100,
            max_lane_limit=500,
            backoff_policy="3 attempts for transport, 429, and transient 5xx responses",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "PubMed search query is empty.")
        page_size = max(1, min(int(request.limit or 10), 100))
        search_payload = await self._get_json(
            self.SEARCH_URL,
            {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": page_size,
                "retstart": max(0, (int(request.page or 1) - 1) * page_size),
                "sort": "pub+date" if request.lane == "recent" else "relevance",
            },
            query,
        )
        search_result = search_payload.get("esearchresult")
        if not isinstance(search_result, dict):
            raise SourceError(self.name, "PubMed returned an invalid search payload.", query=query)
        errors = search_result.get("errorlist") or {}
        if isinstance(errors, dict) and errors.get("fieldsnotfound"):
            raise SourceError(self.name, "PubMed rejected an unknown query field.", status=400, query=query)
        if not str(search_result.get('count', '')).isdigit() or not isinstance(search_result.get('idlist'), list):
            raise SourceError(self.name, "PubMed search count or identifiers are missing.", query=query)
        feedback = []
        for diagnostics in (errors, search_result.get('warninglist') or {}):
            if not isinstance(diagnostics, dict):
                continue
            for key in ('phrasesnotfound', 'quotedphrasesnotfound', 'phrasesignored', 'outputmessages'):
                values = diagnostics.get(key)
                if isinstance(values, list) and values:
                    feedback.append(f"PubMed {key}: " + "; ".join(str(v)[:160] for v in values[:6]))
        warnings = ['pubmed_query_warning'] if feedback else []
        ids = [str(value) for value in search_result.get("idlist") or [] if value]
        total = int(search_result.get("count") or 0)
        metadata = SourceSearchMetadata(total=total, page=max(1, request.page), limit=page_size)
        if not ids:
            return SourceSearchResult(metadata=metadata, hits=[], warnings=warnings, query_feedback=feedback)
        summaries = (await self._get_json(
            self.SUMMARY_URL,
            {"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
            query,
        )).get("result") or {}
        if not isinstance(summaries, dict):
            raise SourceError(self.name, "PubMed returned invalid summaries.", query=query)
        abstracts, abstracts_failed = await self._fetch_abstracts(ids, query)
        if abstracts_failed:
            warnings.append('pubmed_abstracts_unavailable')
        hits = [self._to_record(pmid, summaries[pmid], abstracts.get(pmid, "")) for pmid in ids if isinstance(summaries.get(pmid), dict)]
        if not hits:
            raise SourceError(self.name, "PubMed returned identifiers without usable summaries.", query=query)
        if len(hits) != len(ids):
            warnings.append('pubmed_metadata_partial')
        return SourceSearchResult(metadata=metadata, hits=hits, warnings=warnings, query_feedback=feedback)

    async def _get_json(self, url: str, params: dict[str, Any], query: str) -> dict[str, Any]:
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            url,
            params={**params, **self._common_params()},
            headers={"Accept": "application/json", "User-Agent": self._user_agent()},
            timeout=45,
            query=query,
            response_status=self._api_error_status,
        )
        if response.status_code >= 400:
            raise SourceError(self.name, f"PubMed returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "PubMed returned a non-JSON response.", body=response.text[:1000], query=query) from exc
        if not isinstance(payload, dict):
            raise SourceError(self.name, "PubMed returned an invalid JSON payload.", query=query)
        return payload

    @staticmethod
    def _api_error_status(response) -> int | None:
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get('error') or next((value.get('error') for value in payload.values()
                                             if isinstance(value, dict) and value.get('error')), None)
        if not error:
            return None
        text = str(error).lower()
        return 429 if 'rate limit' in text else 401 if 'api key' in text else 502

    async def _fetch_abstracts(self, ids: list[str], query: str) -> tuple[dict[str, str], bool]:
        try:
            response, self.last_response_info = await self.transport.request(
                self.name,
                "GET",
                self.FETCH_URL,
                params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml", **self._common_params()},
                headers={"Accept": "application/xml", "User-Agent": self._user_agent()},
                timeout=45,
                query=query,
            )
        except SourceError:
            return {}, True
        if response.status_code >= 400:
            return {}, True
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            return {}, True
        if root.find('.//ERROR') is not None or root.tag != 'PubmedArticleSet':
            return {}, True
        abstracts: dict[str, str] = {}
        for article in root.findall(".//PubmedArticle"):
            pmid = article.findtext(".//PMID") or ""
            parts = []
            for node in article.findall(".//Abstract/AbstractText"):
                text = "".join(node.itertext()).strip()
                if text:
                    parts.append(f"{node.attrib.get('Label')}: {text}" if node.attrib.get("Label") else text)
            if pmid and parts:
                abstracts[pmid] = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return abstracts, False

    def _common_params(self) -> dict[str, str]:
        params = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _user_agent(self) -> str:
        return f"paperseek/1.0 (mailto:{self.email})" if self.email else "paperseek/1.0"

    @staticmethod
    def _to_record(pmid: str, item: dict[str, Any], abstract: str) -> PaperRecord:
        doi = ""
        for article_id in item.get("articleids") or []:
            if isinstance(article_id, dict) and str(article_id.get("idtype") or "").lower() == "doi":
                doi = normalize_doi(article_id.get("value") or "")
                break
        authors = [PaperAuthor(display_name=str(author.get("name") or "")) for author in item.get("authors") or [] if isinstance(author, dict) and author.get("name")]
        date = item.get("pubdate") or item.get("epubdate") or ""
        match = re.search(r"\b(19|20)\d{2}\b", date)
        pub_types = [str(value) for value in item.get("pubtype") or [] if value]
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
        return PaperRecord(
            uid=f"PMID:{pmid}",
            title=item.get("title") or pmid,
            types=pub_types,
            source_types=pub_types,
            source=PaperSource(source_title=item.get("fulljournalname") or item.get("source") or "PubMed", publish_year=int(match.group(0)) if match else None,
                               volume=str(item.get("volume") or ""), issue=str(item.get("issue") or ""), pages=item.get("pages") or None),
            names=PaperNames(authors=authors),
            links=PaperLinks(record=url, landing_page=url),
            identifiers=PaperIdentifiers(doi=doi, pmid=pmid, issn=str(item.get("issn") or ""), eissn=str(item.get("essn") or "")),
            keywords=PaperKeywords(author_keywords=[]),
            abstract=abstract,
            provider="pubmed",
            raw=item,
        )
