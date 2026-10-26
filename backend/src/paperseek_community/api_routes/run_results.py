from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import Response

from paperseek_app.errors import ArtifactNotFound, InvalidCursor, InvalidResultQuery, RunNotFound
from paperseek_app.models import RunAudit
from paperseek_community.api_dependencies import ApiError, content_response, open_query_service
from paperseek_app.api_schemas import CitationMapOutput, ResultPageOutput, SelectedExportInput


def register_run_result_routes(app: FastAPI, *, database: Path) -> None:
    @app.get("/api/v1/runs/{run_id}/results", response_model=ResultPageOutput)
    def get_results(
        run_id: str,
        cursor: str | None = None,
        page_size: int = Query(default=50, ge=1, le=200),
        query: str | None = None,
        min_score: float | None = None,
        metadata: str | None = None,
        sort: str = "rank",
        journal: str | None = None,
        acceptance: str | None = None,
    ) -> ResultPageOutput:
        run_service, uow = open_query_service(database)
        try:
            page = run_service.get_result_page(
                run_id,
                cursor=cursor,
                page_size=page_size,
                query=query,
                min_score=min_score,
                metadata=metadata,
                sort=sort,
                journal=journal,
                acceptance=acceptance,
            )
            return {
                "results": [
                    {
                        **item.record.model_dump(mode="json"),
                    }
                    for item in page.results
                ],
                "next_cursor": page.next_cursor,
                "total_count": page.total_count,
                "matched_count": page.matched_count,
                "scored_count": page.scored_count,
                "high_score_count": page.high_score_count,
                "max_score": page.max_score,
            }
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except InvalidCursor as exc:
            raise ApiError(400, "invalid_cursor", "The result cursor is invalid.") from exc
        except InvalidResultQuery as exc:
            raise ApiError(400, "invalid_result_query", str(exc) or "The result query is invalid.") from exc
        finally:
            uow.close()

    @app.post("/api/v1/runs/{run_id}/export-selected")
    def export_selected(run_id: str, payload: SelectedExportInput) -> Response:
        run_service, uow = open_query_service(database)
        try:
            content = run_service.get_selected_export(run_id, payload.format, payload.uids)
            return content_response(content, disposition=True)
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except InvalidResultQuery as exc:
            raise ApiError(400, "invalid_result_query", str(exc) or "The selection is invalid.") from exc
        finally:
            uow.close()

    @app.get("/api/v1/runs/{run_id}/citation-map", response_model=CitationMapOutput)
    def get_citation_map(run_id: str) -> Response:
        run_service, uow = open_query_service(database)
        try:
            return content_response(run_service.get_citation_map(run_id))
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except ArtifactNotFound as exc:
            raise ApiError(404, "artifact_not_found", "Citation map not found.") from exc
        finally:
            uow.close()

    @app.get("/api/v1/runs/{run_id}/audit", response_model=RunAudit)
    def get_run_audit(run_id: str) -> RunAudit:
        run_service, uow = open_query_service(database)
        try:
            return run_service.get_run_audit(run_id)
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except ArtifactNotFound as exc:
            raise ApiError(404, "artifact_not_found", "Run audit not found.") from exc
        finally:
            uow.close()

    @app.get("/api/v1/runs/{run_id}/logs")
    def get_run_logs(run_id: str) -> Response:
        run_service, uow = open_query_service(database)
        try:
            content = run_service.get_run_log(run_id)
            return Response(
                content=content,
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="paperseek-{run_id}.log"'},
            )
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        finally:
            uow.close()
