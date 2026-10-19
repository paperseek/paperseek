from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from paperseek_core.models import PaperRecord
from paperseek_core.ports import SourcePort
from paperseek_core.sources import SourceError, SourceSearchRequest
from paperseek_core.sources.arxiv import ArxivSource
from paperseek_core.sources.crossref import CrossrefSource
from paperseek_core.sources.google_scholar import GoogleScholarSource
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openalex import OpenAlexSource
from paperseek_core.sources.paperhub import PaperHubSource
from paperseek_core.sources.pubmed import PubMedSource
from paperseek_core.sources import rate_limit
from paperseek_core.sources.semantic_scholar import SemanticScholarSource
from paperseek_core.sources.wos import WosSource


FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "providers"


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeAsyncClient:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        call = {
            "method": method,
            "url": url,
            "params": kwargs.get("params"),
            "json": kwargs.get("json"),
            "headers": kwargs.get("headers"),
            "timeout": kwargs.get("timeout"),
        }
        self.calls.append(call)
        route = self.routes[url]
        if callable(route):
            route = route(call)
        if isinstance(route, list):
            route = route.pop(0)
        request = httpx.Request(method, url)
        if isinstance(route, Exception):
            if isinstance(route, httpx.RequestError) and route.request is None:
                route.request = request
            raise route
        status, content, content_type = route
        return httpx.Response(
            status,
            content=content,
            headers={"Content-Type": content_type},
            request=request,
        )


async def no_sleep(_seconds: float) -> None:
    return None


def json_route(name: str, status: int = 200) -> tuple[int, bytes, str]:
    return status, (FIXTURES / name).read_bytes(), "application/json"


def text_route(name: str, content_type: str) -> tuple[int, bytes, str]:
    return 200, (FIXTURES / name).read_bytes(), content_type


def captured(call: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in call.items()
        if key not in {"params", "json"} and value is not None
    }
    if call["method"] == "GET":
        result["params"] = call.get("params")
    if call.get("json") is not None:
        result["json"] = call["json"]
    return result


