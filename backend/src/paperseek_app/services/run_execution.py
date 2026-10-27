from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta

from pydantic import ValidationError

from paperseek_app.errors import (
    ArtifactError,
    IncompatibleCheckpoint,
    RunOwnershipLost,
    RunPersistenceError,
    RunStateError,
)
from paperseek_app.leased_task import run_leased_task
from paperseek_app.execution import CancellationScope, WorkflowExecution, run_blocking
from paperseek_app.models import (
    ArtifactRole,
    ArtifactVisibility,
    ArtifactWrite,
    LeaseToken,
    PendingEvent,
    PersistedCheckpoint,
    RunDetail,
    RunStatus,
    StoredArtifact,
)
from paperseek_app.ports import ArtifactStore, RunUnitOfWork
from paperseek_core.errors import LLMRequestError, QueryGenerationError, QuerySourceError, RetrievalSourceError
from paperseek_core.http_retry import failure_details
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.models import (
    CandidateSelectionResult,
    DecisionBatchCheckpoint,
    CheckpointDraft,
    CitationExpansionResult,
    CoreProgressEvent,
    PaperRecord,
    QueryLoopResumeState,
    ResearchWorkflowRequest,
    ResearchWorkflowResumeState,
    ResearchResult,
    RetrievalResult,
)
from paperseek_core.ports import (
    CancellationToken,
    EmbeddingPort,
    DecisionPort,
    EnrichmentPort,
    LLMPort,
    RerankPort,
    SourcePort,
)


class ProgressObserver:
    on_checkpoint = None

    def __init__(
        self,
        service: RunExecutionService,
        lease: LeaseToken,
        workflow_version: str,
    ) -> None:
        self._service = service
        self._lease = lease
        self._workflow_version = workflow_version
        self._pending: list[PendingEvent] = []
        self._write_lock = asyncio.Lock()

    def on_event(self, event: CoreProgressEvent) -> None:
        self._pending.append(
            PendingEvent(
                event_type=event.event_type,
                stage=event.stage,
                payload=event.payload,
                schema_version=event.schema_version,
            )
        )

    async def flush_progress(self) -> None:
        async with self._write_lock:
            if self._pending:
                await run_blocking(
                    self._service.uow.append_events_and_checkpoint,
                    self._lease, self.drain(), None, None,
                )

    def drain(self) -> list[PendingEvent]:
        pending, self._pending = self._pending, []
        return pending


class TransactionalObserver(ProgressObserver):
    async def on_checkpoint(self, checkpoint: CheckpointDraft) -> None:
        state = {**checkpoint.state, "step_workflow_version": checkpoint.workflow_version}
        async with self._write_lock:
            await run_blocking(
                self._service.append_checkpoint, self._lease, self.drain(),
                checkpoint.model_copy(update={"state": state, "workflow_version": self._workflow_version}),
            )


