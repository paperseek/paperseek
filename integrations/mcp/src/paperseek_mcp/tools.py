from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Annotated, Any, Literal, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from paperseek_app.errors import (
    ArtifactNotFound,
    InvalidCursor,
    InvalidResultQuery,
    RunNotFound,
    RunStateError,
)
from paperseek_app.submission import SubmissionValidationError


logger = logging.getLogger(__name__)
RunId = Annotated[str, Field(min_length=1, description="The run_id returned by start_search.")]
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _call_application(operation: Callable[..., dict], **values: Any) -> dict[str, Any]:
    """Expose recoverable errors without sending internal exceptions to the host."""
    try:
        return operation(**values)
    except (
        SubmissionValidationError,
        RunNotFound,
        InvalidCursor,
        InvalidResultQuery,
        RunStateError,
        ArtifactNotFound,
    ) as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(
            "Invalid arguments or local configuration. Check the tool parameters "
            "and run paperseek doctor --local."
        ) from exc
    except Exception as exc:
        logger.exception("MCP application operation failed: %s", operation.__name__)
        raise ToolError("PaperSeek could not complete this operation. Check the server log.") from exc


TOOL_NAMES = (
    "start_search",
    "get_search_status",
    "get_search_events",
    "get_search_results",
    "get_citation_graph",
    "cancel_search",
    "export_results",
    "list_sources",
    "get_source_capabilities",
)


class PaperSeekApplication(Protocol):
    def start_search(self, **values) -> dict: ...
    def get_search_status(self, **values) -> dict: ...
    def get_search_events(self, **values) -> dict: ...
    def get_search_results(self, **values) -> dict: ...
    def get_citation_graph(self, **values) -> dict: ...
    def cancel_search(self, **values) -> dict: ...
    def export_results(self, **values) -> dict: ...
    def list_sources(self) -> dict: ...
    def get_source_capabilities(self, **values) -> dict: ...


def register_tools(server: MCPServer, application: PaperSeekApplication) -> None:
    @server.tool(
        name="start_search",
        title="Start literature search",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    def start_search(
        question: Annotated[
            str, Field(min_length=1, max_length=10_000, description="Research question to search for.")
        ],
        source: Annotated[str, Field(description="Source ID from list_sources.")] = "openalex",
        search_budget: Literal["fast", "medium", "high", "max"] = "fast",
        field_ids: Annotated[
            list[str] | None, Field(description="Filter IDs from get_source_capabilities.")
        ] = None,
        field_hint: Annotated[str, Field(description="Optional research-field context.")] = "",
        citation_enabled: Annotated[
            bool | None, Field(description="Enable citation expansion when supported by the source.")
        ] = None,
        idempotency_key: Annotated[
            str,
            Field(
                max_length=200,
                description="Reuse the same nonempty key and arguments when retrying a submission.",
            ),
        ] = "",
    ) -> dict[str, Any]:
        """Start a local search using configured provider credentials; may incur API charges.

        Returns run_id and status immediately. Poll get_search_status, then read
        get_search_results. Runs remain in the local database until deleted by
        the user. Retrying without an idempotency_key can create another run.
        """
        return _call_application(
            application.start_search,
            question=question,
            source=source,
            search_budget=search_budget,
            field_ids=field_ids or [],
            field_hint=field_hint,
            citation_enabled=citation_enabled,
            idempotency_key=idempotency_key,
        )

    @server.tool(
        name="get_search_status", title="Search status", structured_output=True, annotations=READ_ONLY
    )
    def get_search_status(run_id: RunId) -> dict[str, Any]:
        """Poll every few seconds until completed, completed_with_warnings, failed, or cancelled."""
        return _call_application(application.get_search_status, run_id=run_id)

    @server.tool(
        name="get_search_events", title="Search progress", structured_output=True, annotations=READ_ONLY
    )
    def get_search_events(
        run_id: RunId,
        after_sequence: Annotated[
            int,
            Field(
                ge=0, description="Use next_sequence from the previous response; 0 starts at the beginning."
            ),
        ] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        """Return persisted events and next_sequence. An empty page does not mean the run is finished."""
        return _call_application(
            application.get_search_events,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    @server.tool(
        name="get_search_results", title="Search results", structured_output=True, annotations=READ_ONLY
    )
    def get_search_results(
        run_id: RunId,
        cursor: Annotated[
            str | None,
            Field(description="Opaque next_cursor from the previous page; omit for the first page."),
        ] = None,
        page_size: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Return a cursor-paginated page of persisted search results."""
        return _call_application(
            application.get_search_results,
            run_id=run_id,
            cursor=cursor,
            page_size=page_size,
        )

    @server.tool(
        name="get_citation_graph", title="Citation graph", structured_output=True, annotations=READ_ONLY
    )
    def get_citation_graph(run_id: RunId) -> dict[str, Any]:
        """Return the persisted citation graph for a completed run."""
        return _call_application(application.get_citation_graph, run_id=run_id)

    @server.tool(
        name="cancel_search",
        title="Cancel search",
        structured_output=True,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def cancel_search(run_id: RunId) -> dict[str, Any]:
        """Request logical cancellation of a queued or running search."""
        return _call_application(application.cancel_search, run_id=run_id)

    @server.tool(name="export_results", title="Export results", structured_output=True, annotations=READ_ONLY)
    def export_results(run_id: RunId, format: Literal["json", "csv", "bibtex", "ris"] = "json") -> dict[str, Any]:
        """Return a complete persisted export: JSON, CSV, or BibTeX / RIS for Zotero, EndNote and JabRef."""
        return _call_application(application.export_results, run_id=run_id, format=format)

    @server.tool(
        name="list_sources", title="Literature sources", structured_output=True, annotations=READ_ONLY
    )
    def list_sources() -> dict[str, Any]:
        """List all supported literature sources and their descriptors."""
        return _call_application(application.list_sources)

    @server.tool(
        name="get_source_capabilities", title="Source options", structured_output=True, annotations=READ_ONLY
    )
    def get_source_capabilities(
        source: Annotated[str, Field(min_length=1, description="Source ID from list_sources.")],
    ) -> dict[str, Any]:
        """Return the descriptor and capabilities for one source."""
        return _call_application(application.get_source_capabilities, source=source)


__all__ = ["PaperSeekApplication", "TOOL_NAMES", "register_tools"]
