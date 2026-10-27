from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from paperseek_app.api_schemas import DiagnosticOutput, SourceListOutput
from paperseek_app.settings import (
    SettingsError,
)
from paperseek_app.source_catalog import list_source_descriptors
from paperseek_app.submission import DiagnosticSubmission, RunSubmission
from paperseek_community.api_dependencies import ApiError, open_query_service
from paperseek_community.diagnostics import run_configuration_diagnostics
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import (
    WorkspaceSettingsStore,
)


def register_system_routes(
    app: FastAPI,
    *,
    database: Path,
    settings: CommunityDeploymentSettings,
    workspace: WorkspaceSettingsStore,
) -> None:
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": "1.0.0b5"}

    @app.get("/ready")
    def ready() -> dict:
        if app.state.worker_required:
            task = app.state.worker_task
            if task is None or task.done():
                raise ApiError(503, "executor_unavailable", "The embedded executor is not running.")
        run_service, uow = open_query_service(database)
        try:
            run_service.get_history_page(page_size=1)
            return {"status": "ready"}
        finally:
            uow.close()

    @app.get("/api/v1/sources", response_model=SourceListOutput)
    def get_sources() -> SourceListOutput:
        return {"sources": list_source_descriptors()}

    @app.post("/api/v1/diagnostics", response_model=DiagnosticOutput)
    def diagnostics(submission: DiagnosticSubmission) -> dict[str, object]:
        if submission.service is not None:
            raise ApiError(422, "unsupported_service_mode", "Hosted service modes are not available in Community.")
        try:
            resolved = workspace.apply_to_submission(
                RunSubmission.model_validate({**submission.model_dump(), "question": "diagnostics"}),
            )
        except SettingsError as exc:
            raise ApiError(exc.status, exc.code, exc.code) from exc
        return run_configuration_diagnostics(resolved.model_dump(mode="json"), settings)
