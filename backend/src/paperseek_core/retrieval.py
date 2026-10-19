"""Capability-driven multi-lane retrieval with deterministic local fusion."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from paperseek_core.document_identity import document_key
from paperseek_core.bibliometrics.corpus import build_corpus
from paperseek_core.candidate_selection import CandidateSelectionPipeline, embedding_provenance
from paperseek_core.errors import RetrievalSourceError
from paperseek_core.execution import NEVER_CANCELLED, record_checkpoint
from paperseek_core.http_retry import failure_details
from paperseek_core.models import (
    CheckpointDraft,
    CoreProgressEvent,
    PaperRecord,
    RetrievalRequest,
    RetrievalResult,
    SourceSearchMetadata,
)
from paperseek_core.ports import (
    CancellationToken,
    EmbeddingPort,
    SourcePort,
    WorkflowObserver,
)
from paperseek_core.sources.contracts import SourceError, SourceSearchRequest
from paperseek_core.retrieval_fusion import (
    FusedRetrievalResult, RetrievalLane, dedupe_documents, fuse_candidates_rrf,
)


@dataclass
class _LaneOutcome:
    documents: list[PaperRecord]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _LaneCollectionResult:
    capability_lanes: tuple[str, ...]
    lane_concurrency: int
    lane_results: dict[str, list[PaperRecord]]
    completed: set[str]
    warnings: list[str]


class RetrievalPipeline:
    def __init__(
        self,
        source: SourcePort,
        observer: WorkflowObserver,
        *,
        embedding: EmbeddingPort | None = None,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> None:
        self.source = source
        self.observer = observer
        self.embedding = embedding
        self.cancellation = cancellation

    async def run(self, request: RetrievalRequest) -> RetrievalResult:
        self.cancellation.raise_if_cancelled()
        collected = await self._collect_lanes(request)
        ordered_lane_results = self._prepare_ordered_lane_results(request, collected)
        embedding_state = await CandidateSelectionPipeline(
            self.observer, embedding=self.embedding, cancellation=self.cancellation,
        ).embed(request.question, [doc for lane in ordered_lane_results.values() for doc in lane], collected.warnings)
        fused = await asyncio.to_thread(fuse_candidates_rrf,
            request.question,
            ordered_lane_results,
            pool_max=request.pool_max,
            rrf_k=request.rrf_k,
            embedding_scores_by_key=embedding_state.scores_by_identity if embedding_state else None,
        )
        documents = list(fused.documents)
        provenance = self._build_provenance(fused)
        for document in documents:
            provenance[document_key(document)].update(embedding_provenance(document, embedding_state))

        completed_lanes = [
            lane for lane in collected.capability_lanes if lane in collected.completed
        ]
        result = RetrievalResult(
            bibliometric_corpus=await asyncio.to_thread(
                build_corpus, (doc for lane in [*collected.lane_results.values(), *request.supplemental_lanes.values()] for doc in lane),
                retrieval_pool_limit=request.pool_max, scoring_limit=request.ranking_candidate_limit,
            ),
            documents=documents,
            provenance_by_identity=provenance,
            lane_counts=fused.lane_counts,
            total_candidates=fused.total_candidates,
            completed_lanes=completed_lanes,
            warnings=collected.warnings,
            embedding_state=embedding_state,
        )
        await self._fusion_checkpoint(request, result)
        self._event(
            "retrieval.completed",
            {
                "source": self.source.name,
                "candidate_count": len(documents),
                "total_candidates": fused.total_candidates,
                "completed_lanes": len(completed_lanes),
                "total_lanes": len(collected.capability_lanes),
                "lane_concurrency": collected.lane_concurrency,
                "fusion": "rrf",
                "lane_counts": fused.lane_counts,
                "warning_count": len(collected.warnings),
            },
        )
        return result

    async def _collect_lanes(self, request: RetrievalRequest) -> _LaneCollectionResult:
        capabilities = self.source.capabilities
        capability_lanes = tuple(capabilities.lanes)
        limits = {
            lane: self._effective_lane_limit(request, lane)
            for lane in capability_lanes
        }
        completed = {
            lane
            for lane in request.completed_lanes
            if lane in capability_lanes
        }
        lane_results = {
            lane: list(request.resume_lane_results.get(lane, []))
            for lane in capability_lanes
            if lane in completed
        }
        warnings_by_lane: dict[str, list[str]] = {}
        errors_by_lane: dict[str, Exception] = {}
        lane_concurrency = max(
            1,
            min(len(capability_lanes) or 1, int(capabilities.lane_concurrency or 1)),
        )
        semaphore = asyncio.Semaphore(lane_concurrency)
        self._event(
            "retrieval.started",
            {
                "source": self.source.name,
                "lanes": list(capability_lanes),
                "total_lanes": len(capability_lanes),
                "lane_limits": limits,
                "lane_concurrency": lane_concurrency,
                "pool_max": request.pool_max,
            },
        )

        async def run_lane(lane: str) -> None:
            try:
                async with semaphore:
                    self.cancellation.raise_if_cancelled()
                    outcome = await self._collect_lane(request, lane, limits[lane])
                    self.cancellation.raise_if_cancelled()
            except Exception as exc:
                errors_by_lane[lane] = exc
                warnings_by_lane[lane] = [f"retrieval_lane_failed:{lane}"]
                if isinstance(exc, SourceError) and exc.status == 429:
                    warnings_by_lane[lane].append('source_rate_limited')
                self._event(
                    "retrieval.degraded",
                    {
                        "code": "retrieval_lane_failed",
                        "lane": lane,
                        "error_type": type(exc).__name__, **failure_details(exc),
                    },
                )
                return
            lane_results[lane] = outcome.documents
            warnings_by_lane[lane] = outcome.warnings
            completed.add(lane)
            await self._lane_checkpoint(request, lane, lane_results, completed, warnings_by_lane)
            self._event(
                "retrieval.progress",
                {
                    "lane": lane,
                    "completed_lanes": len(completed),
                    "total_lanes": len(capability_lanes),
                    "lane_concurrency": lane_concurrency,
                    "lane_count": len(outcome.documents),
                    "candidate_count": sum(len(values) for values in lane_results.values()),
                },
            )

        async with asyncio.TaskGroup() as group:
            for lane in capability_lanes:
                if lane in completed:
                    continue
                self.cancellation.raise_if_cancelled()
                group.create_task(run_lane(lane))

        primary_error = errors_by_lane.get(RetrievalLane.RELEVANCE)
        if primary_error and not lane_results.get(RetrievalLane.RELEVANCE):
            self._event(
                "retrieval.failed",
                {
                    "code": "retrieval_primary_lane_failed",
                    "source": self.source.name,
                    "lane": RetrievalLane.RELEVANCE,
                    "error_type": type(primary_error).__name__, **failure_details(primary_error),
                },
            )
            raise RetrievalSourceError("The primary retrieval lane failed without recoverable candidates.") from primary_error

        return _LaneCollectionResult(
            capability_lanes=capability_lanes,
            lane_concurrency=lane_concurrency,
            lane_results=lane_results,
            completed=completed,
            warnings=self._ordered_lane_warnings(capability_lanes, warnings_by_lane),
        )

    def _prepare_ordered_lane_results(
        self,
        request: RetrievalRequest,
        collected: _LaneCollectionResult,
    ) -> dict[str, list[PaperRecord]]:
        ordered_lane_results = {
            lane: collected.lane_results[lane][:self._effective_lane_limit(request, lane)]
            for lane in collected.capability_lanes
            if lane in collected.lane_results
        }
        for lane, documents in request.supplemental_lanes.items():
            if documents:
                ordered_lane_results[lane] = dedupe_documents(documents)
        if not ordered_lane_results and request.primary_hits:
            ordered_lane_results[RetrievalLane.RELEVANCE] = list(request.primary_hits)
        return ordered_lane_results

    @staticmethod
    def _build_provenance(
        fused: FusedRetrievalResult,
    ) -> dict[str, dict[str, Any]]:
        provenance = {}
        for document in fused.documents:
            key = document_key(document)
            metadata = dict(fused.metadata_by_key.get(key, {}))
            metadata["origins"] = ["retrieval"]
            metadata["sources"] = [document.provider] if document.provider else []
            provenance[key] = metadata
        return provenance

    def _effective_lane_limit(self, request: RetrievalRequest, lane: str) -> int:
        ranking_limit = min(request.ranking_candidate_limit, request.pool_max)
        useful_depth = max(100, ranking_limit)
        if lane != RetrievalLane.RELEVANCE:
            useful_depth = max(100, int(math.ceil(ranking_limit / 4.0)))
        return max(
            1,
            min(
                request.lane_limit,
                self.source.capabilities.max_lane_limit,
                request.pool_max,
                useful_depth,
            ),
        )

    async def _collect_lane(
        self,
        request: RetrievalRequest,
        lane: str,
        limit: int,
    ) -> _LaneOutcome:
        initial = request.primary_hits if lane == RetrievalLane.RELEVANCE else []
        first_page = request.primary_page if lane == RetrievalLane.RELEVANCE else None
        collected = []
        warnings = []
        page = first_page.page if first_page else 1
        page_size = first_page.limit if first_page else min(
            self.source.capabilities.page_size(lane), limit, 100 if initial else limit,
        )
        scan_limit = min(request.lane_limit, self.source.capabilities.max_lane_limit, request.pool_max)
        max_pages = max(1, math.ceil(scan_limit / page_size))
        pages_read = 0
        raw_count = 0
        stop_reason = "scan_budget_exhausted"
        while pages_read < max_pages:
            self.cancellation.raise_if_cancelled()
            if pages_read == 0 and (initial or first_page is not None):
                hits = initial
                metadata = first_page or SourceSearchMetadata(page=1, limit=page_size)
            else:
                try:
                    response = await self.source.search(SourceSearchRequest(
                        query=request.question if lane == RetrievalLane.SEMANTIC else request.query,
                        limit=page_size, page=page, lane=lane, field_ids=request.field_ids,
                    ))
                    self.cancellation.raise_if_cancelled()
                except Exception as exc:
                    if not collected:
                        raise
                    warnings.append(f"retrieval_page_failed_candidates_retained:{lane}")
                    if isinstance(exc, SourceError) and exc.status == 429:
                        warnings.append('source_rate_limited')
                    self._event("retrieval.degraded", {
                        "code": "retrieval_page_failed_candidates_retained", "lane": lane,
                        "page": page, "retained_candidates": len(collected),
                        "error_type": type(exc).__name__, **failure_details(exc),
                    })
                    stop_reason = "page_failed"
                    break
                hits, metadata = list(response.hits), response.metadata
                warnings.extend(code for code in response.warnings if code not in warnings)
            pages_read += 1
            returned = metadata.raw_count if metadata.raw_count is not None else len(hits)
            raw_count += returned
            # Keep the whole returned page for bibliometrics and checkpoint
            # recovery. Ranking still receives the same bounded lane slice.
            collected = dedupe_documents([*collected, *hits])
            if len(collected) >= limit:
                stop_reason = "candidate_limit_reached"
                break
            # Use the source page size and raw count, never the deduplicated count.
            # Page-number APIs must retain this size to keep offsets stable.
            page_size = metadata.limit
            max_pages = max(1, math.ceil(scan_limit / page_size))
            if (
                self.source.capabilities.is_single_page(lane)
                or returned < page_size
                or (metadata.total > 0 and page * page_size >= metadata.total)
            ):
                stop_reason = "source_exhausted"
                break
            page += 1
        if stop_reason == "scan_budget_exhausted":
            warnings.append(f"retrieval_scan_budget_exhausted:{lane}")
        self._event("retrieval.lane_completed", {
            "lane": lane, "pages_read": pages_read, "raw_count": raw_count,
            "candidate_count": min(len(collected), limit), "stop_reason": stop_reason,
        })
        return _LaneOutcome(collected, warnings)

    async def _lane_checkpoint(
        self,
        request: RetrievalRequest,
        lane: str,
        lane_results: dict[str, list[PaperRecord]],
        completed: set[str],
        warnings_by_lane: dict[str, list[str]],
    ) -> None:
        self.cancellation.raise_if_cancelled()
        if self.observer.on_checkpoint is None:
            return
        ordered_lanes = tuple(self.source.capabilities.lanes)
        state_results = {
            key: [document.model_dump(mode="json") for document in lane_results[key]]
            for key in ordered_lanes
            if key in lane_results
        }
        await record_checkpoint(
            self.observer,
            CheckpointDraft(
                step_key="retrieve.lane",
                idempotency_key=self._lane_idempotency_key(request, lane),
                state={
                    "completed_lanes": [key for key in ordered_lanes if key in completed],
                    "lane_results": state_results,
                    "warnings": self._ordered_lane_warnings(ordered_lanes, warnings_by_lane),
                    "resume_from_stage": request.checkpoint_resume_stage,
                },
                workflow_version="m2.retrieval.v2",
            )
        )

    async def _fusion_checkpoint(self, request: RetrievalRequest, result: RetrievalResult) -> None:
        self.cancellation.raise_if_cancelled()
        if self.observer.on_checkpoint is None:
            return
        payload = {
            "source": self.source.name,
            "question": request.question,
            "query": request.query,
            "completed_lanes": result.completed_lanes,
            "pool_max": request.pool_max,
            "rrf_k": request.rrf_k,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        await record_checkpoint(
            self.observer,
            CheckpointDraft(
                step_key="retrieve.fuse",
                idempotency_key=f"retrieval:fuse:{digest}",
                state={
                    "bibliometric_corpus": result.bibliometric_corpus.model_dump(mode="json") if result.bibliometric_corpus else None,
                    "candidate_pool": [document.model_dump(mode="json") for document in result.documents],
                    "embedding_state": result.embedding_state.model_dump(mode="json") if result.embedding_state else None,
                    "provenance_by_identity": result.provenance_by_identity,
                    "lane_counts": result.lane_counts,
                    "total_candidates": result.total_candidates,
                    "completed_lanes": result.completed_lanes,
                    "warnings": result.warnings,
                    "resume_from_stage": "post_retrieval",
                },
                workflow_version="m2.retrieval.v2",
            )
        )

    def _lane_idempotency_key(self, request: RetrievalRequest, lane: str) -> str:
        payload = {
            "source": self.source.name,
            "lane": lane,
            "query": request.question if lane == RetrievalLane.SEMANTIC else request.query,
            "limit": self._effective_lane_limit(request, lane),
            "field_ids": request.field_ids,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"retrieval:lane:{lane}:{digest}"

    @staticmethod
    def _ordered_lane_warnings(
        lanes: Sequence[str],
        warnings_by_lane: Mapping[str, Sequence[str]],
    ) -> list[str]:
        return [warning for lane in lanes for warning in warnings_by_lane.get(lane, ())]

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.cancellation.raise_if_cancelled()
        self.observer.on_event(
            CoreProgressEvent(event_type=event_type, stage="retrieval", payload=payload)
        )


__all__ = ["RetrievalPipeline"]
