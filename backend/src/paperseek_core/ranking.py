"""Deterministic LLM ranking, recovery, and checkpoint semantics."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from paperseek_core.document_identity import document_identity, document_key
from paperseek_core.execution import NEVER_CANCELLED, record_checkpoint
from paperseek_core.http_retry import failure_details
from paperseek_core.models import (
    CheckpointDraft,
    CoreProgressEvent,
    PaperRecord,
    RankedPaper,
    RankingRequest,
    RankingResult,
    RankingScore,
)
from paperseek_core.ports import CancellationToken, LLMPort, WorkflowObserver
from paperseek_core.errors import EmptyRankingBatchError
from paperseek_core.ranking_output import RankingOutputCodec
from paperseek_core.ranking_scoring import RankingScorer


@dataclass(frozen=True)
class RankingBatchScheduler:
    batch_size_value: Any = 64
    concurrency_value: Any = 8

    def batch_size(self, total_documents: int) -> int:
        try:
            configured = max(1, int(self.batch_size_value or 64))
        except (TypeError, ValueError):
            configured = 64
        return min(configured, max(1, int(total_documents or 0)))

    def concurrency(self) -> int:
        try:
            return min(32, max(1, int(self.concurrency_value or 8)))
        except (TypeError, ValueError):
            return 8

    def concurrency_schedule(self) -> list[int]:
        """The configured level first, then lower ones to fall back to after a rate limit."""
        configured = self.concurrency()
        schedule = [tier for tier in (32, 16, 8, 4, 2, 1) if tier <= configured]
        if configured not in schedule:
            schedule.insert(0, configured)
        return schedule

    @staticmethod
    def batch_id(
        step_id: str,
        documents: list[PaperRecord],
        identity: Callable[[PaperRecord], str] = document_identity,
    ) -> str:
        candidate_ids = [str(document.uid or identity(document)) for document in documents]
        digest = hashlib.sha256("\0".join(candidate_ids).encode("utf-8")).hexdigest()[:20]
        return f"{step_id}:{digest}"


class RankingPipeline:
    def __init__(
        self,
        llm: LLMPort,
        observer: WorkflowObserver,
        *,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ):
        self.llm = llm
        self.observer = observer
        self.cancellation = cancellation
        self.codec = RankingOutputCodec()
        self.scorer = RankingScorer(llm)

    async def run(self, request: RankingRequest) -> RankingResult:
        self.cancellation.raise_if_cancelled()
        documents = list(request.documents)
        if not documents:
            return RankingResult()

        scheduler = RankingBatchScheduler(request.batch_size, request.concurrency)
        batch_size = scheduler.batch_size(len(documents))
        batches = [documents[index:index + batch_size] for index in range(0, len(documents), batch_size)]
        batch_ids = {
            index: scheduler.batch_id(request.step_id, batch)
            for index, batch in enumerate(batches, 1)
        }
        completed_ids = set(request.completed_batch_ids)
        scores = self.codec.validated_scores(request.resume_scores)
        self._event(
            "ranking.started",
            {
                "candidate_count": len(documents),
                "llm_gate_candidate_count": len(documents),
                "batch_count": len(batches),
                "batch_size": batch_size,
                "concurrency": scheduler.concurrency(),
            },
        )

        if len(batches) == 1:
            warnings = await self._run_single_batch(
                request,
                batches[0],
                batch_ids[1],
                completed_ids,
                scores,
                scheduler.concurrency(),
            )
        else:
            scores, warnings = await self._run_multiple_batches(
                request,
                batches,
                batch_ids,
                completed_ids,
                scores,
                scheduler,
            )

        _, missing = self.codec.match_scores(documents, scores)
        if scores and missing and "ranking_partial_failure_local_order_retained" not in warnings:
            warnings.append("ranking_partial_failure_local_order_retained")
            self._event("ranking.degraded", {
                "code": "ranking_partial_failure_local_order_retained",
                "failed_candidates": len(missing),
                "scored_count": len(scores),
            })
        items = self._rank_from_scores(documents, scores, request.provenance_by_identity)
        self._event(
            "ranking.completed",
            {
                "candidate_count": len(documents),
                "scored_count": len(scores),
                "warning_count": len(warnings),
            },
        )
        return RankingResult(
            items=items,
            scores=[RankingScore.model_validate(score) for score in scores],
            completed_batch_ids=sorted(completed_ids),
            warnings=warnings,
        )

    async def _run_single_batch(
        self,
        request: RankingRequest,
        documents: list[PaperRecord],
        batch_id: str,
        completed_ids: set[str],
        scores: list[dict[str, Any]],
        concurrency: int,
    ) -> list[str]:
        if batch_id in completed_ids:
            return []
        try:
            self.cancellation.raise_if_cancelled()
            batch_scores = await self.scorer.score_batch(
                request.question,
                documents,
                request.plan_context,
            )
            self.cancellation.raise_if_cancelled()
            if documents and not batch_scores:
                raise EmptyRankingBatchError("LLM ranking batch returned no valid score entries.")
            batch_scores, warnings = await self._attach_reasons(
                request.question,
                documents,
                batch_scores,
                request.plan_context,
                concurrency,
            )
        except Exception as exc:
            self._degraded(exc, failed_batches=1, failed_candidates=len(documents))
            return ["ranking_failed_local_order_retained"]

        scores.extend(batch_scores)
        completed_ids.add(batch_id)
        await self._checkpoint(request, batch_id, scores, completed_ids)
        self._event(
            "ranking.batch_completed",
            {
                "batch_index": 1,
                "batch_count": 1,
                "completed_batches": len(completed_ids),
                "candidate_count": len(documents),
                "scored_count": len(scores),
                "concurrency": concurrency,
            },
        )
        return warnings

    async def _run_multiple_batches(
        self,
        request: RankingRequest,
        batches: list[list[PaperRecord]],
        batch_ids: dict[int, str],
        completed_ids: set[str],
        scores: list[dict[str, Any]],
        scheduler: RankingBatchScheduler,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        remaining = [
            (index, batch)
            for index, batch in enumerate(batches, 1)
            if batch_ids[index] not in completed_ids
        ]
        schedule = scheduler.concurrency_schedule()
        exhausted = []
        # Reasons later use the level scoring settled on, lower after a rate limit.
        settled = schedule[0]
        for tier_index, workers in enumerate(schedule):
            settled = workers
            self.cancellation.raise_if_cancelled()
            if not remaining:
                break
            lower_tier_available = tier_index < len(schedule) - 1
            same_tier_retry_used = False
            while True:
                failed: list[tuple[int, list[PaperRecord]]] = []
                empty_failures = 0
                rate_limited = False
                worklist = list(remaining)
                for start in range(0, len(worklist), workers):
                    self.cancellation.raise_if_cancelled()
                    chunk = worklist[start:start + workers]

                    async def score(batch: list[PaperRecord]):
                        try:
                            result = await self.scorer.score_batch(
                                request.question,
                                batch,
                                request.plan_context,
                            )
                            self.cancellation.raise_if_cancelled()
                            return result
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            return exc

                    tasks = []
                    async with asyncio.TaskGroup() as group:
                        for _, batch in chunk:
                            self.cancellation.raise_if_cancelled()
                            tasks.append(group.create_task(score(batch)))
                    outcomes = [task.result() for task in tasks]
                    for (index, batch), outcome in zip(chunk, outcomes, strict=True):
                        if isinstance(outcome, asyncio.CancelledError):
                            raise outcome
                        if isinstance(outcome, Exception):
                            target = exhausted if failure_details(outcome).get("attempts") else failed
                            target.append((index, batch))
                            empty_failures += isinstance(outcome, EmptyRankingBatchError)
                            rate_limited = rate_limited or self._is_rate_limit_error(outcome)
                            self._event(
                                "ranking.batch_failed",
                                {
                                    "batch_index": index,
                                    "batch_count": len(batches),
                                    "candidate_count": len(batch),
                                    "concurrency": workers,
                                    "rate_limited": self._is_rate_limit_error(outcome),
                                    "error_code": str(getattr(outcome, "code", "") or ""),
                                    **failure_details(outcome),
                                },
                            )
                            continue
                        scores.extend(outcome)
                        completed_ids.add(batch_ids[index])
                        await self._checkpoint(request, batch_ids[index], scores, completed_ids)
                        self._event(
                            "ranking.batch_completed",
                            {
                                "batch_index": index,
                                "batch_count": len(batches),
                                "completed_batches": len(completed_ids),
                                "candidate_count": len(batch),
                                "scored_count": len(scores),
                                "concurrency": workers,
                            },
                        )
                    if rate_limited and lower_tier_available:
                        failed.extend(worklist[start + len(chunk):])
                        break
                if (
                    failed
                    and empty_failures == len(failed)
                    and not rate_limited
                    and not same_tier_retry_used
                ):
                    same_tier_retry_used = True
                    remaining = failed
                    continue
                remaining = failed
                break

        warnings = []
        remaining.extend(exhausted)
        if remaining:
            failed_candidates = sum(len(batch) for _, batch in remaining)
            self._degraded(
                EmptyRankingBatchError("One or more ranking batches failed."),
                failed_batches=len(remaining),
                failed_candidates=failed_candidates,
            )
            warnings.append(
                "ranking_partial_failure_local_order_retained"
                if scores
                else "ranking_failed_local_order_retained"
            )
        if scores:
            scores, reason_warnings = await self._attach_reasons(
                request.question,
                list(request.documents),
                scores,
                request.plan_context,
                settled,
                restored={str(entry["uid"]): str(entry["reasoning"])
                          for entry in self.codec.validated_scores(request.resume_scores) if entry["reasoning"]},
            )
            warnings.extend(reason_warnings)
            # Keep the written reasons, so resuming after this point does not ask for them again.
            await self._checkpoint(request, f"{request.step_id}:reasons", scores, completed_ids)
        return scores, warnings

    async def _attach_reasons(
        self,
        question: str,
        documents: list[PaperRecord],
        scores: list[dict[str, Any]],
        plan_context: str,
        concurrency: int,
        restored: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        def progress(phase: str, completed: int, total: int, papers: int) -> None:
            self._event("ranking.reasons_progress", {
                "phase": phase, "completed_groups": completed, "group_count": total,
                "paper_count": papers, "concurrency": concurrency,
            })

        scores = await self.scorer.attach_reasons(
            question,
            documents,
            scores,
            plan_context,
            concurrency=concurrency,
            on_progress=progress,
            restored=restored,
        )
        self.cancellation.raise_if_cancelled()
        if not self.scorer.missing_reason_count:
            return scores, []
        self._event(
            "ranking.degraded",
            {
                "code": "ranking_reasons_missing",
                "missing_reason_count": self.scorer.missing_reason_count,
                **(self.scorer.reason_failures[0] if self.scorer.reason_failures else {}),
            },
        )
        return scores, ["ranking_reasons_missing"]

    async def _checkpoint(
        self,
        request: RankingRequest,
        batch_id: str,
        scores: list[dict[str, Any]],
        completed_ids: set[str],
    ) -> None:
        self.cancellation.raise_if_cancelled()
        if self.observer.on_checkpoint is None:
            return
        await record_checkpoint(
            self.observer,
            CheckpointDraft(
                step_key="rank_results.batch",
                idempotency_key=batch_id,
                state={
                    "ranking_scores": self.codec.validated_scores(scores),
                    "completed_batch_ids": sorted(completed_ids),
                    "resume_from_stage": request.checkpoint_resume_stage,
                },
                workflow_version="m2.ranking.v1",
            )
        )

    def _rank_from_scores(
        self,
        documents: list[PaperRecord],
        scores: list[dict[str, Any]],
        provenance_by_identity: dict[str, dict[str, Any]],
    ) -> list[RankedPaper]:
        valid_scores, missing_documents = self.codec.match_scores(documents, scores)
        identity_to_document = {
            document_identity(document): document
            for document in documents
            if document_identity(document)
        }
        ranked = []
        for entry in valid_scores:
            document = identity_to_document.get(entry["uid"])
            if document is None:
                continue
            ranked.append(
                RankedPaper(
                    document=document,
                    score=entry.get("score", 0),
                    reasoning=entry.get("reasoning", ""),
                    provenance=self._provenance(document, provenance_by_identity),
                )
            )
        for document in missing_documents:
            ranked.append(
                RankedPaper(
                    document=document,
                    provenance=self._provenance(document, provenance_by_identity),
                )
            )
        input_order = {document_identity(doc): index for index, doc in enumerate(documents)}
        return sorted(ranked, key=lambda item: (-float(item.score or 0), input_order[document_identity(item.document)]))

    @staticmethod
    def _provenance(
        document: PaperRecord,
        provenance_by_identity: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        return dict(
            provenance_by_identity.get(document_identity(document))
            or provenance_by_identity.get(document_key(document))
            or {}
        )

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc or "").casefold()
        return "429" in text or "rate_limit" in text or "too many requests" in text

    def _degraded(self, exc: Exception, *, failed_batches: int, failed_candidates: int) -> None:
        self._event(
            "ranking.degraded",
            {
                "code": "ranking_failed_local_order_retained",
                "error_type": type(exc).__name__,
                "error_code": str(getattr(exc, "code", "") or ""),
                "failed_batches": failed_batches,
                "failed_candidates": failed_candidates,
                **failure_details(exc),
            },
        )

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.cancellation.raise_if_cancelled()
        self.observer.on_event(
            CoreProgressEvent(event_type=event_type, stage="ranking", payload=payload)
        )


__all__ = ["RankingBatchScheduler", "RankingPipeline"]
