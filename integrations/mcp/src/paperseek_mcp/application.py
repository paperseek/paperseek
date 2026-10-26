from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import json
from pathlib import Path
from uuid import uuid4

from paperseek_app.execution import WorkflowExecution
from paperseek_app.models import RunStatus
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.journal_tables import JournalTableStore
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_app.submission import RunSubmission, prepare_run_submission
from paperseek_app.source_catalog import (
    SOURCE_BY_ID,
    list_source_descriptors,
)
from paperseek_app.worker import RunExecutor
from paperseek_core.ports import CancellationToken


TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNINGS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class CommunityMCPApplication:
    """Thin Community composition over the durable application services."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        deployment_settings: CommunityDeploymentSettings | None = None,
        credential_store: EncryptedRunCredentialStore | None = None,
        execution_resolver: Callable[[object, CancellationToken], WorkflowExecution] | None = None,
    ) -> None:
        self.database = Path(database_path).expanduser().resolve()
        migrate_database(self.database)
        self.settings = deployment_settings or CommunityDeploymentSettings.from_environment()
        self.credentials = credential_store or EncryptedRunCredentialStore(self.database)
        self.execution_resolver = execution_resolver or CommunityExecutionResolver(
            self.settings,
            self.credentials,
            journal_tables=JournalTableStore(self.database.parent),
        )

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        worker_uow = SQLiteRunUnitOfWork(self.database)
        executor = RunExecutor(
            RunExecutionService(worker_uow, FileArtifactStore(self.database.parent)),
            self.execution_resolver,
            owner_id=f"community-mcp-{uuid4()}",
        )
        stop = asyncio.Event()

        async def work() -> None:
            while not stop.is_set():
                detail = await executor.run_once()
                if detail is not None and detail.status in TERMINAL_STATUSES:
                    self.credentials.delete(detail.id)
                if detail is None:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.05)
                    except TimeoutError:
                        pass

        worker_task = asyncio.create_task(work())
        try:
            yield
        finally:
            stop.set()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            worker_uow.close()

    def start_search(
        self,
        *,
        question: str,
        source: str = "openalex",
        search_budget: str = "fast",
        field_ids: list[str] | None = None,
        field_hint: str = "",
        citation_enabled: bool | None = None,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        prepared = prepare_run_submission(
            RunSubmission(
                question=question,
                source=source,
                search_budget=search_budget,
                field_ids=field_ids or [],
                field_hint=field_hint,
                citation_enabled=citation_enabled,
                            ),
            self.settings,
        )
        key = idempotency_key.strip() or str(uuid4())
        if len(key) > 200:
            raise ValueError("The idempotency key must not exceed 200 characters.")
        service, uow = self._submission_service()
        try:
            detail = service.submit_run(
                subject="community",
                idempotency_key=key,
                request_digest=prepared.request_digest,
                question=prepared.question,
                source=prepared.source,
                config_snapshot=prepared.config_snapshot,
            )
            return {"run_id": detail.id, "status": detail.status.value}
        finally:
            uow.close()

    def get_search_status(self, *, run_id: str) -> dict[str, object]:
        service, uow = self._query_service()
        try:
            return service.get_run(run_id).model_dump(mode="json")
        finally:
            uow.close()

    def get_search_events(
        self,
        *,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, object]:
        if after_sequence < 0:
            raise ValueError("The event cursor must not be negative.")
        if limit < 1 or limit > 200:
            raise ValueError("Event limit must be between 1 and 200.")
        service, uow = self._query_service()
        try:
            events = service.list_events(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            next_sequence = events[-1].sequence if events else after_sequence
            return {
                "events": [event.model_dump(mode="json") for event in events],
                "next_sequence": next_sequence,
            }
        finally:
            uow.close()

    def get_search_results(
        self,
        *,
        run_id: str,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> dict[str, object]:
        service, uow = self._query_service()
        try:
            page = service.get_result_page(run_id, cursor=cursor, page_size=page_size)
            return {
                "results": [
                    {
                        **item.record.model_dump(mode="json"),
                    }
                    for item in page.results
                ],
                "next_cursor": page.next_cursor,
            }
        finally:
            uow.close()

    def get_citation_graph(self, *, run_id: str) -> dict[str, object]:
        service, uow = self._query_service()
        try:
            content = service.get_citation_map(run_id)
            payload = json.loads(content.content)
            if not isinstance(payload, dict):
                raise ValueError("The citation graph artifact is not an object.")
            return payload
        finally:
            uow.close()

    def cancel_search(self, *, run_id: str) -> dict[str, object]:
        service, uow = self._submission_service()
        try:
            return service.request_cancel(run_id).model_dump(mode="json")
        finally:
            uow.close()

    def export_results(self, *, run_id: str, format: str = "json") -> dict[str, object]:
        service, uow = self._query_service()
        try:
            content = service.get_export(run_id, format)
            return {
                "filename": content.filename,
                "media_type": content.media_type,
                "sha256": content.sha256,
                "size": content.size,
                "content": content.content.decode("utf-8"),
            }
        finally:
            uow.close()

    def list_sources(self) -> dict[str, object]:
        return {"sources": list_source_descriptors()}

    def get_source_capabilities(self, *, source: str) -> dict[str, object]:
        source_definition = SOURCE_BY_ID.get(source)
        if source_definition is None:
            raise ValueError("The selected source is not supported.")
        return source_definition.descriptor()

    def _submission_service(self) -> tuple[RunSubmissionService, SQLiteRunUnitOfWork]:
        uow = SQLiteRunUnitOfWork(self.database)
        return RunSubmissionService(uow), uow

    def _query_service(self) -> tuple[RunQueryService, SQLiteRunUnitOfWork]:
        uow = SQLiteRunUnitOfWork(self.database)
        return RunQueryService(uow, FileArtifactStore(self.database.parent)), uow


__all__ = ["CommunityMCPApplication"]
