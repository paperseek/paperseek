from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from paperseek_app.api_schemas import ErrorResponse
from paperseek_app.execution import WorkflowExecution, run_blocking
from paperseek_app.models import RunDetail
from paperseek_app.review.execution import ReviewExecution, ReviewExecutor
from paperseek_app.services import RunExecutionService
from paperseek_app.worker import RunExecutor
from paperseek_community import api_dependencies
from paperseek_community.api_routes import history as history_routes
from paperseek_community.api_routes import runs as run_routes
from paperseek_community.api_routes import system as system_routes
from paperseek_community.api_routes.bibliometrics import (
    cleanup_analyses,
    register_bibliometric_routes,
)
from paperseek_community.api_routes.review_events import register_review_events
from paperseek_community.api_routes.reviews import register_review_routes
from paperseek_community.api_routes.settings import register_settings_routes
from paperseek_community.api_routes.workspace_data import register_workspace_data_routes
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.persistence import migrate_database
from paperseek_community.persistence.sqlite_review_jobs import SQLiteReviewUnitOfWork
from paperseek_community.review_composition import CommunityReviewResolver
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import (
    WorkspaceSettingsStore,
)
from paperseek_core.ports import CancellationToken


class SpaStaticFiles(StaticFiles):
    @staticmethod
    def _is_spa_route(path: str) -> bool:
        parts = [part for part in path.replace("\\", "/").strip("/").split("/") if part]
        if not parts or parts == ["search"] or parts == ["history"] or parts == ["reviews"] or parts == ["bibliometrics"]:
            return True
        if len(parts) == 2 and parts[0] in {"reviews", "bibliometrics"}:
            return True
        if parts == ["settings"] or (
            len(parts) == 2 and parts[0] == "settings"
            and parts[1] in {"models", "sources", "general", "user"}
        ):
            return True
        return (
            len(parts) in {2, 3}
            and parts[0] == "runs"
            and (len(parts) == 2 or parts[2] in {"results", "citation-map"})
        )

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (
                exc.status_code == 404
                and scope["method"] in {"GET", "HEAD"}
                and self._is_spa_route(path)
            ):
                return await super().get_response("index.html", scope)
            raise
        if (
            response.status_code == 404
            and scope["method"] in {"GET", "HEAD"}
            and self._is_spa_route(path)
        ):
            return await super().get_response("index.html", scope)
        return response


def create_app(
    database_path: Path | str,
    *,
    execution_resolver: Callable[[object, CancellationToken], WorkflowExecution] | None = None,
    accept_submissions_without_worker: bool = False,
    static_dir: Path | str | None = None,
    deployment_settings: CommunityDeploymentSettings | None = None,
    credential_store: EncryptedRunCredentialStore | None = None,
    review_execution_resolver: Callable[[RunDetail, CancellationToken], ReviewExecution] | None = None,
) -> FastAPI:
    database = Path(database_path).expanduser().resolve()
    migrate_database(database)
    settings = deployment_settings or CommunityDeploymentSettings.from_environment()
    credentials = credential_store or EncryptedRunCredentialStore(database)
    workspace = WorkspaceSettingsStore(database, credentials)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await run_blocking(api_dependencies.drain_cleanup_jobs, database)
        await run_blocking(cleanup_analyses, database)
        worker_uow = None
        worker_task = None
        stop = asyncio.Event()
        if execution_resolver is not None:
            worker_uow = SQLiteReviewUnitOfWork(database, claim_kind=None)
            run_service = RunExecutionService(worker_uow, FileArtifactStore(database.parent))
            executor = RunExecutor(
                run_service,
                execution_resolver,
                owner_id=f"community-embedded-{uuid4()}",
                review_executor=ReviewExecutor(worker_uow, run_service,
                    review_execution_resolver or CommunityReviewResolver(
                    settings, credentials, proxy=lambda: workspace.proxy_policy(settings))),
            )

            async def finished(detail: RunDetail) -> None:
                if detail.status in api_dependencies.TERMINAL_STATUSES:
                    await run_blocking(credentials.delete, detail.id)

            worker_task = asyncio.create_task(executor.serve(
                max_concurrent=settings.max_concurrent_runs, poll_seconds=0.05, stop=stop, on_finished=finished))
            _app.state.worker_task = worker_task
        try:
            yield
        finally:
            stop.set()
            try:
                if worker_task is not None:
                    worker_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker_task
            finally:
                if worker_uow is not None:
                    await run_blocking(worker_uow.close)

    app = FastAPI(
        title="PaperSeek Community API",
        version="1.0.0b2",
        responses={status: {"model": ErrorResponse} for status in (400, 404, 409, 422, 500, 503)},
        lifespan=lifespan,
    )
    app.state.worker_required = execution_resolver is not None
    app.state.worker_task = None

    api_dependencies.register_error_handlers(app)
    system_routes.register_system_routes(app, database=database, settings=settings, workspace=workspace)
    register_settings_routes(app, settings=settings, store=workspace)
    run_routes.register_run_routes(
        app,
        database=database,
        settings=settings,
        credentials=credentials,
        workspace=workspace,
        execution_resolver=execution_resolver,
        accept_submissions_without_worker=accept_submissions_without_worker,
    )
    history_routes.register_history_routes(app, database=database)
    register_review_routes(app, database=database, settings=settings, credentials=credentials, workspace=workspace,
                           accept_jobs=execution_resolver is not None or accept_submissions_without_worker)
    register_review_events(app, database=database, credentials=credentials)
    register_bibliometric_routes(app, database=database)
    register_workspace_data_routes(app, database=database, credentials=credentials)

    if static_dir is not None:
        root = Path(static_dir).expanduser().resolve()
        if not (root / "index.html").is_file():
            raise ValueError("The Community static directory must contain index.html.")
        app.mount("/", SpaStaticFiles(directory=root, html=True), name="community-web")

    return app
