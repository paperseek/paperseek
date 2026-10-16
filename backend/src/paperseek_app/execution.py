from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from paperseek_core.models import ResearchWorkflowRequest
from paperseek_core.ports import CancellationToken, DecisionPort, EmbeddingPort, LLMPort, RerankPort, SourcePort, WorkflowObserver
from paperseek_core.workflow import ResearchWorkflow


async def run_blocking[T](function: Callable[..., T], *args, **kwargs) -> T:
    """Keep synchronous storage off the event loop and drain writes before shutdown."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled. Wait before their store/client can be closed, but keep the
        # cancellation: a failure inside the thread must not replace it and keep a loop running.
        await asyncio.gather(task, return_exceptions=True)
        raise


async def warm_up_model(warmup: Callable[[], Awaitable[None]] | None) -> None:
    """A cold local runtime pays for the model load on its very first request.

    A failed warm-up is not a run failure: the first real request reports the real
    error, and the deadline still has to end the run rather than be swallowed here.
    """
    if warmup is None:
        return
    try:
        await warmup()
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - the first real model call reports the real failure
        return


class CancellationScope:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    llm: LLMPort
    source: SourcePort
    request: ResearchWorkflowRequest
    embedding: EmbeddingPort | None = None
    reranker: RerankPort | None = None
    decision: DecisionPort | None = None
    close: Callable[[], Awaitable[None]] | None = None
    warmup: Callable[[], Awaitable[None]] | None = None
    deadline_at: float | None = None

    async def run(self, observer: WorkflowObserver, cancellation: CancellationToken):
        async with asyncio.timeout_at(self.deadline_at):
            await warm_up_model(self.warmup)
            return await ResearchWorkflow(
                llm=self.llm, source=self.source, observer=observer,
                embedding=self.embedding,
                reranker=self.reranker, decision=self.decision, cancellation=cancellation,
            ).run(self.request)


assert isinstance(CancellationScope(), CancellationToken)


__all__ = ["CancellationScope", "WorkflowExecution", "warm_up_model"]
