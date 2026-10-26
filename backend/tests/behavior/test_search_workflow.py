from __future__ import annotations

import json
import unittest
from pathlib import Path

from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.models import (
    PaperCitation,
    PaperIdentifiers,
    PaperKeywords,
    PaperLinks,
    PaperNames,
    PaperRecord,
    PaperSource,
    QueryLoopBudget,
    RankedPaper,
    ResearchAssemblyRequest,
    ResearchResult,
    ResearchWorkflowRequest,
    RetrievalEmbeddingResult,
    RetrievalRerankItem,
    RetrievalRerankResult,
    SearchPlan,
)
from paperseek_core.sources.contracts import (
    SourceCapabilities,
    SourceSearchMetadata,
    SourceSearchResult,
)
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openalex import OpenAlexSource
from paperseek_core.workflow import ResearchWorkflow

ROOT = Path(__file__).resolve().parents[3]
M0 = ROOT / "tests" / "fixtures" / "golden_search"


class RecordingObserver:
    def __init__(self) -> None:
        self.events = []
        self.checkpoints = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_checkpoint(self, checkpoint) -> None:
        self.checkpoints.append(checkpoint)




def record(uid: str, doi: str, abstract: str) -> PaperRecord:
    return PaperRecord(
        uid=uid,
        title=uid,
        identifiers=PaperIdentifiers(doi=doi),
        abstract=abstract,
        provider="fixture",
    )




def _paper_from_expected(item: dict) -> RankedPaper:
    document = PaperRecord(
        uid=item["uid"],
        title=item["title"],
        types=item["document_types"],
        source_types=item["document_types"],
        source=PaperSource(source_title=item["source"], publish_year=item["publish_year"]),
        names=PaperNames(
            authors=[{"display_name": name} for name in item["authors"]]
        ),
        links=PaperLinks.model_validate(item["links"]),
        citations=[PaperCitation(db="OpenAlex", count=item["citation_count"])],
        identifiers=PaperIdentifiers(doi=item["doi"], openalex=item["uid"]),
        keywords=PaperKeywords(author_keywords=item["keywords"].split("; ")),
        abstract=item["abstract"],
        provider=item["provider"],
    )
    return RankedPaper(
        document=document,
        score=item["score"],
        reasoning=item["reasoning"],
        provenance={
            "source_rank": item["source_rank"],
            "retrieval_score": item["retrieval_score"],
            "retrieval_lanes": item["retrieval_lanes"],
            "origins": ["retrieval"],
        },
    )


class M2FinalAssemblyContractTest(unittest.TestCase):
    def test_final_graph_marks_results_and_preserves_null_scores_and_edges(self) -> None:
        graph = {"status": "complete", "nodes": [{"id": "scored", "roles": ["seed"]}],
                 "edges": [{"source": "scored", "target": "unscored", "type": "references"}]}
        request = ResearchAssemblyRequest(
            question="graph", source="openalex", search_plan=SearchPlan(intent="graph"), final_query="graph",
            citation_map=graph,
            items=[RankedPaper(document=record("scored", "", ""), score=0, reasoning="valid zero"),
                   RankedPaper(document=record("unscored", "", ""), score=None)],
        )
        result = FinalResultAssembler().assemble(request)
        first, second = result.citation_map["nodes"]
        self.assertEqual(first["roles"], ["seed", "result"])
        self.assertEqual((first["rank"], first["score"], first["reasoning"]), (1, 0, "valid zero"))
        self.assertEqual((second["rank"], second["score"]), (2, None))
        self.assertEqual(result.citation_map["edges"], graph["edges"])
        self.assertEqual(graph["nodes"][0]["roles"], ["seed"])
        artifact = next(item for item in result.artifacts if item.kind == "citation_map")
        self.assertEqual(json.loads(artifact.content), result.citation_map)

    def test_imported_history_preserves_explicit_search_field_in_both_json_artifacts(self) -> None:
        result = ResearchResult(question="Historical question", search_intent="", search_plan={},
            source="wos", final_query="TS=(synthetic)", db="WOS", field="TS")
        artifacts = {item.kind: json.loads(item.content) for item in FinalResultAssembler().artifacts_for(result)
                     if item.kind in {"final_result", "export_json"}}
        self.assertEqual(artifacts["final_result"]["field"], "TS")
        self.assertEqual(artifacts["export_json"]["field"], "TS")

    def test_m0_ranked_user_fields_and_export_artifacts_are_exact(self) -> None:
        expected = json.loads((M0 / "final_result.json").read_text(encoding="utf-8"))
        expected_export = json.loads((M0 / "export.json").read_text(encoding="utf-8"))
        request = ResearchAssemblyRequest(
            question=expected["question"],
            source=expected["source"],
            search_plan=SearchPlan.model_validate(expected["search_plan"]),
            final_query=expected["final_query"],
            total=expected["total"],
            iterations=expected["iterations"],
            history=expected["history"],
            query_view_selection=expected["query_view_selection"],
            query_portfolio=expected["query_portfolio"],
            query_iteration_memory=expected_export["query_iteration_memory"],
            citation_map=expected["citation_map"],
            ranking_steps=expected["ranking_steps"],
            search_state=expected["search_state"],
            items=[_paper_from_expected(item) for item in expected["ranked"]],
        )

        result = FinalResultAssembler().assemble(request)
        self.assertEqual(FinalResultAssembler().artifacts_for(result), result.artifacts)

        self.assertEqual(
            result.public_payload(),
            expected,
        )
        artifacts = {artifact.filename: artifact for artifact in result.artifacts}
        self.assertEqual(
            set(artifacts),
            {"final_result.json", "export.json", "export.csv", "export.bib", "export.ris", "candidate_pool.jsonl",
             "citation_map.json"},
        )
        self.assertEqual(artifacts["export.csv"].content, (M0 / "export.csv").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(artifacts["export.json"].content), json.loads((M0 / "export.json").read_text(encoding="utf-8")))
        self.assertEqual(artifacts["candidate_pool.jsonl"].content, (M0 / "candidate_pool.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(artifacts["citation_map.json"].content), expected["citation_map"])


class FixtureWorkflowLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
        del messages, temperature, max_tokens
        if not self.responses:
            raise AssertionError("workflow LLM fixture was exhausted")
        return self.responses.pop(0)


class FixtureWorkflowSource:
    name = "openalex"
    capabilities = SourceCapabilities(source=name, lanes=("relevance",), supports_abstracts=True)

    def __init__(self, records: list[PaperRecord]) -> None:
        self.records = records

    async def search(self, request) -> SourceSearchResult:
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=len(self.records), page=request.page, limit=request.limit),
            hits=self.records if request.page == 1 else [],
        )




