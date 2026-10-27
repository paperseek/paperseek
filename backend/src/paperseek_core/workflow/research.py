"""The single M2 research workflow over explicit core ports."""

from __future__ import annotations

import asyncio

import hashlib
import json
from typing import Any

from paperseek_core.citation import CitationPipeline, select_seed_candidates
from paperseek_core.bibliometrics.corpus import merge_corpus
from paperseek_core.execution import NEVER_CANCELLED, record_checkpoint
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.models import (
    CheckpointDraft,
    CitationExpansionRequest,
    CoreProgressEvent,
    QueryLoopRequest,
    RankingRequest,
    ResearchAssemblyRequest,
    ResearchResult,
    ResearchWorkflowRequest,
    ResearchWorkflowResumeState,
)
from paperseek_core.ports import (
    CancellationToken,
    EmbeddingPort,
    DecisionPort,
    EnrichmentPort,
    LLMPort,
    RerankPort,
    SourcePort,
    WorkflowObserver,
)
from paperseek_core.query_loop import QueryLoopPipeline
from paperseek_core.document_identity import document_key
from paperseek_core.ranking import RankingPipeline
from paperseek_core.candidate_selection import CandidateSelectionPipeline
from paperseek_core.retrieval import RetrievalPipeline


class ResearchWorkflow:
    def __init__(
        self,
        *,
        llm: LLMPort,
        source: SourcePort,
        observer: WorkflowObserver,
        embedding: EmbeddingPort | None = None,
        reranker: RerankPort | None = None,
        decision: DecisionPort | None = None,
        enrichment: EnrichmentPort | None = None,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> None:
        self.llm = llm
        self.source = source
        self.observer = observer
        self.embedding = embedding
        self.reranker = reranker
        self.decision = decision
        self.enrichment = enrichment
        self.cancellation = cancellation

    async def run(self, request: ResearchWorkflowRequest) -> ResearchResult:
        resume = request.resume or ResearchWorkflowResumeState()
        self._event(
            "workflow.started",
            {"source": self.source.name, "question_length": len(request.question)},
        )
        recovery = {
            "query": int(resume.query is not None),
            "retrieval": int(
                resume.retrieval is not None
                or bool(resume.retrieval_completed_lanes)
                or bool(resume.retrieval_lane_results)
            ),
            "citation": int(resume.citation is not None),
            "selection": int(resume.selection is not None),
            "ranking": int(bool(resume.ranking_scores) or bool(resume.ranking_completed_batch_ids)),
        }
        if any(recovery.values()):
            self._event("workflow.recovery", recovery)
        query = await QueryLoopPipeline(
            self.llm,
            self.source,
            self.observer,
            cancellation=self.cancellation,
        ).run(
            request=self._query_request(request, resume)
        )
        self.cancellation.raise_if_cancelled()
        if resume.retrieval is not None:
            retrieval = resume.retrieval
        else:
            retrieval_request = query.retrieval_request.model_copy(
                update={
                    "resume_lane_results": resume.retrieval_lane_results,
                    "completed_lanes": resume.retrieval_completed_lanes,
                }
            )
            retrieval = await RetrievalPipeline(
                self.source,
                self.observer,
                embedding=self.embedding,
                cancellation=self.cancellation,
            ).run(retrieval_request)
        self.cancellation.raise_if_cancelled()
        if resume.citation is not None:
            citation = resume.citation
        else:
            citation = await CitationPipeline(
                self.source,
                self.observer,
                cancellation=self.cancellation,
            ).run(
                CitationExpansionRequest(
                    candidates=retrieval.documents,
                    seed_candidates=select_seed_candidates(
                        retrieval.documents,
                        {lane.removeprefix("query_view:"): records for lane, records in query.retrieval_request.supplemental_lanes.items() if lane.startswith("query_view:")},
                        {view.view_id: view.view_type for view in query.search_plan.query_views},
                        limit=min(request.query_budget.ranking_candidate_limit, max(request.citation_seed_count * 4, 64)),
                    ),
                    enabled=request.citation_enabled,
                    seed_count=request.citation_seed_count,
                    per_seed=request.citation_per_seed,
                    max_records=request.citation_max_records,
                    depth=request.citation_depth,
                    field_ids=request.query_budget.field_ids,
                )
            )
        provenance = self._merge_provenance(
            citation.candidates,
            retrieval.provenance_by_identity,
            citation.provenance_by_identity,
        )
        selection = await CandidateSelectionPipeline(
            self.observer, embedding=self.embedding, reranker=self.reranker, decision=self.decision,
            cancellation=self.cancellation,
        ).run(
            question=request.question, candidates=citation.candidates, provenance=provenance,
            embedding_state=retrieval.embedding_state,
            pool_max=request.query_budget.retrieval_pool_max,
            ranking_limit=request.query_budget.ranking_candidate_limit,
            rrf_k=query.retrieval_request.rrf_k, resume=resume.selection,
            search_plan=query.search_plan.audit_summary(), decision_batches=resume.decision_batches,
        )
        provenance = selection.provenance_by_identity
        self.cancellation.raise_if_cancelled()
        ranking = await RankingPipeline(
            self.llm,
            self.observer,
            cancellation=self.cancellation,
        ).run(
            RankingRequest(
                question=request.question,
                documents=selection.documents[:selection.selected_count],
                plan_context=query.search_plan.audit_summary(),
                provenance_by_identity=provenance,
                batch_size=request.ranking_batch_size,
                concurrency=request.ranking_concurrency,
                resume_scores=resume.ranking_scores,
                completed_batch_ids=resume.ranking_completed_batch_ids,
            )
        )
        warnings = list(
            dict.fromkeys(
                [
                    *query.warnings,
                    *retrieval.warnings,
                    *citation.warnings,
                    *ranking.warnings,
                    *selection.warnings,
                ]
            )
        )
        annotations = await self._annotations(ranking.items)
        self.cancellation.raise_if_cancelled()
        result = await asyncio.to_thread(FinalResultAssembler().assemble,
            ResearchAssemblyRequest(
                bibliometric_corpus=await asyncio.to_thread(
                    merge_corpus, retrieval.bibliometric_corpus, citation.candidates,
                    scored_ids={document_key(item.document) for item in ranking.items if item.score is not None},
                    source_reported_total=query.total,
                    retrieval_pool_limit=request.query_budget.retrieval_pool_max,
                    scoring_limit=request.query_budget.ranking_candidate_limit,
                    warnings=["bounded_retrieval"],
                ) if retrieval.bibliometric_corpus is not None else None,
                question=request.question,
                source=self.source.name,
                search_plan=query.search_plan,
                final_query=query.final_query,
                total=query.total,
                iterations=query.iterations,
                history=query.history,
                query_view_selection=query.query_view_selection,
                query_portfolio=query.query_portfolio,
                query_iteration_memory=[
                    {
                        "iteration": item.iteration,
                        "query": item.query,
                        "total": item.total,
                        "candidate_count": len(item.records),
                    }
                    for item in query.query_iteration_memory
                ],
                citation_map=citation.citation_map,
                ranking_steps=self._ranking_steps(retrieval, citation, ranking, selection),
                search_state=self._search_state(query, selection.documents, provenance),
                items=ranking.items,
                warnings=warnings,
                annotations_by_uid=annotations,
            )
        )
        self.cancellation.raise_if_cancelled()
        await self._final_checkpoint(result)
        self._event(
            "workflow.completed",
            {
                "source": self.source.name,
                "result_count": len(result.ranked),
                "warning_count": len(result.warnings),
                "artifact_count": len(result.artifacts),
            },
        )
        return result

    async def _annotations(self, items) -> dict[str, dict[str, Any]]:
        if self.enrichment is None or not items:
            return {}
        try:
            annotations = await self.enrichment.annotate([item.document for item in items])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Labels are optional; the ranked results stand without them.
            self._event("enrichment.failed", {"error": type(exc).__name__})
            return {}
        self._event("enrichment.completed", {
            "journal_matches": sum(1 for value in annotations.values() if value.get("journal_metrics")),
            "acceptance_matches": sum(1 for value in annotations.values() if value.get("acceptance")),
        })
        return annotations

    @staticmethod
    def _query_request(
        request: ResearchWorkflowRequest,
        resume: ResearchWorkflowResumeState,
    ) -> QueryLoopRequest:
        return QueryLoopRequest(
            question=request.question,
            source_context=request.source_context,
            budget=request.query_budget,
            resume=resume.query,
        )

    @staticmethod
    def _merge_provenance(
        candidates,
        retrieval: dict[str, dict[str, Any]],
        citation: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        output = {key: dict(value) for key, value in retrieval.items()}
        for index, document in enumerate(candidates, 1):
            key = document_key(document)
            current = dict(output.get(key) or output.get(document.uid) or {})
            citation_detail = citation.get(key) or citation.get(document.uid) or {}
            if citation_detail:
                current.update(citation_detail)
                current.setdefault("origins", ["citation_expansion"])
                current.setdefault("retrieval_lanes", ["citation_expansion"])
                current.setdefault("retrieval_score", 0.0)
                current.setdefault("source_rank", index)
            output[key] = current
            output[document.uid] = current
        return output

    @staticmethod
    def _ranking_steps(retrieval, citation, ranking, selection) -> list[dict[str, Any]]:
        return [
            {
                "id": "multi_lane_retrieval",
                "title": "Multi-lane retrieval",
                "status": "complete",
                "current": len(retrieval.documents),
                "total": len(retrieval.documents),
                "candidate_count": len(retrieval.documents),
                "lane_counts": retrieval.lane_counts,
            },
            {
                "id": "citation_expansion",
                "title": "Citation expansion",
                "status": citation.citation_map.get("status", "complete"),
                "current": len(citation.candidates),
                "total": len(citation.candidates),
                "candidate_count": len(citation.candidates),
                "added_candidates": int(citation.citation_map.get("added_candidates") or 0),
            },
            {
                "id": "candidate_selection",
                "title": "Preliminary ranking",
                "status": "degraded" if selection.warnings else "complete",
                "requested_mode": selection.requested_mode,
                "actual_mode": selection.actual_mode,
                "component_statuses": selection.component_statuses,
                "candidate_count": len(selection.documents),
                "selected_count": selection.selected_count,
            },
            {
                "id": "llm_reranking",
                "title": "LLM reranking",
                "status": "complete" if not ranking.warnings else "degraded",
                "current": len(ranking.items),
                "total": len(ranking.items),
                "candidate_count": len(ranking.items),
            },
        ]

    @staticmethod
    def _search_state(query, candidates, provenance) -> dict[str, Any]:
        keys = [document_key(document) for document in candidates]
        ledger = {}
        for document in candidates:
            key = document_key(document)
            detail = dict(provenance.get(key) or provenance.get(document.uid) or {})
            identifiers = document.identifiers
            ledger[key] = {
                "candidate_id": key,
                "title": document.title,
                "doi": identifiers.doi if identifiers else "",
                "openalex": identifiers.openalex if identifiers else "",
                "year": document.source.publish_year if document.source else None,
                "sources": detail.get("sources") or ([document.provider] if document.provider else []),
                "origins": detail.get("origins") or [],
                "retrieval_lanes": detail.get("retrieval_lanes") or [],
                "retrieval_score": float(detail.get("retrieval_score") or 0),
                **{key: detail[key] for key in ("decision_score", "decision_rank", "decision_provider", "decision_model", "decision_confidence", "selection_rrf_score") if key in detail},
            }
        origin_counts: dict[str, int] = {}
        for detail in ledger.values():
            for origin in detail["origins"]:
                origin_counts[origin] = origin_counts.get(origin, 0) + 1
        return {
            "question": query.question,
            "source": query.source,
            "current_query": query.final_query,
            "terminal_status": query.terminal_status,
            "candidate_queue": {
                "keys": keys,
                "ledger": ledger,
                "origin_counts": origin_counts,
                "duplicate_count": 0,
                "ledger_summary": {
                    "total_candidates": len(keys),
                    "serialized_candidates": len(keys),
                    "omitted_candidates": 0,
                },
            },
        }

    async def _final_checkpoint(self, result: ResearchResult) -> None:
        self.cancellation.raise_if_cancelled()
        if self.observer.on_checkpoint is None:
            return
        artifact_rows = [
            {
                "kind": artifact.kind,
                "filename": artifact.filename,
                "media_type": artifact.media_type,
                "size": len(artifact.content.encode("utf-8")),
                "sha256": hashlib.sha256(artifact.content.encode("utf-8")).hexdigest(),
            }
            for artifact in result.artifacts
        ]
        payload = {
            "final_result": result.public_payload(),
            "result": result.model_dump(mode="json", exclude={"artifacts"}),
            "warnings": result.warnings,
            "artifacts": artifact_rows,
            "resume_from_stage": "complete",
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        await record_checkpoint(
            self.observer,
            CheckpointDraft(
                step_key="assemble_final_result",
                idempotency_key=f"final:{digest}",
                state=payload,
                workflow_version="m2.research-workflow.v2",
            )
        )

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.cancellation.raise_if_cancelled()
        self.observer.on_event(
            CoreProgressEvent(event_type=event_type, stage="workflow", payload=payload)
        )


__all__ = ["ResearchWorkflow"]
