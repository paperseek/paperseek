from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from paperseek_app.models import (
    ArtifactWrite,
    CleanupLease,
    DeletionReceipt,
    LeaseToken,
    PendingEvent,
    PersistedCheckpoint,
    PersistedEvent,
    PersistedResult,
    ResultPage,
    HistoryPage,
    RunDetail,
    RunVersions,
    StoredArtifact,
)
from paperseek_core.models import CheckpointDraft, ResearchResult


class RunUnitOfWork(Protocol):
    def submit_run(
        self,
        subject: str,
        idempotency_key: str,
        request_digest: str,
        question: str,
        source: str,
        config_snapshot: dict,
        versions: RunVersions,
        deadline_for: timedelta,
    ) -> RunDetail: ...

    def create_run(
        self,
        question: str,
        source: str,
        config_snapshot: dict,
        versions: RunVersions,
        deadline_for: timedelta,
    ) -> RunDetail: ...

    def claim_run(self, run_id: str, owner_id: str, lease_for: timedelta) -> LeaseToken: ...

    def claim_next_run(self, owner_id: str, lease_for: timedelta) -> LeaseToken | None: ...

    def renew_lease(self, lease: LeaseToken, lease_for: timedelta) -> LeaseToken: ...

    def request_cancel(self, run_id: str) -> RunDetail: ...

    def complete_cancel(self, lease: LeaseToken) -> RunDetail: ...

    def append_events_and_checkpoint(
        self,
        lease: LeaseToken,
        events: list[PendingEvent],
        checkpoint: CheckpointDraft | None,
        checkpoint_artifact: StoredArtifact | None,
    ) -> PersistedCheckpoint | None: ...

    def complete_run(
        self,
        lease: LeaseToken,
        events: list[PendingEvent],
        result: ResearchResult | None,
        artifacts: list[StoredArtifact],
    ) -> RunDetail: ...

    def fail_run(
        self,
        lease: LeaseToken,
        events: list[PendingEvent],
        error_code: str,
    ) -> RunDetail: ...

    def retry_from_step(
        self,
        run_id: str,
        *,
        checkpoint_patterns: tuple[str, ...],
        step: str,
        deadline_for: timedelta,
    ) -> tuple[RunDetail, list[StoredArtifact]]: ...

    def get_run(self, run_id: str) -> RunDetail: ...

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[PersistedEvent]: ...

    def get_checkpoint(self, run_id: str) -> PersistedCheckpoint | None: ...

    def list_checkpoints(self, run_id: str) -> list[PersistedCheckpoint]: ...

    def get_results(
        self,
        run_id: str,
        *,
        after_rank: int = 0,
        limit: int | None = None,
        uids: list[str] | None = None,
    ) -> list[PersistedResult]: ...

    def query_results(
        self, run_id: str, *, offset: int, limit: int, query: str | None,
        min_score: float | None, metadata: str | None, sort: str,
    ) -> ResultPage: ...

    def search_history(self, *, query: str, offset: int, limit: int) -> HistoryPage: ...

    def rename_history(self, run_id: str, title: str) -> None: ...

    def list_history(
        self,
        *,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> list[RunDetail]: ...

    def get_artifact(self, artifact_id: str) -> StoredArtifact: ...

    def list_artifacts(self, run_id: str) -> list[StoredArtifact]: ...

    def stage_run_deletion(self, run_id: str) -> DeletionReceipt: ...

    def claim_cleanup_job(
        self,
        owner_id: str,
        lease_for: timedelta,
    ) -> CleanupLease | None: ...

    def list_cleanup_artifacts(self, lease: CleanupLease) -> list[StoredArtifact]: ...

    def complete_cleanup(self, lease: CleanupLease) -> None: ...

    def fail_cleanup(self, lease: CleanupLease, error_code: str) -> None: ...


class ArtifactStore(Protocol):
    def put(self, artifact: ArtifactWrite) -> StoredArtifact: ...

    def read(self, artifact: StoredArtifact) -> bytes: ...

    def delete(self, artifact: StoredArtifact) -> None: ...