class M2ProviderHttpContractTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = load_json("request_contracts.json")

    def transport(self, routes: dict[str, Any]) -> tuple[AsyncHttpTransport, FakeAsyncClient]:
        # Independent provider fixtures must not inherit another fixture's 429.
        rate_limit._windows.clear()
        client = FakeAsyncClient(routes)
        return AsyncHttpTransport(client=client, sleep=no_sleep), client

    async def test_openalex_exact_request_and_mapping(self) -> None:
        transport, client = self.transport({OpenAlexSource.BASE_URL: json_route("openalex.json")})
        source = OpenAlexSource(
            transport=transport,
            api_key="fixture-openalex-key",
            email="fixture@example.org",
        )

        result = await source.search(
            SourceSearchRequest(
                query="AI governance",
                limit=25,
                page=3,
                field_ids=("17", "14"),
                lane="impact",
            )
        )

        self.assertEqual(captured(client.calls[0]), self.contracts["openalex"])
        self.assertEqual(result.metadata.model_dump(exclude_none=True), {"total": 1, "page": 3, "limit": 25})
        record = result.hits[0]
        self.assertIsInstance(record, PaperRecord)
        self.assertEqual(record.uid, "https://openalex.org/W123")
        self.assertEqual(record.identifiers.doi, "10.1234/openalex.fixture")
        self.assertEqual(record.identifiers.pmid, "123")
        self.assertEqual(record.names.authors[0].display_name, "Ada Lovelace")
        self.assertEqual(record.source.source_title, "Journal of AI Governance")
        self.assertEqual(record.abstract, "Governance evidence")
        self.assertEqual(record.citations[0].count, 12)

    async def test_openalex_semantic_filters_fields_locally_without_unsupported_parameter(self) -> None:
        works = [
            {"id": f"https://openalex.org/W{index}", "title": f"Paper {index}",
             "primary_topic": {"field": {"id": field}} if field else None}
            for index, field in enumerate([
                "https://openalex.org/fields/14", "https://openalex.org/fields/17",
                "https://openalex.org/fields/22", None,
            ])
        ]

        def semantic_response(call):
            if "filter" in call["params"]:
                return 400, b'{"error":"field filter not supported with semantic search"}', "application/json"
            return 200, json.dumps({"meta": {"count": 4}, "results": works}).encode(), "application/json"

        transport, client = self.transport({OpenAlexSource.BASE_URL: semantic_response})
        source = OpenAlexSource(transport=transport, api_key="fixture-openalex-key")
        result = await source.search(SourceSearchRequest(
            query="AI governance", lane="semantic", limit=100,
            field_ids=("https://openalex.org/fields/14", "17"),
        ))

        self.assertEqual([record.uid for record in result.hits], [work["id"] for work in works[:2]])
        self.assertEqual(result.metadata.raw_count, 4)
        self.assertEqual(client.calls[0]["params"]["per-page"], 50)
        self.assertEqual(client.calls[0]["params"]["search.semantic"], "AI governance")
        self.assertNotIn("sort", client.calls[0]["params"])
        self.assertNotIn("filter", client.calls[0]["params"])

        unrestricted = await source.search(SourceSearchRequest(query="AI governance", lane="semantic"))
        self.assertEqual([record.uid for record in unrestricted.hits], [work["id"] for work in works])

    async def test_crossref_exact_request_and_mapping(self) -> None:
        transport, client = self.transport({CrossrefSource.BASE_URL: json_route("crossref.json")})
        source = CrossrefSource(transport=transport, email="fixture@example.org")

        result = await source.search(
            SourceSearchRequest(query="AI governance", limit=25, page=3, lane="impact")
        )

        self.assertEqual(captured(client.calls[0]), self.contracts["crossref"])
        record = result.hits[0]
        self.assertEqual(record.uid, "DOI:10.1234/crossref.fixture")
        self.assertEqual(record.names.authors[0].display_name, "Ada Lovelace")
        self.assertEqual(record.source.source_title, "Journal of AI Governance")
        self.assertEqual(record.source.publish_year, 2023)
        self.assertEqual(record.abstract, "Crossref abstract.")
        self.assertEqual(record.identifiers.issn, "1234-5678")
        self.assertEqual(record.citations[0].count, 9)

    async def test_arxiv_exact_request_and_mapping(self) -> None:
        transport, client = self.transport(
            {ArxivSource.BASE_URL: text_route("arxiv.xml", "application/atom+xml")}
        )
        source = ArxivSource(transport=transport, throttle_seconds=0)

        result = await source.search(
            SourceSearchRequest(query="graph retrieval", limit=1, page=2, lane="recent")
        )

        self.assertEqual(captured(client.calls[0]), self.contracts["arxiv"])
        record = result.hits[0]
        self.assertEqual(result.metadata.total, 1)
        self.assertEqual(result.metadata.page, 2)
        self.assertEqual(record.uid, "arxiv:2401.00001v1")
        self.assertEqual(record.identifiers.doi, "10.1234/arxiv.fixture")
        self.assertEqual(record.keywords.author_keywords, ["cs.IR"])
        self.assertEqual(record.abstract, "Graph retrieval abstract.")

        await source.search(
            SourceSearchRequest(
                query="graph retrieval",
                limit=1,
                page=1,
                lane="relevance",
                field_ids=("cs.AI", "stat.ML"),
            )
        )
        self.assertEqual(
            client.calls[1]["params"]["search_query"],
            'all:"graph retrieval" AND (cat:cs.AI OR cat:stat.ML)',
        )

    async def test_semantic_scholar_exact_request_mapping_and_bulk_lanes(self) -> None:
        routes = {
            SemanticScholarSource.BASE_URL: json_route("semantic_scholar.json"),
            SemanticScholarSource.BULK_URL: (200, b'{"total":0,"data":[]}', "application/json"),
        }
        transport, client = self.transport(routes)
        source = SemanticScholarSource(
            transport=transport,
            api_key="fixture-semantic-key",
            throttle_seconds=0,
        )

        result = await source.search(
            SourceSearchRequest(query="graph learning", limit=1, page=3)
        )
        await source.search(SourceSearchRequest(query="graph learning", limit=125, page=4, lane="impact"))
        await source.search(SourceSearchRequest(query="graph learning", limit=125, page=4, lane="recent"))

        self.assertEqual(captured(client.calls[0]), self.contracts["semanticscholar"])
        impact, recent = client.calls[1:]
        self.assertEqual(impact["url"], SemanticScholarSource.BULK_URL)
        self.assertEqual(impact["params"]["limit"], 125)
        self.assertEqual(impact["params"]["sort"], "citationCount:desc")
        self.assertNotIn("offset", impact["params"])
        self.assertEqual(recent["params"]["sort"], "publicationDate:desc")
        record = result.hits[0]
        self.assertEqual(record.uid, "S2-FIXTURE")
        self.assertEqual(record.identifiers.doi, "10.1234/semantic.fixture")
        self.assertEqual(record.identifiers.pmid, "456")
        self.assertEqual(record.links.pdf, "https://example.org/semantic.pdf")
        self.assertEqual(record.source.source_title, "Graph Journal")
        self.assertEqual(record.citations[0].count, 7)

    async def test_pubmed_exact_request_sequence_and_mapping(self) -> None:
        transport, client = self.transport(
            {
                PubMedSource.SEARCH_URL: json_route("pubmed_esearch.json"),
                PubMedSource.SUMMARY_URL: json_route("pubmed_esummary.json"),
                PubMedSource.FETCH_URL: text_route("pubmed_efetch.xml", "application/xml"),
            }
        )
        source = PubMedSource(
            transport=transport,
            api_key="fixture-pubmed-key",
            email="fixture@example.org",
            tool="paperseek-fixture",
        )

        result = await source.search(
            SourceSearchRequest(query="cancer", limit=1, page=2, lane="recent")
        )

        expected = [{"method": "GET", **item} for item in self.contracts["pubmed"]["requests"]]
        self.assertEqual([captured(call) for call in client.calls], expected)
        record = result.hits[0]
        self.assertEqual(record.uid, "PMID:123")
        self.assertEqual(record.identifiers.doi, "10.1234/pubmed.fixture")
        self.assertEqual(record.source.source_title, "Medical Evidence Journal")
        self.assertEqual(record.abstract, "BACKGROUND: PubMed fixture abstract.")

    async def test_google_scholar_exact_request_and_mapping(self) -> None:
        transport, client = self.transport(
            {GoogleScholarSource.BASE_URL: json_route("google_scholar.json")}
        )
        source = GoogleScholarSource(transport=transport, api_key="fixture-serper-key")

        result = await source.search(
            SourceSearchRequest(query="AI governance", limit=1, page=2)
        )

        self.assertEqual(captured(client.calls[0]), self.contracts["googlescholar"])
        record = result.hits[0]
        self.assertEqual(result.metadata.total, 11)
        self.assertEqual(record.uid, "googlescholar:GS-FIXTURE")
        self.assertEqual(record.names.authors[0].display_name, "Ada Lovelace")
        self.assertEqual(record.source.source_title, "Journal of AI Policy")
        self.assertEqual(record.abstract, "A Google Scholar result snippet.")
        self.assertEqual(record.citations[0].count, 12)

    async def test_google_scholar_locked_metadata_cleanup_semantics(self) -> None:
        record = GoogleScholarSource._to_record(
            {
                "id": "gs-clean",
                "title": "Digital鈥揜eal Economy Integration",
                "snippet": "鈥?, resource allocation and industrial innovation 鈥?",
                "publicationInfo": {
                    "summary": "Y Feng, Y Gao, L Yang鈥? - Resources Policy, 2024 - Elsevier"
                },
                "citedBy": {"cites": 41},
            }
        )
        self.assertEqual(record.title, "Digital–Real Economy Integration")
        self.assertEqual([author.display_name for author in record.names.authors], ["Y Feng", "Y Gao", "L Yang"])
        self.assertEqual(record.source.source_title, "Resources Policy")
        self.assertEqual(record.source.publish_year, 2024)
        self.assertEqual(record.abstract, "resource allocation and industrial innovation")

        implausible_year = GoogleScholarSource._to_record(
            {
                "id": "gs-ssrn",
                "title": "Investment",
                "year": 3505,
                "publicationInfo": {
                    "summary": "JMR Oualyбн - Available at SSRN 3505572, 2019 - papers.ssrn.com"
                },
            }
        )
        self.assertEqual([author.display_name for author in implausible_year.names.authors], ["JMR Oualy"])
        self.assertEqual(implausible_year.source.source_title, "SSRN")
        self.assertEqual(implausible_year.source.publish_year, 2019)

    async def test_paperhub_exact_request_sequence_sort_paging_and_mapping(self) -> None:
        shard_url = f"{PaperHubSource.RAW_BASE_URL}/shards/iclr.fixture.json"
        transport, client = self.transport(
            {
                PaperHubSource.MANIFEST_URL: json_route("paperhub_manifest.json"),
                shard_url: json_route("paperhub_shard.json"),
            }
        )
        source = PaperHubSource(transport=transport)

        result = await source.search(
            SourceSearchRequest(query="graph retrieval", limit=1, page=2, lane="recent")
        )

        expected = [{"method": "GET", **item} for item in self.contracts["paperhub"]["requests"]]
        self.assertEqual([captured(call) for call in client.calls], expected)
        self.assertEqual(result.metadata.model_dump(exclude_none=True), {"total": 2, "page": 2, "limit": 1})
        record = result.hits[0]
        self.assertEqual(record.uid, "paperhub-old")
        self.assertEqual(record.source.source_title, "ICLR")
        self.assertEqual(record.source.publish_year, 2023)
        self.assertEqual(record.names.authors[0].display_name, "Grace Hopper")

    async def test_wos_exact_request_limit_cap_and_mapping(self) -> None:
        transport, client = self.transport({WosSource.BASE_URL: json_route("wos.json")})
        source = WosSource(transport=transport, api_key="fixture-wos-key")

        result = await source.search(
            SourceSearchRequest(query="TS=(governance)", limit=100, page=2)
        )

        self.assertEqual(captured(client.calls[0]), self.contracts["wos"])
        self.assertEqual(result.metadata.total, 1)
        record = result.hits[0]
        self.assertEqual(record.uid, "WOS:FIXTURE")
        self.assertEqual(record.provider, "wos")
        self.assertEqual(record.identifiers.doi, "10.1234/wos.fixture")
        self.assertEqual(record.names.authors[0].wos_standard, "Lovelace, Ada")
        self.assertEqual(record.citations[0].count, 8)

    async def test_wos_uid_detail_request_and_optional_omission(self) -> None:
        document_url = f"{WosSource.BASE_URL}/WOS:FIXTURE"
        payload = load_json("wos.json")["hits"][0]
        transport, client = self.transport(
            {document_url: (200, json.dumps(payload).encode(), "application/json")}
        )
        source = WosSource(transport=transport, api_key="fixture-wos-key")

        record = await source.get_document("WOS:FIXTURE", detail="full")
        await source.get_document("WOS:FIXTURE")

        self.assertEqual(record.uid, "WOS:FIXTURE")
        self.assertEqual(client.calls[0]["params"], {"db": "WOS", "detail": "full"})
        self.assertEqual(client.calls[1]["params"], {"db": "WOS"})

    async def test_optional_values_are_omitted_and_capabilities_are_explicit(self) -> None:
        transport, client = self.transport(
            {OpenAlexSource.BASE_URL: (200, b'{"meta":{"count":0},"results":[]}', "application/json")}
        )
        source = OpenAlexSource(transport=transport)
        await source.search(SourceSearchRequest(query="governance"))

        params = client.calls[0]["params"]
        self.assertNotIn("api_key", params)
        self.assertNotIn("mailto", params)
        self.assertNotIn("filter", params)
        self.assertNotIn("semantic", source.capabilities.lanes)
        self.assertEqual(source.capabilities.max_page_size, 100)
        self.assertEqual(source.capabilities.api_key, "recommended")

        transport, client = self.transport(
            {CrossrefSource.BASE_URL: (200, b'{"message":{"total-results":0,"items":[]}}', "application/json")}
        )
        await CrossrefSource(transport=transport).search(SourceSearchRequest(query="governance"))
        self.assertNotIn("mailto", client.calls[0]["params"])
        self.assertNotIn("sort", client.calls[0]["params"])
        self.assertEqual(client.calls[0]["headers"]["User-Agent"], "paperseek/1.0")

        transport, client = self.transport(
            {SemanticScholarSource.BASE_URL: (200, b'{"total":0,"data":[]}', "application/json")}
        )
        await SemanticScholarSource(transport=transport, throttle_seconds=0).search(
            SourceSearchRequest(query="governance")
        )
        self.assertNotIn("x-api-key", client.calls[0]["headers"])

        transport, client = self.transport(
            {PubMedSource.SEARCH_URL: (200, b'{"esearchresult":{"count":"0","idlist":[]}}', "application/json")}
        )
        await PubMedSource(transport=transport).search(SourceSearchRequest(query="governance"))
        self.assertNotIn("email", client.calls[0]["params"])
        self.assertNotIn("api_key", client.calls[0]["params"])
        self.assertEqual(client.calls[0]["params"]["tool"], "paperseek")

    async def test_all_eight_adapters_implement_source_port_and_declare_capabilities(self) -> None:
        transport, _ = self.transport({})
        sources = (
            OpenAlexSource(transport=transport),
            CrossrefSource(transport=transport),
            ArxivSource(transport=transport, throttle_seconds=0),
            SemanticScholarSource(transport=transport, throttle_seconds=0),
            PubMedSource(transport=transport),
            GoogleScholarSource(transport=transport, api_key="fixture-key"),
            PaperHubSource(transport=transport),
            WosSource(transport=transport, api_key="fixture-key"),
        )
        self.assertEqual({source.name for source in sources}, {
            "openalex", "crossref", "arxiv", "semanticscholar", "pubmed", "googlescholar", "paperhub", "wos"
        })
        for source in sources:
            with self.subTest(source=source.name):
                self.assertIsInstance(source, SourcePort)
                self.assertEqual(source.capabilities.source, source.name)
                self.assertTrue(source.capabilities.lanes)
                self.assertTrue(source.capabilities.backoff_policy)

    async def test_retry_after_does_not_retry_before_the_source_allows_it(self) -> None:
        delays = []
        clock = [100.0]
        statuses = iter((429, 200))
        def handle(request):
            return httpx.Response(next(statuses), headers={"Retry-After": "32"}, json={"meta": {"count": 0}, "results": []})
        async def wait(seconds):
            delays.append(seconds)
            clock[0] += seconds
        with patch.object(rate_limit, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                source = OpenAlexSource(transport=AsyncHttpTransport(client=client, sleep=wait))
                await source.search(SourceSearchRequest(query="governance"))
        self.assertEqual(delays, [32.0])

    async def test_status_transport_invalid_payload_and_empty_result_semantics(self) -> None:
        for status in (401, 403, 429, 503):
            with self.subTest(status=status):
                transport, client = self.transport(
                    {CrossrefSource.BASE_URL: (status, b'failure', "text/plain")}
                )
                source = CrossrefSource(transport=transport)
                with self.assertRaises(SourceError) as raised:
                    await source.search(SourceSearchRequest(query="governance"))
                self.assertEqual(raised.exception.source, "crossref")
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(raised.exception.query, "governance")
                expected_calls = 3 if status in (429, 503) else 1
                self.assertEqual(len(client.calls), expected_calls)

        timeout = httpx.TimeoutException("fixture timeout", request=httpx.Request("GET", CrossrefSource.BASE_URL))
        transport, client = self.transport({CrossrefSource.BASE_URL: timeout})
        with self.assertRaises(SourceError) as raised:
            await CrossrefSource(transport=transport).search(SourceSearchRequest(query="governance"))
        self.assertEqual(raised.exception.source, "crossref")
        self.assertIsNone(raised.exception.status)
        self.assertEqual(len(client.calls), 3)

        transport, _ = self.transport(
            {CrossrefSource.BASE_URL: (200, b'not-json', "text/plain")}
        )
        with self.assertRaises(SourceError) as raised:
            await CrossrefSource(transport=transport).search(SourceSearchRequest(query="governance"))
        self.assertIn("non-JSON", str(raised.exception))

        transport, _ = self.transport(
            {CrossrefSource.BASE_URL: (200, b'{"message":{"total-results":0,"items":[]}}', "application/json")}
        )
        result = await CrossrefSource(transport=transport).search(SourceSearchRequest(query="governance"))
        self.assertEqual(result.metadata.total, 0)
        self.assertEqual(result.hits, [])

    def source_case(
        self,
        name: str,
        route: Any,
    ) -> tuple[Any, FakeAsyncClient]:
        if name == "openalex":
            transport, client = self.transport({OpenAlexSource.BASE_URL: route})
            return OpenAlexSource(transport=transport), client
        if name == "crossref":
            transport, client = self.transport({CrossrefSource.BASE_URL: route})
            return CrossrefSource(transport=transport), client
        if name == "arxiv":
            transport, client = self.transport({ArxivSource.BASE_URL: route})
            return ArxivSource(transport=transport, throttle_seconds=0), client
        if name == "semanticscholar":
            transport, client = self.transport({SemanticScholarSource.BASE_URL: route})
            return SemanticScholarSource(transport=transport, throttle_seconds=0), client
        if name == "pubmed":
            transport, client = self.transport({PubMedSource.SEARCH_URL: route})
            return PubMedSource(transport=transport), client
        if name == "googlescholar":
            transport, client = self.transport({GoogleScholarSource.BASE_URL: route})
            return GoogleScholarSource(transport=transport, api_key="fixture-key"), client
        if name == "paperhub":
            PaperHubSource.reset_cache()
            transport, client = self.transport({PaperHubSource.MANIFEST_URL: route})
            return PaperHubSource(transport=transport), client
        if name == "wos":
            transport, client = self.transport({WosSource.BASE_URL: route})
            return WosSource(transport=transport, api_key="fixture-key"), client
        raise AssertionError(name)

    @staticmethod
    def request_for(name: str) -> SourceSearchRequest:
        query = "TS=(governance)" if name == "wos" else "governance"
        return SourceSearchRequest(query=query)

    async def test_final_http_status_classification_for_all_eight_sources(self) -> None:
        names = ("openalex", "crossref", "arxiv", "semanticscholar", "pubmed", "googlescholar", "paperhub", "wos")
        for name in names:
            for status in (401, 403, 429, 503):
                with self.subTest(source=name, status=status):
                    source, client = self.source_case(name, (status, b'{"error":"fixture"}', "application/json"))
                    with self.assertRaises(SourceError) as raised:
                        await source.search(self.request_for(name))
                    self.assertEqual(raised.exception.source, name)
                    self.assertEqual(raised.exception.status, status)
                    self.assertEqual(raised.exception.query, self.request_for(name).query)
                    expected = 3 if status in (429, 503) else 1
                    if name == "semanticscholar" and status == 503:
                        expected = 6
                    self.assertEqual(len(client.calls), expected)

    async def test_transport_failure_classification_for_all_eight_sources(self) -> None:
        names = ("openalex", "crossref", "arxiv", "semanticscholar", "pubmed", "googlescholar", "paperhub", "wos")
        for name in names:
            with self.subTest(source=name):
                failure = httpx.TimeoutException("fixture timeout", request=httpx.Request("GET", "https://fixture.invalid"))
                source, client = self.source_case(name, failure)
                with self.assertRaises(SourceError) as raised:
                    await source.search(self.request_for(name))
                self.assertEqual(raised.exception.source, name)
                self.assertIsNone(raised.exception.status)
                expected = 3
                self.assertEqual(len(client.calls), expected)

    async def test_invalid_payload_classification_for_all_eight_sources(self) -> None:
        names = ("openalex", "crossref", "arxiv", "semanticscholar", "pubmed", "googlescholar", "paperhub", "wos")
        for name in names:
            with self.subTest(source=name):
                source, _ = self.source_case(name, (200, b"not-a-payload", "text/plain"))
                with self.assertRaises(SourceError) as raised:
                    await source.search(self.request_for(name))
                self.assertEqual(raised.exception.source, name)
                if name == "wos":
                    self.assertEqual(raised.exception.status, 200)

    async def test_empty_result_semantics_for_all_eight_sources(self) -> None:
        empty_atom = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"><opensearch:totalResults>0</opensearch:totalResults></feed>'
        simple_cases = {
            "openalex": (200, b'{"meta":{"count":0},"results":[]}', "application/json"),
            "crossref": (200, b'{"message":{"total-results":0,"items":[]}}', "application/json"),
            "arxiv": (200, empty_atom, "application/atom+xml"),
            "semanticscholar": (200, b'{"total":0,"data":[]}', "application/json"),
            "pubmed": (200, b'{"esearchresult":{"count":"0","idlist":[]}}', "application/json"),
            "googlescholar": (200, b'{"organic":[]}', "application/json"),
            "wos": (200, b'{"metadata":{"total":0},"hits":[]}', "application/json"),
        }
        for name, route in simple_cases.items():
            with self.subTest(source=name):
                source, _ = self.source_case(name, route)
                result = await source.search(self.request_for(name))
                self.assertEqual(result.metadata.total, 0)
                self.assertEqual(result.hits, [])

        manifest = load_json("paperhub_manifest.json")
        manifest["shards"][0]["count"] = 0
        shard_url = f"{PaperHubSource.RAW_BASE_URL}/shards/iclr.fixture.json"
        transport, _ = self.transport(
            {
                PaperHubSource.MANIFEST_URL: (200, json.dumps(manifest).encode(), "application/json"),
                shard_url: (200, b'{"papers":[]}', "application/json"),
            }
        )
        PaperHubSource.reset_cache()
        result = await PaperHubSource(transport=transport).search(SourceSearchRequest(query="governance"))
        self.assertEqual(result.metadata.total, 0)
        self.assertEqual(result.hits, [])


if __name__ == "__main__":
    unittest.main()
