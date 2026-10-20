from __future__ import annotations

import asyncio
import json
import math
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from paperseek_core.citation import (
    CitationPipeline,
    build_seed_plans,
    select_seed_candidates,
)
from paperseek_core.document_identity import document_key
from paperseek_core.models import (
    CandidateSelectionResult,
    CitationEdge,
    CitationExpansionRequest,
    CitationExpansionResult,
    CitationSeedPlan,
    CitationSourceRequest,
    CitationSourceResult,
    PaperCitation,
    PaperIdentifiers,
    PaperRecord,
    PaperSource,
    QueryLoopBudget,
    QueryLoopResult,
    ResearchWorkflowRequest,
    ResearchWorkflowResumeState,
    RetrievalRequest,
    RetrievalResult,
    SearchPlan,
)
from paperseek_core.ports import CitationPort
from paperseek_core.retrieval_fusion import (
    fuse_candidates_rrf,
    fuse_citation_candidates,
)
from paperseek_core.sources.contracts import SourceCapabilities, SourceError
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openalex import OpenAlexSource
from paperseek_core.workflow import ResearchWorkflow

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_ROOT = ROOT / "tests" / "fixtures" / "golden_search"


def read_json(name: str) -> dict:
    return json.loads((GOLDEN_ROOT / name).read_text(encoding="utf-8"))


