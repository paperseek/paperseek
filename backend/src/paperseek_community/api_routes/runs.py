from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI

from paperseek_app.execution import WorkflowExecution
from paperseek_community.api_routes.run_artifacts import register_run_artifact_routes
from paperseek_community.api_routes.run_events import register_run_event_routes
from paperseek_community.api_routes.run_lifecycle import (
    register_run_cancellation_route,
    register_run_retry_route,
    register_run_submission_and_status_routes,
)
from paperseek_community.api_routes.run_results import register_run_result_routes
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import (
    WorkspaceSettingsStore,
)
from paperseek_core.ports import CancellationToken


def register_run_routes(
    app: FastAPI,
    *,
    database: Path,
    settings: CommunityDeploymentSettings,
    credentials: EncryptedRunCredentialStore,
    workspace: WorkspaceSettingsStore,
    execution_resolver: Callable[[object, CancellationToken], WorkflowExecution] | None,
    accept_submissions_without_worker: bool,
) -> None:
    register_run_submission_and_status_routes(
        app,
        database=database,
        settings=settings,
        credentials=credentials,
        workspace=workspace,
        execution_resolver=execution_resolver,
        accept_submissions_without_worker=accept_submissions_without_worker,
    )
    register_run_event_routes(app, database=database)
    register_run_result_routes(app, database=database)
    register_run_artifact_routes(app, database=database)
    register_run_cancellation_route(app, database=database)
    register_run_retry_route(
        app,
        database=database,
        settings=settings,
        credentials=credentials,
        workspace=workspace,
        execution_resolver=execution_resolver,
        accept_submissions_without_worker=accept_submissions_without_worker,
    )
