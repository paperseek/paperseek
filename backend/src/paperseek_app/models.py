from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from paperseek_core.models import FinalPaperResult, ProgressPayload
from paperseek_app.configuration import RunConfigurationSnapshot


class AppModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRole(str, Enum):
    COMPLETION_REQUIRED = "completion_required"
    RECOVERY = "recovery"
    AUDIT = "audit"
    TEMPORARY = "temporary"


class ArtifactVisibility(str, Enum):
    USER = "user"
    INTERNAL = "internal"


class ArtifactWrite(AppModel):
    run_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    role: ArtifactRole
    visibility: ArtifactVisibility
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    content: bytes


class StoredArtifact(AppModel):
    id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    role: ArtifactRole
    visibility: ArtifactVisibility
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    storage_key: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    schema_version: int = Field(ge=1)
    created_at: datetime


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunVersions(AppModel):
    workflow_version: str = "m2.research-workflow.v2"
    engine_version: str = "1.0.0b4"
    prompt_version: str = "m2.search-history.v2"
    config_schema_version: int = 1
    artifact_schema_version: int = 1


class PendingEvent(AppModel):
    event_type: str
    stage: str
    payload: ProgressPayload = Field(default_factory=dict)
    schema_version: int = 1


class LeaseToken(AppModel):
    run_id: str
    owner_id: str
    lease_generation: int
    expires_at: datetime


class CleanupLease(AppModel):
    job_id: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    expires_at: datetime


class QueueStatus(AppModel):
    ahead: int = Field(ge=0)
    running: int = Field(ge=0)
    capacity: int = Field(ge=0)


class RunDetail(AppModel):
    display_title: str = ""
    id: str
    kind: Literal["research", "review"] = "research"
    question: str
    source: str
    status: RunStatus
    config_snapshot: RunConfigurationSnapshot = Field(default_factory=dict)
    current_step: str = ""
    attempt: int = 0
    lease_generation: int = 0
    lease_owner: str = ""
    lease_expires_at: datetime | None = None
    last_event_sequence: int = 0
    error_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    deadline_at: datetime
    started_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime
    result_count: int = 0
    queue: QueueStatus | None = None
    versions: RunVersions


class PersistedEvent(AppModel):
    run_id: str
    sequence: int
    event_type: str
    stage: str
    occurred_at: datetime
    schema_version: int
    payload: ProgressPayload = Field(default_factory=dict)


class PersistedCheckpoint(AppModel):
    run_id: str
    step_key: str
    idempotency_key: str
    event_sequence: int
    state: dict[str, Any] = Field(default_factory=dict)
    attempt: int
    lease_generation: int
    workflow_version: str
    schema_version: int
    created_at: datetime
    state_artifact_id: str = ""


class PersistedResult(AppModel):
    run_id: str
    rank: int
    record: FinalPaperResult
    record_artifact_id: str


class ResultPage(AppModel):
    results: list[PersistedResult] = Field(default_factory=list)
    next_cursor: str | None = None
    total_count: int = 0
    matched_count: int = 0
    scored_count: int = 0
    high_score_count: int = 0
    max_score: float | None = None


class HistoryItem(AppModel):
    display_title: str = ""
    id: str
    question: str
    source: str
    status: RunStatus
    result_count: int = Field(ge=0)
    error_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None = None


class HistoryPage(AppModel):
    items: list[HistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None
    matched_count: int = 0


class RecordRename(AppModel):
    title: str = Field(min_length=1, max_length=200, pattern=r"\S")


class ArtifactDescriptor(AppModel):
    id: str
    kind: str
    filename: str
    media_type: str
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    schema_version: int = Field(ge=1)
    created_at: datetime
    download_url: str


class ArtifactContent(AppModel):
    id: str
    filename: str
    media_type: str
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    content: bytes


class RunAudit(AppModel):
    query_portfolio: list[dict[str, Any]] = Field(default_factory=list)
    loop_decisions: list[dict[str, Any]] = Field(default_factory=list)
    candidate_queue: dict[str, Any] = Field(default_factory=dict)
    candidate_lifecycle: dict[str, int] = Field(default_factory=dict)
    recovery: dict[str, int] = Field(default_factory=dict)


class DeletionReceipt(AppModel):
    run_id: str
    status: str = "cleanup_pending"
    deleted_at: datetime
