"""Community HTTP adapter for saved analyses and explicitly requested networks."""

from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Response

from paperseek_app.bibliometrics.models import (
    AnalysisCreate,
    AnalysisDetail,
    AnalysisError,
    AnalysisPage,
    AnalysisRename,
)
from paperseek_app.bibliometrics.services import AnalysisService
from paperseek_app.errors import ArtifactError, RunNotFound
from paperseek_community.api_dependencies import ApiError, open_query_service
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.journal_tables import JournalTableStore
from paperseek_community.persistence.sqlite_bibliometrics import SQLiteAnalysisStore
from paperseek_core.bibliometrics.models import MetricNetwork, NetworkRequest


def cleanup_analyses(database: Path) -> None:
    store = SQLiteAnalysisStore(database)
    try:
        AnalysisService(store, FileArtifactStore(database.parent)).cleanup_deleted()
    finally:
        store.close()


def register_bibliometric_routes(app: FastAPI, *, database: Path) -> None:
    computation = BoundedSemaphore(1)
    rankings = JournalTableStore(database.parent)

    def service():
        store = SQLiteAnalysisStore(database)
        runs, run_store = open_query_service(database)
        try:
            yield AnalysisService(store, FileArtifactStore(database.parent), runs, rankings=rankings.table)
        except AnalysisError as exc:
            raise ApiError(exc.status, exc.code, exc.code) from exc
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Source search not found.") from exc
        except ArtifactError as exc:
            raise ApiError(
                500, "analysis_artifact_unavailable", "Analysis files are unavailable."
            ) from exc
        finally:
            store.close()
            run_store.close()

    @contextmanager
    def bounded():
        if not computation.acquire(blocking=False):
            raise ApiError(
                503,
                "analysis_busy",
                "Another analysis is being prepared. Retry shortly.",
            )
        try:
            yield
        finally:
            computation.release()

    Service = Annotated[AnalysisService, Depends(service)]

    @app.post("/api/v1/bibliometrics", status_code=201, response_model=AnalysisDetail)
    def create(
        request: AnalysisCreate,
        engine: Service,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=1, max_length=200
        ),
    ):
        with bounded():
            return engine.create(request, idempotency_key)

    @app.get("/api/v1/bibliometrics", response_model=AnalysisPage)
    def list_analyses(
        engine: Service,
        q: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=100),
    ):
        return engine.store.list_analyses(q, offset, limit)

    @app.get("/api/v1/bibliometrics/{identity}", response_model=AnalysisDetail)
    def get(identity: str, engine: Service):
        return engine.get(identity)

    @app.patch("/api/v1/bibliometrics/{identity}", status_code=204)
    def rename(identity: str, request: AnalysisRename, engine: Service):
        if not request.title.strip():
            raise AnalysisError("analysis_title_required", 422)
        engine.store.rename_analysis(identity, request.title.strip())
        return Response(status_code=204)

    @app.post("/api/v1/bibliometrics/{identity}/network", response_model=MetricNetwork)
    def build_network(identity: str, request: NetworkRequest, engine: Service):
        with bounded():
            return engine.network(identity, request)

    @app.get("/api/v1/bibliometrics/{identity}/export")
    def export(identity: str, engine: Service, format: Literal["csv", "json"] = "json"):
        return Response(
            engine.export(identity, format),
            media_type="text/csv" if format == "csv" else "application/json",
            headers={
                "Content-Disposition": f'attachment; filename="bibliometrics.{format}"'
            },
        )

    @app.delete("/api/v1/bibliometrics/{identity}", status_code=204)
    def delete(identity: str, engine: Service):
        engine.delete(identity)
        return Response(status_code=204)
