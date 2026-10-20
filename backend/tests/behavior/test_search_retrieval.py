from __future__ import annotations

import json
import unittest
from pathlib import Path

import httpx
from fixtures.retrieval import FixtureSource, RecordingObserver, record, source_result

from paperseek_core.candidate_selection import CandidateSelectionPipeline
from paperseek_core.document_identity import document_key
from paperseek_core.errors import RetrievalSourceError
from paperseek_core.models import (
    PaperRecord,
    RetrievalEmbeddingResult,
    RetrievalRequest,
    RetrievalRerankItem,
    RetrievalRerankResult,
)
from paperseek_core.ports import EmbeddingPort, RerankPort, SourcePort
from paperseek_core.retrieval import RetrievalPipeline
from paperseek_core.retrieval_fusion import RetrievalLane
from paperseek_core.sources.arxiv import ArxivSource
from paperseek_core.sources.contracts import (
    SourceCapabilities,
)
from paperseek_core.sources.crossref import CrossrefSource
from paperseek_core.sources.google_scholar import GoogleScholarSource
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openalex import OpenAlexSource
from paperseek_core.sources.paperhub import PaperHubSource
from paperseek_core.sources.pubmed import PubMedSource
from paperseek_core.sources.semantic_scholar import SemanticScholarSource
from paperseek_core.sources.wos import WosSource

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_ROOT = ROOT / "tests" / "fixtures" / "golden_search"
QUESTION = "How do transparent AI decision systems affect accountability in public agencies?"








class FixtureEmbedding:
    def __init__(self, scores: list[float] | Exception) -> None:
        self.scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    async def embedding_scores(self, question: str, documents: list[PaperRecord], *, model=None) -> RetrievalEmbeddingResult:
        self.calls.append((question, [document.uid for document in documents]))
        if isinstance(self.scores, Exception):
            raise self.scores
        return RetrievalEmbeddingResult(scores=list(self.scores), model="fixture", provider="fixture")


class FixtureReranker:
    def __init__(self, result: RetrievalRerankResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, question: str, documents: list[PaperRecord]) -> RetrievalRerankResult:
        self.calls.append((question, [document.uid for document in documents]))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result




def golden_records() -> list[PaperRecord]:
    payload = json.loads((GOLDEN_ROOT / "source_responses.json").read_text(encoding="utf-8"))
    mapper = OpenAlexSource(transport=AsyncHttpTransport())
    return [mapper._to_record(work) for work in payload["relevance"]]


