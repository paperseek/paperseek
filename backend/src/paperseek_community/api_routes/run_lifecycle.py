from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Header

from paperseek_app.errors import (
    IdempotencyConflict,
    RunNotFound,
    RunStateError,
)
from paperseek_app.execution import WorkflowExecution
from paperseek_app.models import RunDetail
from paperseek_app.settings import (
    SettingsError,
)
from paperseek_app.submission import (
    RunSubmission,
    SubmissionValidationError,
    prepare_run_submission,
)
from paperseek_community.api_dependencies import (
    ApiError,
    open_query_service,
    open_submission_service,
)
from paperseek_community.credentials import (
    CredentialPersistenceError,
    EncryptedRunCredentialStore,
)
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import (
    WorkspaceSettingsStore,
)
from paperseek_core.ports import CancellationToken


def register_run_submission_and_status_routes(
    app: FastAPI,
    *,
    database: Path,
    settings: CommunityDeploymentSettings,
    credentials: EncryptedRunCredentialStore,
    workspace: WorkspaceSettingsStore,
    execution_resolver: Callable[[object, CancellationToken], WorkflowExecution] | None,
    accept_submissions_without_worker: bool,
) -> None:
    @app.post("/api/v1/runs", status_code=202, response_model=RunDetail)
    def submit_run(
        submission: RunSubmission,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> RunDetail:
        if submission.service is not None:
            raise ApiError(422, "unsupported_service_mode", "Hosted service modes are not available in Community.")
        if execution_resolver is None and not accept_submissions_without_worker:
            raise ApiError(503, "executor_unavailable", "The embedded executor is not configured.")
        try:
            submission = workspace.apply_to_submission(submission)
        except SettingsError as exc:
            raise ApiError(exc.status, exc.code, exc.code) from exc
        try:
            prepared = prepare_run_submission(submission, settings)
        except SubmissionValidationError as exc:
            raise ApiError(422, exc.code, exc.message) from exc
        run_service, uow = open_submission_service(
            database,
            credential_store=credentials,
            submission_credentials=submission.credentials.model_dump(mode="json"),
        )
        try:
            detail = run_service.submit_run(
                subject="community",
                idempotency_key=idempotency_key,
                request_digest=prepared.request_digest,
                question=prepared.question,
                source=prepared.source,
                config_snapshot=prepared.config_snapshot,
            )
            return detail.model_dump(mode="json")
        except CredentialPersistenceError as exc:
            raise ApiError(
                500, "credential_persistence_failed", "Temporary credentials could not be stored safely.",
            ) from exc
        except IdempotencyConflict as exc:
            raise ApiError(409, "idempotency_conflict", "The idempotency key belongs to another request.") from exc
        finally:
            uow.close()

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str) -> RunDetail:
        run_service, uow = open_query_service(database)
        try:
            return run_service.get_run(run_id).model_dump(mode="json")
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        finally:
            uow.close()


def register_run_cancellation_route(app: FastAPI, *, database: Path) -> None:
    @app.post("/api/v1/runs/{run_id}/cancel", status_code=202, response_model=RunDetail)
    def cancel_run(run_id: str) -> RunDetail:
        run_service, uow = open_submission_service(database)
        try:
            return run_service.request_cancel(run_id).model_dump(mode="json")
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except RunStateError as exc:
            raise ApiError(409, "run_not_cancellable", "Run cannot be cancelled.") from exc
        finally:
            uow.close()
