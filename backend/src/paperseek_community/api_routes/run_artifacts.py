from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response

from paperseek_app.errors import ArtifactNotFound, RunNotFound
from paperseek_community.api_dependencies import ApiError, content_response, open_query_service


def register_run_artifact_routes(app: FastAPI, *, database: Path) -> None:
    @app.get("/api/v1/runs/{run_id}/artifacts")
    def get_artifacts(run_id: str) -> dict:
        run_service, uow = open_query_service(database)
        try:
            return {
                "artifacts": [
                    item.model_dump(mode="json")
                    for item in run_service.list_user_artifacts(run_id)
                ]
            }
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        finally:
            uow.close()

    @app.get("/api/v1/runs/{run_id}/artifacts/{artifact_id}")
    def download_artifact(run_id: str, artifact_id: str) -> Response:
        run_service, uow = open_query_service(database)
        try:
            return content_response(
                run_service.read_user_artifact(run_id, artifact_id),
                disposition=True,
            )
        except (RunNotFound, ArtifactNotFound) as exc:
            raise ApiError(404, "artifact_not_found", "Artifact not found.") from exc
        finally:
            uow.close()

    @app.get("/api/v1/runs/{run_id}/export")
    def export_run(run_id: str, format: str) -> Response:
        run_service, uow = open_query_service(database)
        try:
            return content_response(
                run_service.get_export(run_id, format),
                disposition=True,
            )
        except RunNotFound as exc:
            raise ApiError(404, "run_not_found", "Run not found.") from exc
        except ArtifactNotFound as exc:
            raise ApiError(404, "artifact_not_found", "Export not found.") from exc
        except ValueError as exc:
            raise ApiError(400, "invalid_export_format", "Export format must be csv, json, bibtex or ris.") from exc
        finally:
            uow.close()
