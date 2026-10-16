from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Header

from paperseek_app.errors import (
    ActiveRunExists,
    IdempotencyConflict,
    RunNotFound,
    RunStateError,
    UnknownRunStep,
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
    open_retry_service,
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
        except ActiveRunExists as exc:
            if uow.get_run(exc.run_id).kind == "review":
                raise ApiError(409, "community_task_active", "A review task is already active.") from exc
            raise ApiError(
                409,
                "active_run_exists",
                "A run is already active.",
                {"run_id": exc.run_id},
            ) from exc
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


def _saved_connection_credentials(
    workspace: WorkspaceSettingsStore,
    settings: CommunityDeploymentSettings,
    run: RunDetail,
) -> dict[str, dict[str, str]]:
    """Rebuild the per-run keys from the saved connections the run started with.

    A finished run drops its per-run credentials, so a retry re-resolves them
    from the workspace exactly like a submission does. Kinds that no longer
    match a saved connection stay unresolved and fall back to the deployment
    configuration.
    """
    snapshot = run.config_snapshot or {}
    llm = snapshot.get("llm") or {}
    retrieval = snapshot.get("retrieval") or {}
    wanted: list[tuple[str, dict]] = [("llm", dict(llm))]
    for kind in ("embedding", "reranker", "decision"):
        route = retrieval.get(kind)
        if isinstance(route, dict) and route:
            wanted.append((kind, dict(route)))
    connections = workspace.snapshot(settings).get("connections") or []
    resolved: dict[str, dict[str, str]] = {}
    for kind, route in wanted:
        match = next(
            (
                item
                for item in connections
                if item.get("kind") == kind
                and item.get("provider") == route.get("provider")
                and item.get("base_url") == route.get("base_url")
                and item.get("model") == route.get("model")
            ),
            None,
        )
        if match is None:
            continue
        _, key = workspace.connection_secret(str(match["id"]), kind)
        if key:
            resolved[kind] = {"api_key": key}
    source = str(snapshot.get("source") or "")
    if source:
        key, _ = workspace.source_secret(source)
        if key:
            resolved["source"] = {"api_key": key}
    return resolved


def register_run_retry_route(
    app: FastAPI,
    *,
    database: Path,
    settings: CommunityDeploymentSettings,
    credentials: EncryptedRunCredentialStore,
    workspace: WorkspaceSettingsStore,
    execution_resolver: Callable[[object, CancellationToken], WorkflowExecution] | None,
    accept_submissions_without_worker: bool,
) -> None:
    @app.post(
        "/api/v1/runs/{run_id}/steps/{step}/retry",
        status_code=202,
        response_model=RunDetail,
    )
    def retry_run_step(run_id: str, step: str) -> RunDetail:
        if execution_resolver is None and not accept_submissions_without_worker:
            raise ApiError(503, "executor_unavailable", "The embedded executor is not configured.")
        run_service, uow = open_retry_service(
            database,
            credential_store=credentials,
            resolve_credentials=lambda run: _saved_connection_credentials(workspace, settings, run),
        )
        try:
            return run_service.retry_from_step(run_id, step).model_dump(mode="json")
        except UnknownRunStep as exc:
            raise ApiError(422, "unknown_run_step", "Unknown run step.") from exc
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except RunStateError as exc:
            raise ApiError(409, "run_not_retryable", "Run cannot be retried.") from exc
        except CredentialPersistenceError as exc:
            raise ApiError(
                500, "credential_persistence_failed", "Temporary credentials could not be stored safely.",
            ) from exc
        finally:
            uow.close()
