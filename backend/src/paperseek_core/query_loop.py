"""Structured query planning, bounded refinement, and portfolio execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from paperseek_core.errors import LLMRequestError, QueryGenerationError, QuerySourceError
from paperseek_core.execution import NEVER_CANCELLED, record_checkpoint
from paperseek_core.http_retry import failure_details
from paperseek_core.models import (
    CheckpointDraft,
    CoreProgressEvent,
    PaperRecord,
    QueryHistoryEntry,
    QueryIterationMemory,
    QueryLoopBudget,
    QueryLoopDecision,
    QueryLoopRequest,
    QueryLoopResult,
    QueryLoopResumeState,
    QueryLoopSnapshot,
    RetrievalRequest,
    SearchPlan,
)
from paperseek_core.ports import CancellationToken, LLMPort, SourcePort, WorkflowObserver
from paperseek_core.query_compiler import (
    validate_source_query,
)
from paperseek_core.query_planning import (
    QueryLoopController,
    QueryPlanner,
    normalize_query,
    _title_drift,
)
from paperseek_core.query_portfolio import QueryPortfolioExecutor
from paperseek_core.query_history import executed_attempts, repeated_attempt, repetition_decision, result_preference
from paperseek_core.retrieval_fusion import dedupe_documents
from paperseek_core.sources.contracts import SourceError, SourceSearchRequest, SourceSearchResult


class QueryLoopPipeline:
    """Orchestrate query refinement through LLM, source, and observer ports."""

    def __init__(
        self,
        llm: LLMPort,
        source: SourcePort,
        observer: WorkflowObserver,
        *,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> None:
        self.llm = llm
        self.source = source
        self.observer = observer
        self.cancellation = cancellation
        self.planner = QueryPlanner(llm)
        self.controller = QueryLoopController()
        self.portfolio = QueryPortfolioExecutor(
            source,
            observer,
            self._checkpoint,
            cancellation,
        )
        self.source_context = ""

    async def run(self, request: QueryLoopRequest) -> QueryLoopResult:
        self.cancellation.raise_if_cancelled()
        self.source_context = request.source_context
        budget = request.budget
        self._event(
            "query.started",
            {
                "source": self.source.name,
                "target_min": budget.target_min,
                "target_max": budget.target_max,
                "retrieval_pool_max": budget.retrieval_pool_max,
                "max_iterations": budget.max_iterations,
            },
        )
        if request.resume is not None:
            state = self._validate_resume(request)
            self._event(
                "query.resumed",
                {
                    "phase": state.phase,
                    "iteration": state.iteration,
                    "portfolio_count": len(state.query_portfolio),
                },
            )
        else:
            plan = await self.planner.plan(
                request.question,
                portfolio_required=budget.query_portfolio_enabled,
                source_context=self.source_context,
            )
            self.cancellation.raise_if_cancelled()
            proposal = await self.planner.propose(
                self.source.name,
                request.question,
                plan,
                operation="generate",
                source_context=self.source_context,
            )
            self.cancellation.raise_if_cancelled()
            state = QueryLoopResumeState(
                phase="query_ready",
                question=request.question,
                source=self.source.name,
                search_plan=plan,
                current_query=proposal.query,
                warnings=proposal.warnings,
            )
            self._event(
                "query.planned",
                {
                    "source": self.source.name,
                    "intent": plan.intent,
                    "question_type": plan.question_type,
                    "current_query": proposal.query,
                    "query_views": [view.model_dump(mode="json") for view in plan.query_views],
                    "query_view_count": len(plan.query_views),
                    "warning_count": len(proposal.warnings),
                },
            )
            state = await self._checkpoint(state, "query_ready", "query.plan")

        if state.phase not in {"query_primary_complete", "query_portfolio", "query_complete", "query_empty_recovery", "query_empty_recovery_ready"}:
            state = await self._run_primary_loop(state, budget)
        if state.phase not in {"query_complete", "query_portfolio"} and state.total == 0 and not state.primary_hits and not state.fallback_hits:
            state = await self._recover_empty(state, budget)
        if state.phase != "query_complete":
            state = await self.portfolio.run(state, budget)
            state = await self._checkpoint(state, "query_complete", "query.complete")

        result = self._result(state, budget)
        self._event(
            "query.completed",
            {
                "source": self.source.name,
                "iteration": result.iterations,
                "total": result.total,
                "query": result.final_query,
                "decision": result.terminal_status,
                "primary_count": len(result.primary_hits),
                "candidate_count": len(result.records),
                "portfolio_count": len(result.query_portfolio),
                "warning_count": len(result.warnings),
            },
        )
        return result

    def _validate_resume(self, request: QueryLoopRequest) -> QueryLoopResumeState:
        assert request.resume is not None
        resume = request.resume
        if resume.question.strip() != request.question.strip():
            raise QuerySourceError("query checkpoint question does not match the resumed request")
        if resume.source != self.source.name:
            raise QuerySourceError("query checkpoint source does not match the resumed request")
        return resume

    async def _run_primary_loop(
        self,
        state: QueryLoopResumeState,
        budget: QueryLoopBudget,
    ) -> QueryLoopResumeState:
        configured = max(1, int(budget.max_iterations))
        adaptive = (
            max(configured, int(budget.adaptive_max_iterations))
            if budget.adaptive_max_iterations is not None
            else max(configured + 8, configured * 3, 10)
        )
        pool_min = min(max(0, int(budget.retrieval_pool_min)), budget.target_max)
        while state.iteration < adaptive:
            self.cancellation.raise_if_cancelled()
            if repeated_attempt(state.current_query, state.history) is not None:
                # A persisted next_query may predate the repetition guard. Its
                # primary_hits/total still belong to the last completed attempt.
                last = executed_attempts(state.history)[-1]
                pending_query = state.current_query
                state = state.model_copy(update={"current_query": last.query, "total": last.total})
                return await self._stop_repeated(state, budget, pending_query)
            iteration = state.iteration + 1
            query = state.current_query
            validation = validate_source_query(self.source.name, query)
            if not validation.ok:
                raise QueryGenerationError(
                    f"{self.source.name} query failed preflight: {validation.issue}"
                )
            self._event(
                "query.request_started",
                {
                    "source": self.source.name,
                    "iteration": iteration,
                    "lane": "relevance",
                    "query": query,
                    "limit": self._primary_limit(budget),
                },
            )
            try:
                response = await self._primary_search_with_repair(state, query, iteration, budget)
            except QuerySourceError:
                if not state.fallback_hits:
                    raise
                state = self._restore_visible_fallback(state, budget)
                state = state.model_copy(update={
                    'terminal_status': repetition_decision(state.total, budget).action,
                    'warnings': self._merge_unique(state.warnings, ['query_source_unavailable_candidates_retained']),
                })
                self._event('query.degraded', {'code': 'query_source_unavailable_candidates_retained', 'hit_count': len(state.primary_hits)})
                return await self._checkpoint(state, 'query_primary_complete', 'query.primary')
            query = response[0]
            source_result = response[1]
            if source_result is None:
                last = executed_attempts(state.history)[-1]
                state = state.model_copy(update={"current_query": last.query, "total": last.total})
                return await self._stop_repeated(state, budget, query)
            hits = list(source_result.hits or [])
            primary_page = source_result.metadata.model_copy(update={
                "raw_count": source_result.metadata.raw_count
                if source_result.metadata.raw_count is not None else len(hits),
            })
            total = int(source_result.metadata.total or 0)
            memory = self._record_iteration(
                list(state.query_iteration_memory),
                iteration,
                query,
                total,
                hits,
            )
            fallback_query = state.fallback_query
            fallback_total = state.fallback_total
            fallback_hits = list(state.fallback_hits)
            fallback_page = state.fallback_page
            snapshot = QueryLoopSnapshot(
                iteration=iteration,
                configured_iterations=configured,
                adaptive_max_iterations=adaptive,
                total=total,
                target_min=budget.target_min,
                target_max=budget.target_max,
                retrieval_pool_min=pool_min,
                retrieval_pool_max=budget.retrieval_pool_max,
                history=[item.model_dump(mode="json") for item in state.history],
                query=query,
                hit_count=len(hits),
                top_titles=[record.title for record in hits[:5] if record.title],
                intent_terms=self._intent_terms(state.search_plan),
            )
            previous = snapshot.model_copy(update={
                "query": fallback_query,
                "top_titles": [record.title for record in fallback_hits[:5] if record.title],
            })
            if total and hits and (not fallback_hits or
                result_preference(total, budget, off_intent=_title_drift(snapshot)[0]) <=
                result_preference(fallback_total, budget, off_intent=_title_drift(previous)[0])
            ):
                fallback_query, fallback_total = query, total
                fallback_hits, fallback_page = list(hits), primary_page
            decision = self.controller.decide(snapshot)
            updated = state.model_copy(
                update={
                    "current_query": query,
                    "iteration": iteration,
                    "total": total,
                    "primary_hits": hits,
                    "primary_page": primary_page,
                    "query_iteration_memory": memory,
                    "fallback_query": fallback_query,
                    "fallback_total": fallback_total,
                    "fallback_hits": fallback_hits,
                    "fallback_page": fallback_page,
                    "warnings": self._merge_unique(state.warnings, source_result.warnings),
                }
            )
            self._event(
                "query.progress",
                {
                    "iteration": iteration,
                    "query": query,
                    "total": total,
                    "hit_count": len(hits),
                    "decision": decision.action,
                    "message": decision.message,
                    "reason": decision.reason,
                },
            )
            history = QueryHistoryEntry(
                iteration=iteration, query=query, total=total,
                action=decision.action, message=decision.message,
                controller_reason=decision.reason,
                source_feedback=source_result.query_feedback,
            )
            if total == 0 and not hits and not fallback_hits:
                # Empty responses take the checked, bounded recovery path instead
                # of repeatedly relaxing the question until something matches.
                updated = updated.model_copy(update={
                    "history": [*updated.history, history], "terminal_status": "empty",
                })
                return await self._checkpoint(updated, "query_primary_complete", "query.primary")
            if decision.action in {"broaden", "narrow"}:
                proposal = await self.planner.propose(
                    self.source.name,
                    state.question,
                    state.search_plan,
                    operation=decision.action,
                    current_query=query,
                    feedback=decision.adjustment,
                    source_context=self.source_context,
                    history=[*updated.history, history],
                    top_titles=snapshot.top_titles,
                )
                self.cancellation.raise_if_cancelled()
                warnings = self._merge_unique(updated.warnings, proposal.warnings)
                if repeated_attempt(proposal.query, [*updated.history, history]) is not None:
                    return await self._stop_repeated(updated.model_copy(update={"warnings": warnings}), budget,
                                                     proposal.query, source_feedback=history.source_feedback)
                history = history.model_copy(update={"next_query": proposal.query, "rationale": proposal.rationale})
                updated = updated.model_copy(
                    update={
                        "current_query": proposal.query,
                        "history": [*updated.history, history],
                        "warnings": warnings,
                    }
                )
                state = await self._checkpoint(updated, "next_query", "query.iteration")
                continue
            updated = updated.model_copy(
                update={
                    "history": [*updated.history, history],
                    "terminal_status": decision.action,
                }
            )
            updated = self._restore_visible_fallback(updated, budget)
            if decision.stop_reason:
                decision = decision.model_copy(update={"action": repetition_decision(updated.total, budget).action})
                updated = self._record_refinement_stop(updated, decision)
            updated = updated.model_copy(update={"terminal_status": repetition_decision(updated.total, budget).action})
            return await self._checkpoint(
                updated,
                "query_primary_complete",
                "query.primary",
            )
        return await self._checkpoint(
            self._restore_visible_fallback(state, budget),
            "query_primary_complete",
            "query.primary",
        )

    async def _stop_repeated(
        self, state: QueryLoopResumeState, budget: QueryLoopBudget, proposed_query: str,
        *, source_feedback: list[str] | None = None,
    ) -> QueryLoopResumeState:
        decision = repetition_decision(state.total, budget)
        terminal = QueryHistoryEntry(
            iteration=state.iteration, query=state.current_query, total=state.total,
            action=decision.action, message=decision.message, controller_reason=decision.reason,
            next_query=proposed_query,
            source_feedback=source_feedback or [],
        )
        state = self._restore_visible_fallback(state.model_copy(update={"history": [*state.history, terminal]}), budget)
        decision = repetition_decision(state.total, budget)
        state = self._record_refinement_stop(state, decision).model_copy(update={"terminal_status": decision.action})
        return await self._checkpoint(state, "query_primary_complete", "query.primary")

    async def _recover_empty(self, state: QueryLoopResumeState, budget: QueryLoopBudget) -> QueryLoopResumeState:
        if state.phase != 'query_empty_recovery_ready':
            if state.empty_recovery_attempted:
                return state.model_copy(update={'warnings': self._merge_unique(state.warnings, ['query_empty_unresolved'])})
            state = await self._checkpoint(state.model_copy(update={'empty_recovery_attempted': True}),
                                           'query_empty_recovery', 'query.empty_recovery')
            try:
                proposal = await self.planner.recover_empty(self.source.name, state.question, state.search_plan,
                    current_query=state.current_query, history=state.history, source_context=self.source_context)
            except (QueryGenerationError, LLMRequestError):
                proposal = None
            self.cancellation.raise_if_cancelled()
            if proposal is None:
                state = state.model_copy(update={'warnings': self._merge_unique(state.warnings, ['query_empty_recovery_rejected'])})
                return await self._checkpoint(state, 'query_primary_complete', 'query.empty_recovery')
            state = await self._checkpoint(state.model_copy(update={
                'current_query': proposal.query,
                'warnings': self._merge_unique(state.warnings, proposal.warnings),
            }), 'query_empty_recovery_ready', 'query.empty_recovery')
        iteration = state.iteration + 1
        self._event('query.request_started', {'source': self.source.name, 'iteration': iteration, 'lane': 'relevance',
                    'query': state.current_query, 'limit': self._primary_limit(budget), 'recovery': True})
        try:
            response = await self.source.search(SourceSearchRequest(query=state.current_query,
                limit=self._primary_limit(budget), lane='relevance', field_ids=budget.field_ids))
        except SourceError as error:
            self._primary_failed(iteration, error)
        self.cancellation.raise_if_cancelled()
        hits, total = list(response.hits), response.metadata.total
        code = 'query_empty_recovered' if hits else 'query_empty_unresolved'
        decision = repetition_decision(total, budget).action
        state = state.model_copy(update={
            'iteration': iteration, 'total': total, 'primary_hits': hits, 'primary_page': response.metadata,
            'terminal_status': decision,
            'history': [*state.history, QueryHistoryEntry(iteration=iteration, query=state.current_query,
                total=total, action='empty_recovery', source_feedback=response.query_feedback)],
            'query_iteration_memory': self._record_iteration(list(state.query_iteration_memory), iteration, state.current_query, total, hits),
            'warnings': self._merge_unique(state.warnings, [*response.warnings, code]),
        })
        self._event('query.progress', {'iteration': iteration, 'query': state.current_query, 'total': total,
                    'hit_count': len(hits), 'decision': decision, 'code': code})
        return await self._checkpoint(state, 'query_primary_complete', 'query.empty_recovery')

    def _record_refinement_stop(
        self, state: QueryLoopResumeState, decision: QueryLoopDecision,
    ) -> QueryLoopResumeState:
        self._event("query.refinement_stopped", {
            "code": decision.stop_reason, "iteration": state.iteration,
            "query": state.current_query, "total": state.total,
            "hit_count": len(state.primary_hits), "decision": decision.action,
            "message": decision.message,
        })
        return state.model_copy(update={"warnings": self._merge_unique(state.warnings, [decision.stop_reason])})

    async def _primary_search_with_repair(
        self,
        state: QueryLoopResumeState,
        query: str,
        iteration: int,
        budget: QueryLoopBudget,
    ) -> tuple[str, SourceSearchResult | None]:
        current = query
        repair_attempts = 0
        while True:
            self.cancellation.raise_if_cancelled()
            if repeated_attempt(current, state.history) is not None:
                # Syntax repair can propose a completed query as well. Let the
                # outer loop retain its last result and finish normally.
                return current, None
            request = SourceSearchRequest(
                query=current,
                limit=self._primary_limit(budget),
                page=1,
                lane="relevance",
                field_ids=budget.field_ids,
            )
            try:
                result = await self.source.search(request)
                self.cancellation.raise_if_cancelled()
                return current, result
            except SourceError as exc:
                if exc.status not in {400, 422} or repair_attempts >= 2:
                    self._primary_failed(iteration, exc)
                repair_attempts += 1
                proposal = await self.planner.propose(
                    self.source.name,
                    state.question,
                    state.search_plan,
                    operation="repair",
                    current_query=current,
                    feedback="The source rejected the query syntax; preserve intent and correct only source syntax.",
                    source_context=self.source_context,
                    history=state.history,
                    top_titles=[record.title for record in state.primary_hits[:3] if record.title],
                )
                if normalize_query(proposal.query) == normalize_query(current):
                    self._primary_failed(iteration, exc)
                current = proposal.query
            except Exception as exc:
                self._primary_failed(iteration, exc)

    def _primary_failed(self, iteration: int, exc: Exception) -> None:
        payload = {
            "code": exc.code if isinstance(exc, SourceError) else "query_primary_source_failed",
            "source": self.source.name,
            "iteration": iteration,
            "error_type": type(exc).__name__,
            **failure_details(exc),
        }
        # Persist only bounded diagnostic fields, never response bodies or request URLs.
        if isinstance(exc, SourceError):
            if exc.status is not None:
                payload["http_status"] = exc.status
            if exc.failure_kind is not None:
                payload["failure_kind"] = exc.failure_kind.value
        self._event(
            "query.failed",
            payload,
        )
        raise QuerySourceError(
            "The primary query failed without a recoverable source result.",
            code=payload["code"],
        ) from exc

    def _result(
        self,
        state: QueryLoopResumeState,
        budget: QueryLoopBudget,
    ) -> QueryLoopResult:
        supplemental = {}
        final_normalized = normalize_query(state.current_query)
        for item in state.query_iteration_memory:
            if item.records and normalize_query(item.query) != final_normalized:
                supplemental[f"query_iteration:{max(1, item.iteration)}"] = list(item.records)
        for view_id, records in state.portfolio_lane_results.items():
            if records:
                supplemental[f"query_view:{view_id}"] = list(records)
        records = dedupe_documents(
            [
                *state.primary_hits,
                *[record for values in state.portfolio_lane_results.values() for record in values],
            ]
        )
        retrieval = RetrievalRequest(
            question=state.question,
            query=state.current_query,
            primary_hits=state.primary_hits,
            primary_page=state.primary_page,
            supplemental_lanes=supplemental,
            lane_limit=budget.retrieval_lane_limit,
            ranking_candidate_limit=budget.ranking_candidate_limit,
            pool_max=budget.retrieval_pool_max,
            field_ids=budget.field_ids,
        )
        return QueryLoopResult(
            question=state.question,
            source=state.source,
            search_plan=state.search_plan,
            final_query=state.current_query,
            total=state.total,
            primary_hits=state.primary_hits,
            records=records,
            iterations=state.iteration,
            history=state.history,
            query_iteration_memory=state.query_iteration_memory,
            query_view_selection=state.query_view_selection,
            query_portfolio=state.query_portfolio,
            terminal_status=state.terminal_status,
            warnings=state.warnings,
            retrieval_request=retrieval,
        )

    async def _checkpoint(
        self,
        state: QueryLoopResumeState,
        phase: str,
        step_key: str,
    ) -> QueryLoopResumeState:
        self.cancellation.raise_if_cancelled()
        current = state.model_copy(update={"phase": phase})
        if self.observer.on_checkpoint is None:
            return current
        payload = {
            "source": current.source,
            "question": current.question,
            "phase": phase,
            "iteration": current.iteration,
            "query": current.current_query,
            "portfolio": [item.view_id for item in current.query_portfolio],
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        await record_checkpoint(
            self.observer,
            CheckpointDraft(
                step_key=step_key,
                idempotency_key=f"query:{phase}:{digest}",
                state=current.model_dump(mode="json"),
                workflow_version="m2.query-loop.v2",
            )
        )
        return current

    def _restore_visible_fallback(
        self,
        state: QueryLoopResumeState,
        budget: QueryLoopBudget,
    ) -> QueryLoopResumeState:
        if not state.fallback_hits or state.current_query == state.fallback_query:
            return state
        selected_iteration = next((row.iteration for row in executed_attempts(state.history)
                                   if row.query == state.fallback_query), 0)
        self._event("query.selected", {
            "query": state.fallback_query, "total": state.fallback_total,
            "hit_count": len(state.fallback_hits), "selected_iteration": selected_iteration,
            "iteration": state.iteration, "previous_query": state.current_query,
            "previous_total": state.total, "reason": "intent_and_bounded_scope",
            "decision": repetition_decision(state.fallback_total, budget).action,
        })
        history = QueryHistoryEntry(
            iteration=state.iteration,
            query=state.current_query,
            total=state.total,
            action="fallback",
            next_query=state.fallback_query,
            message=(
                f"Selected iteration {selected_iteration} for downstream retrieval using title-intent "
                "alignment and bounded source count; retained other rounds as supplemental candidates."
            ),
        )
        return state.model_copy(
            update={
                "current_query": state.fallback_query,
                "total": state.fallback_total,
                "primary_hits": state.fallback_hits,
                "primary_page": state.fallback_page,
                "history": [*state.history, history],
                "warnings": self._merge_unique(
                    state.warnings,
                    ["query_terminal_zero_last_nonempty_retained"] if state.total == 0 else [],
                ),
            }
        )

    @staticmethod
    def _record_iteration(
        memory: list[QueryIterationMemory],
        iteration: int,
        query: str,
        total: int,
        records: list[PaperRecord],
    ) -> list[QueryIterationMemory]:
        records = dedupe_documents(records)[:100]
        normalized = normalize_query(query)
        if not records or not normalized:
            return memory
        entry = QueryIterationMemory(
            iteration=iteration,
            query=query,
            total=total,
            records=records,
        )
        output = list(memory)
        for index, existing in enumerate(output):
            if normalize_query(existing.query) == normalized:
                output[index] = entry
                break
        else:
            output.append(entry)
        return output[-12:]

    def _primary_limit(self, budget: QueryLoopBudget) -> int:
        output_limit = max(1, min(int(budget.target_max or 50), 50))
        if self.source.name == "wos":
            return output_limit
        return min(max(output_limit * 2, output_limit), 100)

    @staticmethod
    def _intent_terms(plan: SearchPlan) -> list[str]:
        return [
            *plan.core_concepts,
            *plan.must_have_facets,
            *plan.likely_synonyms,
            plan.required_relation,
        ]

    @staticmethod
    def _merge_unique(values: Iterable[str], additions: Iterable[str]) -> list[str]:
        return list(dict.fromkeys([*values, *additions]))

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.cancellation.raise_if_cancelled()
        self.observer.on_event(
            CoreProgressEvent(event_type=event_type, stage="query", payload=payload)
        )


__all__ = ["QueryLoopPipeline"]
