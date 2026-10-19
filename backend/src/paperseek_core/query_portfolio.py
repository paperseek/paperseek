"""Execution of optional structured-query portfolio views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from paperseek_core.document_identity import document_key
from paperseek_core.models import (
    CoreProgressEvent,
    QueryHistoryEntry,
    QueryLoopBudget,
    QueryLoopResumeState,
    QueryPortfolioExecution,
    QueryViewSelection,
)
from paperseek_core.ports import CancellationToken, SourcePort, WorkflowObserver
from paperseek_core.query_compiler import SUPPORTED_QUERY_SOURCES, validate_source_query
from paperseek_core.query_planning import (
    QuerySelection,
    normalize_query,
    select_query_views,
)
from paperseek_core.retrieval_fusion import dedupe_documents
from paperseek_core.sources.contracts import SourceSearchRequest


CheckpointWriter = Callable[
    [QueryLoopResumeState, str, str],
    Awaitable[QueryLoopResumeState],
]


class QueryPortfolioExecutor:
    """Materialize selected query views without changing the primary query loop."""

    def __init__(
        self,
        source: SourcePort,
        observer: WorkflowObserver,
        checkpoint: CheckpointWriter,
        cancellation: CancellationToken,
    ) -> None:
        self.source = source
        self.observer = observer
        self.checkpoint = checkpoint
        self.cancellation = cancellation

    async def run(
        self,
        state: QueryLoopResumeState,
        budget: QueryLoopBudget,
    ) -> QueryLoopResumeState:
        if (
            not budget.query_portfolio_enabled
            or not state.search_plan.query_views
            or self.source.name not in SUPPORTED_QUERY_SOURCES
        ):
            return state.model_copy(update={"phase": "query_portfolio"})
        selections = select_query_views(
            state.search_plan,
            self.source.name,
            state.current_query,
            budget.query_portfolio_max,
        )
        public_selections = [
            QueryViewSelection(
                source=item.source,
                view_id=item.view.view_id,
                view_type=item.view.view_type,
                selected=item.selected,
                score=round(item.score, 6),
                lexical_overlap=round(item.lexical_overlap, 6),
                max_pairwise_overlap=round(item.max_pairwise_overlap, 6),
                reason_codes=item.reason_codes,
                compile_error=item.compile_error,
                compiled_query=item.compiled_query,
                original_index=item.original_index,
            )
            for item in selections
        ]
        completed_ids = {item.view_id for item in state.query_portfolio}
        requests_started = sum(item.status == "completed" for item in state.query_portfolio)
        seen_queries = {
            normalize_query(value)
            for value in [
                state.current_query,
                *[item.query for item in state.history],
                *[item.next_query for item in state.history],
            ]
            if value
        }
        combined = dedupe_documents(
            [
                *state.primary_hits,
                *[
                    record
                    for records in state.portfolio_lane_results.values()
                    for record in records
                ],
            ]
        )
        seen_keys = {document_key(record) for record in combined if document_key(record)}
        current = state.model_copy(update={"query_view_selection": public_selections})
        for selection in sorted(
            selections,
            key=lambda item: (-item.score, item.original_index),
        ):
            self.cancellation.raise_if_cancelled()
            view = selection.view
            if view.view_id in completed_ids:
                continue
            execution: QueryPortfolioExecution
            if selection.compile_error:
                execution = self._execution(
                    selection,
                    status="failed",
                    error="query_view_compile_failed",
                )
            elif "exact_primary_duplicate" in selection.reason_codes:
                execution = self._execution(selection, status="skipped_duplicate")
            elif requests_started >= budget.query_portfolio_max:
                execution = self._execution(
                    selection,
                    status="skipped_budget",
                    reasons=self._merge_unique(
                        selection.reason_codes,
                        ["query_view_request_budget"],
                    ),
                )
            else:
                query = selection.compiled_query
                normalized = normalize_query(query)
                if not normalized or normalized in seen_queries:
                    execution = self._execution(selection, status="skipped_duplicate")
                else:
                    seen_queries.add(normalized)
                    requests_started += 1
                    try:
                        validation = validate_source_query(self.source.name, query)
                        if not validation.ok:
                            raise ValueError(validation.issue)
                        self._event(
                            "query.portfolio_request_started",
                            {
                                "view_id": view.view_id,
                                "limit": budget.query_portfolio_limit,
                            },
                        )
                        response = await self.source.search(
                            SourceSearchRequest(
                                query=query,
                                limit=budget.query_portfolio_limit,
                                page=1,
                                lane="relevance",
                                field_ids=budget.field_ids,
                            )
                        )
                        self.cancellation.raise_if_cancelled()
                        view_hits = list(response.hits or [])
                        current = current.model_copy(update={'warnings': self._merge_unique(current.warnings, response.warnings)})
                        added = 0
                        for record in view_hits:
                            key = document_key(record)
                            if not key or key in seen_keys:
                                continue
                            seen_keys.add(key)
                            combined.append(record)
                            added += 1
                        lane_results = dict(current.portfolio_lane_results)
                        lane_results[view.view_id] = view_hits
                        current = current.model_copy(
                            update={"portfolio_lane_results": lane_results}
                        )
                        execution = self._execution(
                            selection,
                            total=int(response.metadata.total or 0),
                            hit_count=len(view_hits),
                            added_unique=added,
                        )
                    except Exception as exc:
                        warning = f"query_portfolio_failed:{view.view_id}"
                        current = current.model_copy(
                            update={
                                "warnings": self._merge_unique(
                                    current.warnings,
                                    [warning],
                                )
                            }
                        )
                        execution = self._execution(
                            selection,
                            status="failed",
                            error=type(exc).__name__,
                        )
                        self._event(
                            "query.portfolio_degraded",
                            {
                                "code": "query_portfolio_failed",
                                "view_id": view.view_id,
                                "error_type": type(exc).__name__,
                            },
                        )
            history = QueryHistoryEntry(
                iteration=current.iteration,
                query=execution.query,
                total=execution.total if execution.status == "completed" else None,
                action="portfolio",
                next_query=None,
                message=(
                    f"Query view {execution.view_id} {execution.status}; returned "
                    f"{execution.hit_count} and added {execution.added_unique} unique candidates."
                ),
                view_id=execution.view_id,
                view_type=execution.view_type,
                purpose=execution.purpose,
                status=execution.status,
                selector_score=execution.selector_score,
                selector_overlap=execution.selector_overlap,
                selector_reasons=execution.selector_reasons,
            )
            current = current.model_copy(
                update={
                    "history": [*current.history, history],
                    "query_portfolio": [*current.query_portfolio, execution],
                }
            )
            self._event(
                "query.portfolio_recorded",
                execution.model_dump(mode="json"),
            )
            completed_ids.add(view.view_id)
            current = await self.checkpoint(
                current,
                "query_portfolio",
                "query.portfolio",
            )
        return current.model_copy(update={"phase": "query_portfolio"})

    @staticmethod
    def _execution(
        selection: QuerySelection,
        *,
        status: str = "completed",
        error: str = "",
        reasons: list[str] | None = None,
        total: int = 0,
        hit_count: int = 0,
        added_unique: int = 0,
    ) -> QueryPortfolioExecution:
        return QueryPortfolioExecution(
            view_id=selection.view.view_id,
            view_type=selection.view.view_type,
            purpose=selection.view.purpose,
            query=selection.compiled_query,
            total=total,
            hit_count=hit_count,
            added_unique=added_unique,
            status=status,
            error=error,
            selector_score=selection.score,
            selector_overlap=selection.lexical_overlap,
            selector_reasons=(
                reasons if reasons is not None else selection.reason_codes
            ),
        )

    @staticmethod
    def _merge_unique(
        values: Iterable[str],
        additions: Iterable[str],
    ) -> list[str]:
        return list(dict.fromkeys([*values, *additions]))

    def _event(self, event_type: str, payload: dict[str, object]) -> None:
        self.cancellation.raise_if_cancelled()
        self.observer.on_event(
            CoreProgressEvent(event_type=event_type, stage="query", payload=payload)
        )


__all__ = ["QueryPortfolioExecutor"]