class RunExecutionService:
    _CHECKPOINT_SCHEMA_VERSION = 1
    _STEP_VERSIONS = {
        "query.": "m2.query-loop.v2",
        "retrieve.": "m2.retrieval.v2",
        "expand_citations": "m2.citation.v1",
        "select_candidates": "m2.selection.v1",
        "selection.decision": "decision.v1",
        "rank_results.": "m2.ranking.v1",
        "assemble_final_result": "m2.research-workflow.v2",
    }

    def __init__(self, uow: RunUnitOfWork, artifact_store: ArtifactStore, *, recover_search: bool = True,
                 store_derived_exports: bool = True) -> None:
        self.uow = uow
        self.artifact_store = artifact_store
        self.recover_search = recover_search
        self.store_derived_exports = store_derived_exports

    def claim_next_run(self, owner_id: str, lease_seconds: int = 60) -> LeaseToken | None:
        return self.uow.claim_next_run(owner_id, timedelta(seconds=lease_seconds))

    def get_run(self, run_id: str) -> RunDetail:
        return self.uow.get_run(run_id)

    def get_checkpoint(self, run_id: str) -> PersistedCheckpoint | None:
        checkpoint = self.uow.get_checkpoint(run_id)
        return None if checkpoint is None else self._hydrate_checkpoint(checkpoint)

    def list_checkpoints(self, run_id: str) -> list[PersistedCheckpoint]:
        return [
            self._hydrate_checkpoint(checkpoint)
            for checkpoint in self.uow.list_checkpoints(run_id)
        ]

    def complete_cancel(self, lease: LeaseToken) -> RunDetail:
        return self.uow.complete_cancel(lease)

    def fail_claimed_run(self, lease: LeaseToken, error_code: str,
                         events: list[PendingEvent] | None = None) -> RunDetail:
        cancelled = self._finish_cancel_if_requested(lease)
        if cancelled is not None:
            return cancelled
        return self.uow.fail_run(lease, events or [], error_code)

    async def execute_claimed_workflow(
        self,
        lease: LeaseToken,
        *,
        llm: LLMPort,
        source: SourcePort,
        request: ResearchWorkflowRequest,
        embedding: EmbeddingPort | None = None,
        reranker: RerankPort | None = None,
        decision: DecisionPort | None = None,
        enrichment: EnrichmentPort | None = None,
        cancellation: CancellationScope | None = None,
        cancellation_poll_seconds: float = 0.05,
        lease_seconds: int = 60,
        heartbeat_seconds: float = 20.0,
        deadline_at: float | None = None,
    ) -> RunDetail:
        run = await run_blocking(self.uow.get_run, lease.run_id)
        observer_type = TransactionalObserver if self.recover_search else ProgressObserver
        observer = observer_type(self, lease, run.versions.workflow_version)
        scope = cancellation or CancellationScope()

        async def perform() -> RunDetail:
            try:
                result = await run_blocking(self._resume_final_result, run) if self.recover_search else None
                if result is None:
                    resume = await run_blocking(self._resume_state, run) if self.recover_search else None
                    result = await self._run_cancellable_workflow(
                        lease,
                        WorkflowExecution(
                            llm=llm,
                            source=source,
                            request=request.model_copy(update={"resume": resume}),
                            embedding=embedding,
                            reranker=reranker,
                            decision=decision,
                            enrichment=enrichment,
                            deadline_at=deadline_at,
                        ),
                        observer,
                        scope,
                    )
            except IncompatibleCheckpoint:
                return await run_blocking(self.fail_claimed_run, lease, "incompatible_checkpoint")
            except TimeoutError:
                return await run_blocking(self.fail_claimed_run, lease, "run_deadline_exceeded")
            except asyncio.CancelledError:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                raise
            except RunOwnershipLost:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                raise
            except (LLMRequestError, QueryGenerationError, QuerySourceError, RetrievalSourceError) as exc:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                code = exc.code if isinstance(exc, (LLMRequestError, QuerySourceError)) else "research_workflow_failed"
                observer.on_event(CoreProgressEvent(event_type="workflow.request_failed", stage="workflow",
                                                    payload={"code": code, **failure_details(exc)}))
                return await run_blocking(self.uow.fail_run, lease, observer.drain(), code)
            except ArtifactError:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                return await run_blocking(self.uow.fail_run, lease, observer.drain(), "artifact_persistence_failed")
            except RunPersistenceError:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                return await run_blocking(self.uow.fail_run, lease, observer.drain(), "checkpoint_commit_failed")

            # Join any in-flight progress transaction before final persistence/commit.
            # Progress-only observers have no final checkpoint to provide this barrier.
            await observer.flush_progress()
            cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
            if cancelled is not None:
                scope.cancel()
                return cancelled
            try:
                artifacts = await run_blocking(self._persist_required_artifacts, lease, run, result, scope)
            except RunOwnershipLost:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                raise
            except ArtifactError:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                return await run_blocking(self.uow.fail_run, lease, observer.drain(), "artifact_persistence_failed")
            cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
            if cancelled is not None:
                scope.cancel()
                return cancelled
            try:
                return await run_blocking(self.uow.complete_run, lease, observer.drain(), result, artifacts)
            except RunOwnershipLost:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                raise
            except RunPersistenceError:
                cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
                if cancelled is not None:
                    scope.cancel()
                    return cancelled
                return await run_blocking(self.uow.fail_run, lease, observer.drain(), "artifact_commit_failed")

        try:
            return await run_leased_task(
                perform(), uow=self.uow, lease=lease, scope=scope,
                poll_seconds=cancellation_poll_seconds, lease_seconds=lease_seconds,
                heartbeat_seconds=heartbeat_seconds, flush_progress=observer.flush_progress,
            )
        except asyncio.CancelledError:
            cancelled = await run_blocking(self._finish_cancel_if_requested, lease)
            if cancelled is not None:
                return cancelled
            raise
        except (ArtifactError, RunPersistenceError) as exc:
            code = "artifact_persistence_failed" if isinstance(exc, ArtifactError) else "checkpoint_commit_failed"
            return await run_blocking(self.fail_claimed_run, lease, code, observer.drain())

    async def _run_cancellable_workflow(
        self,
        lease: LeaseToken,
        execution: WorkflowExecution,
        observer: ProgressObserver,
        scope: CancellationScope,
    ):
        return await execution.run(observer, scope)

    def _finish_cancel_if_requested(
        self,
        lease: LeaseToken,
    ) -> RunDetail | None:
        detail = self.uow.get_run(lease.run_id)
        if detail.status is RunStatus.CANCELLING:
            try:
                return self.uow.complete_cancel(lease)
            except (RunOwnershipLost, RunStateError):
                # An expired cancellation may have been finalized by another worker.
                detail = self.uow.get_run(lease.run_id)
                if detail.status is RunStatus.CANCELLED:
                    return detail
                raise
        if detail.status is RunStatus.CANCELLED:
            return detail
        return None

    def append_checkpoint(
        self,
        lease: LeaseToken,
        events: list[PendingEvent],
        checkpoint: CheckpointDraft,
    ) -> PersistedCheckpoint:
        content = json.dumps(
            checkpoint.state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        name_digest = hashlib.sha256(checkpoint.idempotency_key.encode("utf-8")).hexdigest()
        artifact = self.artifact_store.put(
            ArtifactWrite(
                run_id=lease.run_id,
                kind="checkpoint",
                role=ArtifactRole.RECOVERY,
                visibility=ArtifactVisibility.INTERNAL,
                filename=f"state-{name_digest}.json",
                media_type="application/json",
                schema_version=checkpoint.schema_version,
                content=content,
            )
        )
        persisted = self.uow.append_events_and_checkpoint(
            lease,
            events,
            checkpoint,
            artifact,
        )
        if persisted is None:
            raise RunPersistenceError("The checkpoint was not committed.")
        return persisted

    def _hydrate_checkpoint(self, checkpoint: PersistedCheckpoint) -> PersistedCheckpoint:
        if not checkpoint.state_artifact_id:
            return checkpoint
        artifact = self.uow.get_artifact(checkpoint.state_artifact_id)
        try:
            state = json.loads(self.artifact_store.read(artifact))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RunPersistenceError("The checkpoint artifact is not valid JSON.") from exc
        if not isinstance(state, dict):
            raise RunPersistenceError("The checkpoint artifact must contain an object.")
        return checkpoint.model_copy(update={"state": state})

    def _resume_state(self, run: RunDetail) -> ResearchWorkflowResumeState | None:
        persisted = self.uow.list_checkpoints(run.id)
        if not persisted:
            return None
        for checkpoint in persisted:
            self._validate_checkpoint_metadata(run, checkpoint)
        checkpoints = [self._hydrate_checkpoint(item) for item in persisted]
        try:
            return self._compose_resume_state(checkpoints)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise IncompatibleCheckpoint("Checkpoint state is incompatible with this executor.") from exc

    def _resume_final_result(self, run: RunDetail) -> ResearchResult | None:
        for checkpoint in reversed(self.uow.list_checkpoints(run.id)):
            if checkpoint.step_key != "assemble_final_result":
                continue
            self._validate_checkpoint_metadata(run, checkpoint)
            state = self._hydrate_checkpoint(checkpoint).state
            if "result" not in state:
                return None
            try:
                result = ResearchResult.model_validate(state["result"])
            except ValidationError as exc:
                raise IncompatibleCheckpoint("Final result checkpoint is incompatible.") from exc
            return result.model_copy(update={"artifacts": FinalResultAssembler().artifacts_for(result)})
        return None

    def _validate_checkpoint_metadata(
        self,
        run: RunDetail,
        checkpoint: PersistedCheckpoint,
    ) -> None:
        if (
            checkpoint.workflow_version != run.versions.workflow_version
            or checkpoint.schema_version != self._CHECKPOINT_SCHEMA_VERSION
            or not checkpoint.state_artifact_id
        ):
            raise IncompatibleCheckpoint("Checkpoint metadata is incompatible with this executor.")

    def _compose_resume_state(
        self,
        checkpoints: list[PersistedCheckpoint],
    ) -> ResearchWorkflowResumeState:
        query = None
        retrieval = None
        retrieval_lane_results: dict[str, list[PaperRecord]] = {}
        retrieval_completed_lanes: list[str] = []
        citation = None
        selection = None
        decision_batches = []
        ranking_scores = []
        ranking_completed_batch_ids: list[str] = []
        for checkpoint in checkpoints:
            state = dict(checkpoint.state)
            step_version = str(state.pop("step_workflow_version", ""))
            if step_version != self._expected_step_version(checkpoint.step_key):
                raise IncompatibleCheckpoint("Checkpoint step version is incompatible.")
            if checkpoint.step_key.startswith("query."):
                query = QueryLoopResumeState.model_validate(state)
            elif checkpoint.step_key == "retrieve.lane":
                retrieval_lane_results = {
                    lane: [PaperRecord.model_validate(item) for item in records]
                    for lane, records in dict(state.get("lane_results") or {}).items()
                }
                retrieval_completed_lanes = list(state.get("completed_lanes") or [])
            elif checkpoint.step_key == "retrieve.fuse":
                retrieval = RetrievalResult(
                    bibliometric_corpus=state.get("bibliometric_corpus"),
                    documents=[
                        PaperRecord.model_validate(item)
                        for item in state.get("candidate_pool") or []
                    ],
                    provenance_by_identity=dict(state.get("provenance_by_identity") or {}),
                    lane_counts=dict(state.get("lane_counts") or {}),
                    total_candidates=int(state.get("total_candidates") or 0),
                    embedding_state=state.get("embedding_state"),
                    completed_lanes=list(state.get("completed_lanes") or []),
                    warnings=list(state.get("warnings") or []),
                )
            elif checkpoint.step_key == "expand_citations":
                if "candidates" not in state or "provenance_by_identity" not in state:
                    raise IncompatibleCheckpoint("Citation checkpoint lacks recovery state.")
                citation = CitationExpansionResult(
                    candidates=[
                        PaperRecord.model_validate(item)
                        for item in state.get("candidates") or []
                    ],
                    citation_map=dict(state.get("citation_map") or {}),
                    provenance_by_identity=dict(state.get("provenance_by_identity") or {}),
                    warnings=list(state.get("warnings") or []),
                )
            elif checkpoint.step_key == "selection.decision":
                decision_batches.append(DecisionBatchCheckpoint.model_validate(state))
            elif checkpoint.step_key == "select_candidates":
                state.pop("resume_from_stage", None)
                selection = CandidateSelectionResult.model_validate(state)
            elif checkpoint.step_key == "rank_results.batch":
                ranking_scores = list(state.get("ranking_scores") or [])
                ranking_completed_batch_ids = list(state.get("completed_batch_ids") or [])
        return ResearchWorkflowResumeState(
            query=query,
            retrieval=retrieval,
            retrieval_lane_results=retrieval_lane_results,
            retrieval_completed_lanes=retrieval_completed_lanes,
            citation=citation,
            selection=selection,
            decision_batches=decision_batches,
            ranking_scores=ranking_scores,
            ranking_completed_batch_ids=ranking_completed_batch_ids,
        )

    def _expected_step_version(self, step_key: str) -> str:
        for prefix, version in self._STEP_VERSIONS.items():
            if step_key == prefix or step_key.startswith(prefix):
                return version
        raise IncompatibleCheckpoint("Checkpoint step is not supported by this executor.")

    def _persist_required_artifacts(
        self,
        lease: LeaseToken,
        run: RunDetail,
        result,
        cancellation: CancellationToken,
    ) -> list[StoredArtifact]:
        artifacts = []
        for draft in result.artifacts:
            if not self.store_derived_exports and draft.kind not in {"final_result", "bibliometric_corpus"}:
                continue
            cancellation.raise_if_cancelled()
            current = self.uow.get_run(lease.run_id)
            if (
                current.status is not RunStatus.RUNNING
                or current.lease_owner != lease.owner_id
                or current.lease_generation != lease.lease_generation
            ):
                raise RunOwnershipLost("The run can no longer accept required artifacts.")
            artifacts.append(
                self.artifact_store.put(
                    ArtifactWrite(
                        run_id=lease.run_id,
                        kind=draft.kind,
                        role=ArtifactRole.COMPLETION_REQUIRED,
                        visibility=ArtifactVisibility.USER,
                        filename=draft.filename,
                        media_type=draft.media_type,
                        schema_version=run.versions.artifact_schema_version,
                        content=draft.content.encode("utf-8"),
                    )
                )
            )
        return artifacts