def record(
    uid: str,
    *,
    title: str = "",
    citations: int = 0,
    year: int | None = None,
    references: tuple[str, ...] = (),
) -> PaperRecord:
    return PaperRecord(
        uid=uid,
        title=title or uid,
        source=PaperSource(publish_year=year),
        citations=[PaperCitation(db="OpenAlex", count=citations)] if citations else [],
        identifiers=PaperIdentifiers(openalex=uid),
        provider="openalex",
        raw={"referenced_works": list(references)},
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.events = []
        self.checkpoints = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_checkpoint(self, checkpoint) -> None:
        self.checkpoints.append(checkpoint)


class FixtureCitationSource:
    name = "openalex"
    capabilities = SourceCapabilities(
        source="openalex",
        supports_citations=True,
        supports_citation_expansion=True,
    )

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[CitationSourceRequest] = []

    async def traverse_citations(self, request: CitationSourceRequest) -> CitationSourceResult:
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class UnsupportedSource:
    name = "crossref"
    capabilities = SourceCapabilities(source="crossref")


def fixture_source_result() -> CitationSourceResult:
    payload = read_json("source_responses.json")["citations"]
    mapper = OpenAlexSource(transport=AsyncHttpTransport())
    return CitationSourceResult(
        records=[mapper._to_record(work) for work in payload["records"]],
        nodes=payload["nodes"],
        edges=[CitationEdge.from_citation_map(edge) for edge in payload["edges"]],
        frontier=payload["frontier"],
    )


class M2CitationExpansionContractTest(unittest.IsolatedAsyncioTestCase):
    def test_seed_pool_preserves_successor_view_quota_and_fused_records(self) -> None:
        candidates = [record(f"W{i}") for i in range(20)]
        selected = select_seed_candidates(
            candidates,
            {"direct": candidates[8:16], "successor": [record("outside"), *candidates[16:]]},
            {"successor": "successor_paradigm", "direct": "direct_mechanism"},
            limit=8,
        )
        self.assertEqual([item.uid for item in selected], ["W16", "W17", "W18", "W19", "W8", "W9", "W0", "W1"])
        self.assertIs(selected[0], candidates[16])

    async def test_expanded_paper_reaches_full_ranking_pool_and_survives_resume(self) -> None:
        question = "durable research recovery"
        base = [record(f"W{index}", title=f"Unrelated candidate {index}") for index in range(256)]
        neighbor = record("Wnew", title=question)
        initial = fuse_candidates_rrf(question, {"relevance": base})
        retrieval = RetrievalResult(
            documents=initial.documents,
            provenance_by_identity={
                key: {**metadata, "origins": ["retrieval"], "rerank_score": 0.75}
                for key, metadata in initial.metadata_by_key.items()
            },
        )
        query = QueryLoopResult(
            question=question, source="openalex", search_plan=SearchPlan(intent=question),
            final_query=question, total=256,
            retrieval_request=RetrievalRequest(question=question, query=question),
        )
        source = FixtureCitationSource([CitationSourceResult(
            records=[neighbor],
            edges=[CitationEdge(seed_id="W0", parent_id="W0", record_id="Wnew", direction="backward", depth=1)],
        )])
        observer = RecordingObserver()
        llm = AsyncMock()
        llm.chat_json.return_value = json.dumps({
            "scores": [[index, 10 - index / 256] for index in range(1, 257)],
            "reasons": [[index, "Fixture relevance"] for index in range(1, 257)],
        })
        request = ResearchWorkflowRequest(
            question=question,
            query_budget=QueryLoopBudget(retrieval_pool_max=256, ranking_candidate_limit=256),
        )
        with (
            patch("paperseek_core.workflow.research.QueryLoopPipeline.run", AsyncMock(return_value=query)),
            patch("paperseek_core.workflow.research.RetrievalPipeline.run", AsyncMock(return_value=retrieval)) as retrieve,
        ):
            result = await ResearchWorkflow(llm=llm, source=source, observer=observer).run(request)
            self.assertEqual(len(result.ranked), 256)
            self.assertIn(neighbor.uid, [item.uid for item in result.ranked])
            self.assertEqual(result.citation_map["added_candidates"], 1)
            expanded = result.provenance_by_identity[neighbor.uid]
            self.assertEqual(expanded["origins"], ["citation_expansion"])
            self.assertEqual(expanded["retrieval_source_ranks"]["citation_expansion"], 1)
            self.assertEqual(expanded["citation_paths"][0]["parent_id"], "W0")
            original = next(item for item in result.ranked if item.uid != neighbor.uid)
            self.assertEqual(result.provenance_by_identity[original.uid]["rerank_score"], 0.75)

            state = next(cp.state for cp in observer.checkpoints if cp.step_key == "expand_citations")
            citation = CitationExpansionResult.model_validate({
                key: state[key] for key in ("candidates", "citation_map", "provenance_by_identity", "warnings")
            })
            ranking = [cp.state for cp in observer.checkpoints if cp.step_key == "rank_results.batch"][-1]
            selection = dict(next(cp.state for cp in observer.checkpoints if cp.step_key == "select_candidates"))
            selection.pop("resume_from_stage")
            calls_before_resume = llm.chat_json.await_count
            resumed = await ResearchWorkflow(llm=llm, source=source, observer=RecordingObserver()).run(
                request.model_copy(update={"resume": ResearchWorkflowResumeState(
                    retrieval=retrieval, citation=citation,
                    selection=CandidateSelectionResult.model_validate(selection),
                    ranking_scores=ranking["ranking_scores"],
                    ranking_completed_batch_ids=ranking["completed_batch_ids"],
                )})
            )
            self.assertEqual([item.uid for item in resumed.ranked], [item.uid for item in result.ranked])
            self.assertEqual(len(source.calls), 1)
            self.assertEqual(retrieve.await_count, 1)
            self.assertEqual(llm.chat_json.await_count, calls_before_resume)

    def test_no_citation_candidates_preserves_external_reranker_order(self) -> None:
        documents = [record("W2"), record("W1")]
        provenance = {document_key(item): {"origins": ["retrieval"], "rerank_score": 0.5} for item in documents}
        result = fuse_citation_candidates("query", documents, provenance, pool_max=256)
        self.assertEqual(result.documents, documents)
        self.assertEqual(result.metadata_by_key, provenance)

    def test_citation_edge_preserves_seed_parent_direction_depth_and_map_shape(self) -> None:
        backward = CitationEdge(
            seed_id="Wseed",
            parent_id="Wparent",
            record_id="Wreference",
            direction="backward",
            depth=2,
        )
        forward = CitationEdge(
            seed_id="Wseed",
            parent_id="Wparent",
            record_id="Wciting",
            direction="forward",
            depth=1,
        )

        self.assertEqual(
            backward.to_citation_map(),
            {"source": "Wparent", "target": "Wreference", "type": "references", "seed": "Wseed", "layer": "2"},
        )
        self.assertEqual(
            forward.to_citation_map(),
            {"source": "Wciting", "target": "Wparent", "type": "cites", "seed": "Wseed", "layer": "1"},
        )
        self.assertEqual(CitationEdge.from_citation_map(backward.to_citation_map()), backward)
        self.assertEqual(CitationEdge.from_citation_map(forward.to_citation_map()), forward)

    def test_seed_roles_quotas_directions_and_interleaving_match_characterization(self) -> None:
        candidates = [
            record(f"W{index}", citations=citations, year=year)
            for index, (citations, year) in enumerate(
                ((2, 2020), (50, 2016), (4, 2024), (25, 2021), (1, 2025), (12, 2019)),
                start=1,
            )
        ]
        ranked = [(document, score) for document, score in zip(candidates, (10, 9, 8, 6, 5, 4))]

        plans = build_seed_plans(ranked, candidates, seed_limit=6, depth=2)

        self.assertEqual(len(plans), 6)
        roles = {plan.record.uid: plan.role for plan in plans}
        self.assertIn("relevance", roles["W1"])
        self.assertIn("impact", roles["W2"])
        self.assertIn("recent", roles["W5"])
        first_roles = {role for plan in plans[:3] for role in plan.role.split("+")}
        self.assertTrue({"relevance", "impact", "recent"}.issubset(first_roles))
        self.assertEqual({plan.depth for plan in plans}, {2})
        for plan in plans:
            if "relevance" in plan.role:
                self.assertEqual(set(plan.directions), {"backward", "forward"})

    async def test_m0_graph_map_dedupe_provenance_and_checkpoint_are_preserved(self) -> None:
        source_payload = read_json("source_responses.json")
        mapper = OpenAlexSource(transport=AsyncHttpTransport())
        candidates = [mapper._to_record(work) for work in source_payload["relevance"]]
        source_result = fixture_source_result()
        source = FixtureCitationSource([source_result])
        observer = RecordingObserver()

        result = await CitationPipeline(source, observer).run(
            CitationExpansionRequest(
                candidates=candidates,
                seed_candidates=candidates,
                seed_count=3,
                per_seed=4,
                max_records=40,
                depth=2,
            )
        )

        self.assertIsInstance(source, CitationPort)
        self.assertEqual([item.uid for item in result.candidates], [f"https://openalex.org/W{index}" for index in range(1001, 1006)])
        expected = source_payload["citations"]
        self.assertEqual(result.citation_map["nodes"], expected["nodes"])
        self.assertEqual(result.citation_map["edges"], expected["edges"])
        self.assertEqual(result.citation_map["seed_plan_counts"], {"relevance": 1, "impact": 1, "recent": 1})
        provenance = result.provenance_by_identity["https://openalex.org/W1004"]
        self.assertEqual(
            provenance["citation_paths"],
            [{"seed_id": "https://openalex.org/W1001", "parent_id": "https://openalex.org/W1001", "direction": "backward", "depth": 1}],
        )
        checkpoint = observer.checkpoints[-1]
        self.assertEqual(checkpoint.step_key, "expand_citations")
        self.assertEqual(checkpoint.workflow_version, "m2.citation.v1")
        self.assertEqual(checkpoint.state["citation_frontier"], expected["frontier"])
        self.assertEqual(checkpoint.state["citation_map"], result.citation_map)
        self.assertEqual([event.event_type for event in observer.events], ["citation.started", "citation.progress", "citation.completed"])

    async def test_duplicate_neighbor_is_not_added_but_keeps_graph_edge(self) -> None:
        seed = record("Wseed")
        duplicate = record("Wseed", title="duplicate")
        neighbor = record("Wnew")
        source_result = CitationSourceResult(
            records=[duplicate, neighbor],
            edges=[CitationEdge(seed_id="Wseed", parent_id="Wseed", record_id="Wnew", direction="backward", depth=1)],
            frontier={"stop_reason": "completed"},
        )
        result = await CitationPipeline(FixtureCitationSource([source_result]), RecordingObserver()).run(
            CitationExpansionRequest(candidates=[seed], seed_count=1, per_seed=2, max_records=10, depth=1)
        )

        self.assertEqual([item.uid for item in result.candidates], ["Wseed", "Wnew"])
        self.assertEqual(result.citation_map["added_candidates"], 1)
        self.assertEqual(len(result.citation_map["edges"]), 1)

    async def test_transient_failure_retries_once_with_reduced_budget(self) -> None:
        seed = record("Wseed")
        recovered = CitationSourceResult(records=[record("Wnew")], frontier={"stop_reason": "completed"})
        source = FixtureCitationSource([SourceError("openalex", "temporary", status=503), recovered])
        observer = RecordingObserver()

        result = await CitationPipeline(source, observer).run(
            CitationExpansionRequest(candidates=[seed], seed_count=1, per_seed=4, max_records=20, depth=2)
        )

        self.assertEqual([(call.per_seed, call.max_records, call.depth) for call in source.calls], [(4, 20, 2), (2, 10, 1)])
        self.assertEqual([item.uid for item in result.candidates], ["Wseed", "Wnew"])
        self.assertEqual(result.citation_map["citation_depth"], 1)
        event_types = [event.event_type for event in observer.events]
        self.assertIn("citation.retrying", event_types)
        self.assertNotIn("citation.degraded", event_types)

    async def test_persistent_failure_keeps_base_candidates_and_is_visibly_degraded(self) -> None:
        seed = record("Wseed")
        source = FixtureCitationSource(
            [SourceError("openalex", "first", status=503), SourceError("openalex", "second", status=503)]
        )
        observer = RecordingObserver()

        result = await CitationPipeline(source, observer).run(
            CitationExpansionRequest(candidates=[seed], seed_count=1, per_seed=4, max_records=20, depth=2)
        )

        self.assertEqual([item.uid for item in result.candidates], ["Wseed"])
        self.assertEqual(result.citation_map["status"], "error")
        self.assertEqual(result.warnings, ["citation_expansion_failed_base_candidates_retained"])
        degraded = [event for event in observer.events if event.event_type == "citation.degraded"]
        self.assertEqual(degraded[-1].payload["code"], "citation_expansion_failed_base_candidates_retained")
        self.assertNotIn("second", json.dumps(result.citation_map))
        self.assertEqual(observer.checkpoints, [])

    async def test_unsupported_and_prior_rate_limit_skips_are_visible(self) -> None:
        seed = record("Wseed")
        unsupported_observer = RecordingObserver()
        unsupported = await CitationPipeline(UnsupportedSource(), unsupported_observer).run(
            CitationExpansionRequest(candidates=[seed])
        )
        self.assertEqual(unsupported.citation_map["status"], "unsupported")
        self.assertEqual([event.event_type for event in unsupported_observer.events], ["citation.skipped"])

        limited_observer = RecordingObserver()
        limited_source = FixtureCitationSource([fixture_source_result()])
        limited = await CitationPipeline(limited_source, limited_observer).run(
            CitationExpansionRequest(candidates=[seed], source_rate_limited=True)
        )
        self.assertEqual(limited.citation_map["status"], "rate_limited")
        self.assertEqual(limited.warnings, ["citation_expansion_skipped_rate_limited"])
        self.assertEqual(limited_source.calls, [])
        self.assertEqual(limited_observer.events[-1].event_type, "citation.degraded")


class FakeAsyncClient:
    def __init__(self, route) -> None:
        self.route = route
        self.calls = []

    async def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        request = httpx.Request(method, url, params=params, headers=headers)
        self.calls.append({"method": method, "url": url, "params": dict(params or {}), "timeout": timeout})
        payload = self.route(dict(params or {}))
        return httpx.Response(200, json=payload, request=request)


class M2OpenAlexCitationPortContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_openalex_port_maps_backward_forward_parent_depth_and_frontier(self) -> None:
        seed = record("https://openalex.org/Wseed", references=("https://openalex.org/Wback",))

        def route(params: dict) -> dict:
            query_filter = str(params.get("filter") or "")
            if query_filter.startswith("openalex:"):
                return {"meta": {"count": 1}, "results": [{"id": "https://openalex.org/Wback", "title": "Backward"}]}
            if query_filter.startswith("cites:Wseed"):
                return {"meta": {"count": 1}, "results": [{"id": "https://openalex.org/Wforward", "title": "Forward"}]}
            raise AssertionError(f"unexpected OpenAlex filter: {query_filter}")

        client = FakeAsyncClient(route)
        source = OpenAlexSource(transport=AsyncHttpTransport(client=client, sleep=lambda _: asyncio.sleep(0)))
        result = await source.traverse_citations(
            CitationSourceRequest(
                seed_plans=[CitationSeedPlan(record=seed, directions=("backward", "forward"), depth=1)],
                per_seed=10,
                max_records=10,
                depth=1,
            )
        )

        self.assertIsInstance(source, CitationPort)
        self.assertEqual({item.uid for item in result.records}, {"https://openalex.org/Wback", "https://openalex.org/Wforward"})
        self.assertEqual(
            [edge.to_citation_map() for edge in result.edges],
            [
                {"source": "https://openalex.org/Wseed", "target": "https://openalex.org/Wback", "type": "references", "seed": "https://openalex.org/Wseed", "layer": "1"},
                {"source": "https://openalex.org/Wforward", "target": "https://openalex.org/Wseed", "type": "cites", "seed": "https://openalex.org/Wseed", "layer": "1"},
            ],
        )
        self.assertEqual(result.frontier["direction_stats"]["backward"]["budget"], math.ceil(10 * 0.7))
        self.assertEqual(result.frontier["direction_stats"]["backward"]["added"], 1)
        self.assertEqual(result.frontier["direction_stats"]["forward"]["added"], 1)
        self.assertEqual(result.frontier["request_count"], 2)
        self.assertEqual(client.calls[0]["params"]["filter"], "openalex:Wback")
        self.assertEqual(client.calls[1]["params"]["filter"], "cites:Wseed")


if __name__ == "__main__":
    unittest.main()
