from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from paperseek_app.errors import RunOwnershipLost, RunPersistenceError, RunStateError
from paperseek_app.execution import CancellationScope, run_blocking, warm_up_model
from paperseek_app.leased_task import run_leased_task
from paperseek_app.models import LeaseToken, PendingEvent, RunDetail, RunStatus
from paperseek_app.review.models import ReviewError
from paperseek_app.review.ports import ReviewStore
from paperseek_app.review.workflow import ReviewStageWorkflow
from paperseek_app.services.run_execution import RunExecutionService
from paperseek_core.errors import LLMRequestError
from paperseek_core.ports import CancellationToken, LLMPort
from paperseek_core.review.structured import (
    ReviewOutputError,
)


@dataclass(frozen=True, slots=True)
class ReviewExecution:
    llm: LLMPort
    route: dict
    deadline_at: float | None = None
    close: Callable[[], Awaitable[None]] | None = None
    warmup: Callable[[], Awaitable[None]] | None = None


class ReviewExecutor:
    def __init__(self, store: ReviewStore, runs: RunExecutionService,
                 resolve: Callable[[RunDetail, CancellationToken], ReviewExecution]) -> None:
        self.store, self.runs, self.resolve = store, runs, resolve

    async def execute(self, lease: LeaseToken, run: RunDetail, scope: CancellationScope, *,
                      poll_seconds: float, lease_seconds: int, heartbeat_seconds: float) -> RunDetail:
        execution = None
        try:
            resolve_task = asyncio.create_task(asyncio.to_thread(self.resolve, run, scope))
            try:
                execution = await asyncio.shield(resolve_task)
            except asyncio.CancelledError:
                execution = await resolve_task
                raise
            context = await run_blocking(self.store.review_job_context, run.id)
            review = await run_blocking(self.store.get_review, context.review_id)
            workflow = ReviewStageWorkflow(self.store, lease, review, context, execution.llm, execution.route, scope)

            async def perform() -> None:
                async with asyncio.timeout_at(execution.deadline_at):
                    await warm_up_model(execution.warmup)
                    await workflow.run()

            await run_leased_task(perform(), uow=self.runs.uow, lease=lease, scope=scope,
                                  poll_seconds=poll_seconds, lease_seconds=lease_seconds,
                                  heartbeat_seconds=heartbeat_seconds)
        except asyncio.CancelledError:
            detail = await run_blocking(self.runs.get_run, run.id)
            if detail.status == RunStatus.CANCELLING:
                return await run_blocking(self.runs.complete_cancel, lease)
            if detail.status == RunStatus.CANCELLED:
                return detail
            raise
        except RunOwnershipLost:
            raise
        except Exception as exc:
            events = []
            # Concurrent paper/section tasks may report errors as an ExceptionGroup.
            failure = exc
            while isinstance(failure, ExceptionGroup):
                failure = failure.exceptions[0]
            if isinstance(failure, RunOwnershipLost):
                raise failure from exc
            if isinstance(failure, ReviewError):
                code = failure.code
            elif isinstance(failure, LLMRequestError):
                code = failure.code
            elif isinstance(failure, ReviewOutputError):
                code = "review_model_output_invalid"
                events.append(PendingEvent(event_type="review.output.invalid", stage="review." + failure.task,
                                           payload={"task": failure.task, "reason": failure.reason}))
            elif isinstance(failure, (RunPersistenceError, RunStateError)):
                code = "review_persistence_failed"
            elif isinstance(failure, TimeoutError):
                code = "run_deadline_exceeded"
            else:
                code = "review_generation_failed"
            return await run_blocking(self.runs.fail_claimed_run, lease, code, events)
        finally:
            if execution and execution.close:
                await execution.close()
        return await run_blocking(self.runs.get_run, run.id)