class RecordingEmbedding:
    def __init__(self) -> None:
        self.calls = 0

    async def embedding_scores(self, question: str, documents: list[PaperRecord], *, model=None) -> RetrievalEmbeddingResult:
        del question
        self.calls += 1
        return RetrievalEmbeddingResult(scores=[0.5 for _ in documents], model="fixture", provider="fixture")


class RecordingReranker:
    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, question: str, documents: list[PaperRecord]) -> RetrievalRerankResult:
        del question
        self.calls += 1
        return RetrievalRerankResult(
            items=[RetrievalRerankItem(document=document, score=1.0) for document in documents]
        )


class M2ResearchWorkflowContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_single_workflow_uses_external_embedding_and_reranking_once(self) -> None:
        source_payload = json.loads((M0 / "source_responses.json").read_text(encoding="utf-8"))
        mapper = OpenAlexSource(transport=AsyncHttpTransport())
        records = [mapper._to_record(item) for item in source_payload["relevance"]]
        llm = FixtureWorkflowLLM(
            [
                (ROOT / "tests" / "fixtures" / "llm" / "intent_search_plan.json").read_text(encoding="utf-8"),
                (M0 / "llm_query.json").read_text(encoding="utf-8"),
                (M0 / "llm_ranking_scores.json").read_text(encoding="utf-8"),
                (M0 / "llm_ranking_reasons.json").read_text(encoding="utf-8"),
            ]
        )
        embedding = RecordingEmbedding()
        reranker = RecordingReranker()
        workflow = ResearchWorkflow(
            llm=llm,
            source=FixtureWorkflowSource(records),
            observer=RecordingObserver(),
            embedding=embedding,
            reranker=reranker,
        )

        await workflow.run(
            ResearchWorkflowRequest(
                question="How do transparent AI decision systems affect accountability in public agencies?",
                query_budget=QueryLoopBudget(
                    target_min=1,
                    target_max=5,
                    retrieval_pool_min=1,
                    retrieval_pool_max=3000,
                    max_iterations=5,
                    query_portfolio_enabled=False,
                ),
                citation_enabled=False,
            )
        )

        self.assertEqual(embedding.calls, 1)
        self.assertEqual(reranker.calls, 1)

    async def test_single_workflow_composes_every_core_stage_and_checkpoints_final_artifacts(self) -> None:
        source_payload = json.loads((M0 / "source_responses.json").read_text(encoding="utf-8"))
        mapper = OpenAlexSource(transport=AsyncHttpTransport())
        records = [mapper._to_record(item) for item in source_payload["relevance"]]
        llm = FixtureWorkflowLLM(
            [
                (ROOT / "tests" / "fixtures" / "llm" / "intent_search_plan.json").read_text(encoding="utf-8"),
                (M0 / "llm_query.json").read_text(encoding="utf-8"),
                (M0 / "llm_ranking_scores.json").read_text(encoding="utf-8"),
                (M0 / "llm_ranking_reasons.json").read_text(encoding="utf-8"),
            ]
        )
        observer = RecordingObserver()
        workflow = ResearchWorkflow(
            llm=llm,
            source=FixtureWorkflowSource(records),
            observer=observer,
        )

        result = await workflow.run(
            ResearchWorkflowRequest(
                question="How do transparent AI decision systems affect accountability in public agencies?",
                query_budget=QueryLoopBudget(
                    target_min=1,
                    target_max=5,
                    retrieval_pool_min=1,
                    retrieval_pool_max=3000,
                    max_iterations=5,
                    query_portfolio_enabled=False,
                ),
                citation_enabled=True,
            )
        )

        self.assertEqual([item.uid for item in result.ranked], [record.uid for record in records])
        self.assertEqual([item.abstract for item in result.ranked], [record.abstract for record in records])
        self.assertEqual(result.warnings, [])
        stages = [event.stage for event in observer.events]
        for stage in ("workflow", "query", "retrieval", "citation", "ranking"):
            self.assertIn(stage, stages)
        self.assertEqual(observer.checkpoints[-1].step_key, "assemble_final_result")
        self.assertEqual(
            {item["filename"] for item in observer.checkpoints[-1].state["artifacts"]},
            {"final_result.json", "export.json", "export.csv", "export.bib", "export.ris", "candidate_pool.jsonl",
             "citation_map.json", "bibliometric_corpus.json"},
        )
        self.assertEqual(observer.events[-1].event_type, "workflow.completed")


if __name__ == "__main__":
    unittest.main()
