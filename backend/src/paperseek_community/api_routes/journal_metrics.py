"""Settings › Sources: the ShowJCR ranking tables PaperSeek labels and filters results with."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request

from paperseek_app.execution import run_blocking
from paperseek_community.api_dependencies import ApiError
from paperseek_community.journal_tables import MAX_TABLE_BYTES, JournalTableError, JournalTableStore
from paperseek_community.model_transport import create_http_client
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import WorkspaceSettingsStore


def register_journal_metric_routes(app: FastAPI, *, database: Path, settings: CommunityDeploymentSettings,
                                   workspace: WorkspaceSettingsStore) -> None:
    store = JournalTableStore(database.parent)

    @app.post("/api/v1/journal-metrics/refresh")
    async def refresh_journal_tables() -> dict:
        """Download the current ShowJCR edition of every table, through the workspace proxy when one is set."""
        client = create_http_client([], workspace.proxy_policy(settings))
        try:
            await store.download(client)
        except JournalTableError as exc:
            raise ApiError(502, str(exc), str(exc)) from exc
        finally:
            await client.aclose()
        return {"tables": await run_blocking(store.status)}

    @app.get("/api/v1/journal-metrics")
    async def journal_tables() -> dict:
        return {"tables": await run_blocking(store.status)}

    @app.put("/api/v1/journal-metrics/{platform}")
    async def import_journal_table(platform: str, request: Request, filename: str = "",
                                   year: int | None = Query(default=None, ge=1900, le=2100)) -> dict:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_TABLE_BYTES:
                raise ApiError(413, "journal_table_size", "The table is larger than 64 MB.")
        try:
            return await run_blocking(store.import_table, platform, bytes(content), filename=filename, year=year)
        except JournalTableError as exc:
            status = 404 if str(exc) == "journal_table_platform" else 422
            raise ApiError(status, str(exc), str(exc)) from exc