class M2MultiLaneRetrievalContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_bibliometrics_preserves_retrieved_corpus_before_selection_truncation(self):
        documents = [record(f"candidate-{i}") for i in range(12)]
        source = FixtureSource({}, capabilities=SourceCapabilities(source="fixture", lanes=("relevance",)))
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer).run(RetrievalRequest(
            question=QUESTION, query="query", primary_hits=documents + documents[:2],
            lane_limit=12, pool_max=5, ranking_candidate_limit=3,
        ))
        self.assertEqual(len(result.documents), 5)
        self.assertEqual(len(result.bibliometric_corpus.papers), 12)
        self.assertEqual({paper.id for paper in result.bibliometric_corpus.papers}, {document_key(paper) for paper in documents})
        self.assertEqual(len(observer.checkpoints[-1].state['bibliometric_corpus']['papers']), 12)
        lane_state = next(item.state for item in observer.checkpoints if item.step_key == 'retrieve.lane')
        resumed = await RetrievalPipeline(source, RecordingObserver()).run(RetrievalRequest(
            question=QUESTION, query="query", lane_limit=12, pool_max=5, ranking_candidate_limit=3,
            resume_lane_results=lane_state['lane_results'], completed_lanes=lane_state['completed_lanes'],
        ))
        self.assertEqual(len(resumed.documents), 5)
        self.assertEqual(len(resumed.bibliometric_corpus.papers), 12)

    async def test_openalex_semantic_reads_one_page_and_keeps_hard_field_restriction(self) -> None:
        for matching_count in (0, 2, 50):
            with self.subTest(matching_count=matching_count):
                calls = []

                def respond(request: httpx.Request) -> httpx.Response:
                    if "search.semantic" not in request.url.params:
                        return httpx.Response(200, json={"meta": {"count": 0}, "results": []})
                    calls.append(request)
                    self.assertNotIn("filter", request.url.params)
                    self.assertEqual(request.url.params["page"], "1")
                    self.assertEqual(request.url.params["per-page"], "50")
                    works = [
                        {"id": f"https://openalex.org/W{index}", "title": f"Paper {index}",
                         "primary_topic": {"field": {"id": f"https://openalex.org/fields/{14 if index < matching_count else 17}"}}}
                        for index in range(50)
                    ]
                    return httpx.Response(200, json={"meta": {"count": 50}, "results": works})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    source = OpenAlexSource(transport=AsyncHttpTransport(client=client), api_key="fixture")
                    observer = RecordingObserver()
                    result = await RetrievalPipeline(source, observer).run(RetrievalRequest(
                        question=QUESTION, query="AI governance", field_ids=["14"], lane_limit=200,
                    ))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.warnings, [])
                self.assertEqual(len(result.documents), matching_count)
                self.assertIn("semantic", result.completed_lanes)
                completed = next(event for event in observer.events
                                 if event.event_type == "retrieval.lane_completed" and event.payload["lane"] == "semantic")
                self.assertEqual(completed.payload["raw_count"], 50)
                self.assertEqual(completed.payload["pages_read"], 1)

    async def test_provider_lane_depth_page_and_concurrency_capabilities_match_characterization(self) -> None:
        transport = AsyncHttpTransport()
        sources = [
            ArxivSource(transport=transport, throttle_seconds=0),
            CrossrefSource(transport=transport),
            GoogleScholarSource(transport=transport, api_key="fixture"),
            OpenAlexSource(transport=transport),
            PaperHubSource(transport=transport),
            PubMedSource(transport=transport),
            SemanticScholarSource(transport=transport, throttle_seconds=0),
            WosSource(transport=transport, api_key="fixture"),
        ]
        expected = {
            "arxiv": (("relevance", "recent"), 100, 200, 1, ()),
            "crossref": (("relevance", "impact", "recent"), 100, 1000, 2, ()),
            "googlescholar": (("relevance",), 100, 100, 1, ()),
            "openalex": (("relevance", "impact", "recent"), 100, 1000, 2, ()),
            "paperhub": (("relevance", "recent", "local_quality"), 100, 1000, 1, ()),
            "pubmed": (("relevance", "recent"), 100, 500, 1, ()),
            "semanticscholar": (("relevance", "impact", "recent"), 1000, 1000, 1, ("impact", "recent")),
            "wos": (("relevance", "impact", "recent"), 50, 100, 1, ("relevance", "impact", "recent")),
        }
        for source in sources:
            capabilities = source.capabilities
            self.assertEqual(
                (
                    capabilities.lanes,
                    capabilities.max_page_size,
                    capabilities.max_lane_limit,
                    capabilities.lane_concurrency,
                    capabilities.single_page_lanes,
                ),
                expected[source.name],
                source.name,
            )
        semantic = next(source for source in sources if source.name == "semanticscholar")
        self.assertEqual(semantic.capabilities.page_size("relevance"), 100)
        self.assertEqual(semantic.capabilities.page_size("impact"), 1000)

    async def test_m0_fusion_scores_lane_provenance_and_order_are_exact(self) -> None:
        documents = golden_records()
        source = FixtureSource(
            {
                (RetrievalLane.IMPACT, 1): source_result([documents[1], documents[0], documents[2]], limit=3),
                (RetrievalLane.RECENT, 1): source_result([documents[2], documents[0], documents[1]], limit=3),
            }
        )
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer).run(
            RetrievalRequest(
                question=QUESTION,
                query="transparent AI accountability public agencies",
                primary_hits=documents,
                supplemental_lanes={
                    "query_view:direct_governance": documents,
                    "query_view:audit_mechanisms": documents,
                    "query_view:evidence_reviews": documents,
                    RetrievalLane.LOCAL_QUALITY: documents,
                },
                lane_limit=1000,
                ranking_candidate_limit=256,
                pool_max=3000,
                rrf_k=60,
            )
        )

        self.assertIsInstance(source, SourcePort)
        self.assertEqual([document.uid for document in result.documents], [f"https://openalex.org/W{value}" for value in (1001, 1002, 1003)])
        expected = {
            "doi:10.5555/fixture.1001": {
                "source_rank": 1,
                "retrieval_source_ranks": {"relevance": 1, "impact": 2, "recent": 2, "query_view:direct_governance": 1, "query_view:audit_mechanisms": 1, "query_view:evidence_reviews": 1, "local_quality": 1},
                "retrieval_score": 0.902569,
                "retrieval_lanes": ["relevance", "impact", "recent", "query_view:direct_governance", "query_view:audit_mechanisms", "query_view:evidence_reviews", "local_quality"],
                "retrieval_rrf_score": 0.097911,
                "retrieval_rrf_normalized": 1.0,
                "retrieval_primary_prior": 1.0,
                "retrieval_embedding_score": 0.561789,
                "retrieval_bm25_score": 1.0,
                "retrieval_term_coverage": 0.5,
            },
            "doi:10.5555/fixture.1002": {
                "source_rank": 1,
                "retrieval_source_ranks": {"relevance": 2, "impact": 1, "recent": 3, "query_view:direct_governance": 2, "query_view:audit_mechanisms": 2, "query_view:evidence_reviews": 2, "local_quality": 2},
                "retrieval_score": 0.395443,
                "retrieval_lanes": ["relevance", "impact", "recent", "query_view:direct_governance", "query_view:audit_mechanisms", "query_view:evidence_reviews", "local_quality"],
                "retrieval_rrf_score": 0.096807,
                "retrieval_rrf_normalized": 0.51088,
                "retrieval_primary_prior": 0.63093,
                "retrieval_embedding_score": 0.227462,
                "retrieval_bm25_score": 0.0,
                "retrieval_term_coverage": 0.2,
            },
            "doi:10.5555/fixture.1003": {
                "source_rank": 1,
                "retrieval_source_ranks": {"relevance": 3, "impact": 3, "recent": 1, "query_view:direct_governance": 3, "query_view:audit_mechanisms": 3, "query_view:evidence_reviews": 3, "local_quality": 3},
                "retrieval_score": 0.228513,
                "retrieval_lanes": ["relevance", "impact", "recent", "query_view:direct_governance", "query_view:audit_mechanisms", "query_view:evidence_reviews", "local_quality"],
                "retrieval_rrf_score": 0.095654,
                "retrieval_rrf_normalized": 0.0,
                "retrieval_primary_prior": 0.5,
                "retrieval_embedding_score": 0.347062,
                "retrieval_bm25_score": 0.60534,
                "retrieval_term_coverage": 0.3,
            },
        }
        for metadata in expected.values():
            metadata["retrieval_similarity_method"] = "local_hash"
            metadata["origins"] = ["retrieval"]
            metadata["sources"] = ["openalex"]
        self.assertEqual(result.provenance_by_identity, expected)
        self.assertEqual(result.lane_counts, {lane: 3 for lane in expected[next(iter(expected))]["retrieval_lanes"]})
        self.assertEqual(result.total_candidates, 3)
        self.assertEqual([call.lane for call in source.calls], [RetrievalLane.IMPACT, RetrievalLane.RECENT])
        self.assertEqual(result.warnings, [])
        self.assertEqual(observer.checkpoints[-1].step_key, "retrieve.fuse")
        self.assertEqual(observer.checkpoints[-1].workflow_version, "m2.retrieval.v2")
        self.assertEqual(observer.checkpoints[-1].state["candidate_pool"], [document.model_dump(mode="json") for document in result.documents])

    async def test_capability_limits_queries_paging_concurrency_and_completion_order_are_preserved(self) -> None:
        primary = [record(f"primary-{index}") for index in range(100)]
        page_two = [record(f"page-2-{index}") for index in range(50)]
        semantic = [record("semantic")]
        impact = [record("impact")]
        recent = [record("recent")]
        capabilities = SourceCapabilities(
            source="fixture",
            lanes=(RetrievalLane.RELEVANCE, RetrievalLane.SEMANTIC, RetrievalLane.IMPACT, RetrievalLane.RECENT),
            max_page_size=100,
            max_lane_limit=150,
            lane_concurrency=2,
        )
        source = FixtureSource(
            {
                (RetrievalLane.RELEVANCE, 2): source_result(page_two, page=2, limit=100, total=150),
                (RetrievalLane.SEMANTIC, 1): source_result(semantic, limit=100),
                (RetrievalLane.IMPACT, 1): source_result(impact, limit=100),
                (RetrievalLane.RECENT, 1): source_result(recent, limit=100),
            },
            capabilities=capabilities,
            delays={RetrievalLane.RELEVANCE: 0.02, RetrievalLane.SEMANTIC: 0.01},
        )
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer).run(
            RetrievalRequest(
                question="natural language research question",
                query="compiled boolean query",
                primary_hits=primary,
                lane_limit=1000,
                ranking_candidate_limit=256,
                pool_max=300,
            )
        )

        self.assertLessEqual(source.max_active, 2)
        self.assertEqual(source.max_active, 2)
        calls = {(call.lane, call.page): call for call in source.calls}
        self.assertEqual((calls[(RetrievalLane.RELEVANCE, 2)].query, calls[(RetrievalLane.RELEVANCE, 2)].limit), ("compiled boolean query", 100))
        self.assertEqual((calls[(RetrievalLane.SEMANTIC, 1)].query, calls[(RetrievalLane.SEMANTIC, 1)].limit), ("natural language research question", 100))
        self.assertEqual(calls[(RetrievalLane.IMPACT, 1)].limit, 100)
        self.assertEqual(calls[(RetrievalLane.RECENT, 1)].limit, 100)
        self.assertEqual(result.completed_lanes, list(capabilities.lanes))
        self.assertEqual(len(result.documents), 153)
        started = next(event for event in observer.events if event.event_type == "retrieval.started")
        self.assertEqual(started.payload["total_lanes"], 4)
        self.assertEqual(started.payload["lane_concurrency"], 2)
        progress = [event.payload for event in observer.events if event.event_type == "retrieval.progress"]
        self.assertEqual(progress[-1]["completed_lanes"], 4)
        self.assertEqual(progress[-1]["total_lanes"], 4)
        self.assertEqual(progress[-1]["lane_concurrency"], 2)
        completed = next(event for event in observer.events if event.event_type == "retrieval.completed")
        self.assertEqual(completed.payload["fusion"], "rrf")
        self.assertNotIn("llm_gate_candidate_count", completed.payload)

    async def test_partial_last_page_keeps_offset_and_all_unique_candidates(self) -> None:
        documents = [record(f"paper-{index}") for index in range(350)]

        class OffsetSource:
            name = "fixture"
            capabilities = SourceCapabilities(
                source=name, lanes=(RetrievalLane.RELEVANCE,), max_page_size=100,
                max_lane_limit=350,
            )
            calls = []

            async def search(self, request):
                self.calls.append((request.page, request.limit))
                start = (request.page - 1) * request.limit
                return source_result(
                    documents[start:start + request.limit], page=request.page,
                    limit=request.limit, total=len(documents),
                )

        source = OffsetSource()
        result = await RetrievalPipeline(source, RecordingObserver()).run(RetrievalRequest(
            question="paper", query="paper", primary_hits=documents[:100],
            lane_limit=350, pool_max=350, ranking_candidate_limit=350,
        ))
        self.assertEqual(source.calls, [(2, 100), (3, 100), (4, 100)])
        self.assertEqual({item.uid for item in result.documents}, {item.uid for item in documents})

    async def test_later_page_failure_retains_primary_page_and_is_checkpointed_visibly(self) -> None:
        primary = [record(f"primary-{index}") for index in range(100)]
        source = FixtureSource(
            {(RetrievalLane.RELEVANCE, 2): RuntimeError("sensitive upstream detail")},
            capabilities=SourceCapabilities(
                source="fixture",
                lanes=(RetrievalLane.RELEVANCE,),
                max_page_size=100,
                max_lane_limit=150,
            ),
        )
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer).run(
            RetrievalRequest(
                question="question",
                query="query",
                primary_hits=primary,
                lane_limit=150,
                ranking_candidate_limit=256,
                pool_max=150,
            )
        )

        self.assertEqual(len(result.documents), 100)
        self.assertEqual(result.warnings, ["retrieval_page_failed_candidates_retained:relevance"])
        degraded = [event for event in observer.events if event.event_type == "retrieval.degraded"]
        self.assertEqual(degraded[-1].payload, {"code": "retrieval_page_failed_candidates_retained", "lane": "relevance", "page": 2, "retained_candidates": 100, "error_type": "RuntimeError"})
        self.assertNotIn("sensitive upstream detail", str(observer.events))
        lane_checkpoint = next(checkpoint for checkpoint in observer.checkpoints if checkpoint.step_key == "retrieve.lane")
        self.assertEqual(lane_checkpoint.state["completed_lanes"], [RetrievalLane.RELEVANCE])
        self.assertEqual(len(lane_checkpoint.state["lane_results"][RetrievalLane.RELEVANCE]), 100)

    async def test_optional_lane_failure_degrades_but_primary_failure_is_fatal(self) -> None:
        base = record("base")
        optional_source = FixtureSource(
            {
                (RetrievalLane.RELEVANCE, 1): source_result([base], limit=100),
                (RetrievalLane.RECENT, 1): RuntimeError("do not expose"),
            },
            capabilities=SourceCapabilities(
                source="fixture",
                lanes=(RetrievalLane.RELEVANCE, RetrievalLane.RECENT),
                max_page_size=100,
                lane_concurrency=2,
            ),
        )
        observer = RecordingObserver()
        result = await RetrievalPipeline(optional_source, observer).run(
            RetrievalRequest(question="question", query="query", lane_limit=100, pool_max=100)
        )
        self.assertEqual([document.uid for document in result.documents], ["base"])
        self.assertEqual(result.warnings, ["retrieval_lane_failed:recent"])
        self.assertNotIn("do not expose", str(observer.events))

        fatal_source = FixtureSource(
            {(RetrievalLane.RELEVANCE, 1): RuntimeError("private source failure")},
            capabilities=SourceCapabilities(source="fixture", lanes=(RetrievalLane.RELEVANCE,)),
        )
        fatal_observer = RecordingObserver()
        with self.assertRaisesRegex(RetrievalSourceError, "primary retrieval lane failed"):
            await RetrievalPipeline(fatal_source, fatal_observer).run(
                RetrievalRequest(question="question", query="query")
            )
        self.assertEqual(fatal_observer.events[-1].event_type, "retrieval.failed")
        self.assertNotIn("private source failure", str(fatal_observer.events))

    async def test_resume_skips_completed_lanes_and_full_candidate_pool_is_recoverable(self) -> None:
        resumed = record("resumed")
        recent = record("recent")
        source = FixtureSource(
            {(RetrievalLane.RECENT, 1): source_result([recent], limit=100)},
            capabilities=SourceCapabilities(
                source="fixture",
                lanes=(RetrievalLane.RELEVANCE, RetrievalLane.RECENT),
                max_page_size=100,
            ),
        )
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer).run(
            RetrievalRequest(
                question="question",
                query="query",
                resume_lane_results={RetrievalLane.RELEVANCE: [resumed]},
                completed_lanes=[RetrievalLane.RELEVANCE],
            )
        )

        self.assertEqual([call.lane for call in source.calls], [RetrievalLane.RECENT])
        self.assertEqual(result.completed_lanes, [RetrievalLane.RELEVANCE, RetrievalLane.RECENT])
        final_checkpoint = observer.checkpoints[-1]
        self.assertEqual(final_checkpoint.state["resume_from_stage"], "post_retrieval")
        self.assertEqual([item["uid"] for item in final_checkpoint.state["candidate_pool"]], [document.uid for document in result.documents])
        self.assertEqual(final_checkpoint.state["provenance_by_identity"], result.provenance_by_identity)

    async def test_dedupe_pool_cap_embedding_and_rerank_ports_preserve_explicit_fallbacks(self) -> None:
        first = record("first", "First", doi="10.1234/shared")
        duplicate = record("duplicate", "Second", doi="https://doi.org/10.1234/shared")
        third = record("third", "Third")
        overflow = record("overflow", "Overflow")
        source = FixtureSource(
            {
                (RetrievalLane.RELEVANCE, 1): source_result([first, third, overflow], limit=100),
                (RetrievalLane.IMPACT, 1): source_result([duplicate, overflow], limit=100),
            },
            capabilities=SourceCapabilities(
                source="fixture",
                lanes=(RetrievalLane.RELEVANCE, RetrievalLane.IMPACT),
                max_page_size=100,
                lane_concurrency=2,
            ),
        )
        embedding = FixtureEmbedding([0.1, 0.9, 0.0])
        reranker = FixtureReranker(
            RetrievalRerankResult(
                items=[
                    RetrievalRerankItem(document=third, score=0.95),
                    RetrievalRerankItem(document=first, score=0.2),
                ]
            )
        )
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer, embedding=embedding).run(
            RetrievalRequest(question="Third", query="query", pool_max=2)
        )

        selection = await CandidateSelectionPipeline(observer, embedding=embedding, reranker=reranker).run(
            question="Third", candidates=result.documents, provenance=result.provenance_by_identity,
            embedding_state=result.embedding_state, pool_max=2, ranking_limit=256,
        )
        self.assertIsInstance(embedding, EmbeddingPort)
        self.assertIsInstance(reranker, RerankPort)
        self.assertEqual(result.total_candidates, 3)
        self.assertEqual([document.uid for document in selection.documents], ["third", "first"])
        self.assertEqual(selection.provenance_by_identity[document_key(third)]["rerank_score"], 0.95)
        self.assertEqual(selection.provenance_by_identity[document_key(first)]["rerank_score"], 0.2)
        self.assertEqual(result.provenance_by_identity[document_key(first)]["origins"], ["retrieval"])
        self.assertEqual(result.provenance_by_identity[document_key(first)]["sources"], ["fixture"])
        self.assertEqual(result.warnings, [])

        fallback_observer = RecordingObserver()
        fallback = await RetrievalPipeline(
            source,
            fallback_observer,
            embedding=FixtureEmbedding(RuntimeError("embedding secret")),
        ).run(RetrievalRequest(question="Third", query="query", pool_max=2))
        selection = await CandidateSelectionPipeline(
            fallback_observer, reranker=FixtureReranker(RuntimeError("rerank secret")),
        ).run(question="Third", candidates=fallback.documents, provenance=fallback.provenance_by_identity,
              embedding_state=None, pool_max=2, ranking_limit=256)
        self.assertEqual(
            [*fallback.warnings, *selection.warnings],
            [
                "retrieval_embedding_failed_local_similarity_retained",
                "retrieval_rerank_failed_rrf_order_retained",
            ],
        )
        self.assertNotIn("embedding secret", str(fallback_observer.events))
        self.assertNotIn("rerank secret", str(fallback_observer.events))


if __name__ == "__main__":
    unittest.main()
