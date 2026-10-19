from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from paperseek_app.execution import CancellationScope, WorkflowExecution, run_blocking
from paperseek_app.errors import RunOwnershipLost, RunStateError
from paperseek_app.models import RunDetail, RunStatus
from paperseek_app.services import RunExecutionService
from paperseek_core.ports import CancellationToken

if TYPE_CHECKING:
    from paperseek_app.review.execution import ReviewExecutor


class RunExecutor:
    def __init__(
        self,
        service: RunExecutionService,
        resolve: Callable[[RunDetail, CancellationToken], WorkflowExecution],
        *,
        owner_id: str,
        lease_seconds: int = 60,
        heartbeat_seconds: float | None = None,
        cancellation_poll_seconds: float = 0.05,
        review_executor: ReviewExecutor | None = None,
    ) -> None:
        self.service = service
        self.resolve = resolve
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else max(0.01, lease_seconds / 3)
        )
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("The heartbeat interval must be shorter than the lease.")
        self.cancellation_poll_seconds = cancellation_poll_seconds
        self.review_executor = review_executor

    async def run_once(self) -> RunDetail | None:
        lease = await self.claim()
        return None if lease is None else await self.execute(lease)

    async def claim(self):
        return await run_blocking(self.service.claim_next_run, self.owner_id, self.lease_seconds)

    async def serve(
        self,
        *,
        max_concurrent: int = 0,
        poll_seconds: float = 0.25,
        stop: asyncio.Event | None = None,
        on_finished: Callable[[RunDetail], Awaitable[None]] | None = None,
    ) -> None:
        """Claim queued runs and execute up to ``max_concurrent`` at once (0 means no limit)."""
        stop = stop or asyncio.Event()
        running: set[asyncio.Task] = set()

        async def finish(lease) -> None:
            detail = await self.execute(lease)
            if detail is not None and on_finished is not None:
                await on_finished(detail)

        try:
            while not stop.is_set():
                if max_concurrent and len(running) >= max_concurrent:
                    lease = None
                else:
                    lease = await self.claim()
                if lease is not None:
                    running.add(asyncio.create_task(finish(lease)))
                    continue
                waiters = {*running, asyncio.ensure_future(stop.wait())}
                done, _ = await asyncio.wait(waiters, timeout=poll_seconds, return_when=asyncio.FIRST_COMPLETED)
                for task in waiters - running:
                    task.cancel()
                for task in done & running:
                    running.discard(task)
                    task.result()
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    async def execute(self, lease) -> RunDetail | None:
        run = await run_blocking(self.service.get_run, lease.run_id)
        cancellation = CancellationScope()
        try:
            if run.kind == "review":
                if self.review_executor is None:
                    return await run_blocking(self.service.fail_claimed_run, lease, "review_executor_unavailable")
                return await self.review_executor.execute(
                    lease, run, cancellation, poll_seconds=self.cancellation_poll_seconds,
                    lease_seconds=self.lease_seconds, heartbeat_seconds=self.heartbeat_seconds,
                )
            try:
                resolve_task = asyncio.create_task(asyncio.to_thread(self.resolve, run, cancellation))
                try:
                    execution = await asyncio.shield(resolve_task)
                except asyncio.CancelledError:
                    execution = await resolve_task
                    if execution.close is not None:
                        await execution.close()
                    raise
            except Exception:
                return await run_blocking(self.service.fail_claimed_run, lease, "run_configuration_invalid")
            try:
                return await self.service.execute_claimed_workflow(
                    lease,
                    llm=execution.llm,
                    source=execution.source,
                    request=execution.request,
                    embedding=execution.embedding,
                    reranker=execution.reranker,
                    decision=execution.decision,
                    cancellation=cancellation,
                    cancellation_poll_seconds=self.cancellation_poll_seconds,
                    lease_seconds=self.lease_seconds,
                    heartbeat_seconds=self.heartbeat_seconds,
                    deadline_at=execution.deadline_at,
                )
            finally:
                if execution.close is not None:
                    await execution.close()
        except (RunOwnershipLost, RunStateError) as exc:
            detail = await run_blocking(self.service.get_run, lease.run_id)
            if isinstance(exc, RunStateError) and detail.status not in {
                RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS, RunStatus.CANCELLED, RunStatus.FAILED,
            }:
                raise
            return detail
