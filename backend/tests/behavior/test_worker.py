from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from paperseek_app.execution import WorkflowExecution, run_blocking
from paperseek_app.models import RunStatus
from paperseek_app.services import RunSubmissionService
from paperseek_app.worker import RunExecutor
from paperseek_community.api import create_app
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork
from paperseek_core.models import QueryLoopBudget, ResearchWorkflowRequest


class WorkerConcurrencyContract(unittest.IsolatedAsyncioTestCase):
    async def serve(self, limit: int, queued: int = 5):
        leases = list(range(queued))
        running = peak = 0
        finished = []
        release = asyncio.Event()
        executor = RunExecutor.__new__(RunExecutor)

        async def claim():
            return leases.pop(0) if leases else None

        async def execute(lease):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return SimpleNamespace(id=lease)

        async def on_finished(detail):
            finished.append(detail.id)

        executor.claim, executor.execute = claim, execute
        stop = asyncio.Event()
        task = asyncio.create_task(executor.serve(max_concurrent=limit, poll_seconds=0.01, stop=stop,
                                                  on_finished=on_finished))
        await asyncio.sleep(0.1)
        started = peak
        release.set()
        while len(finished) < queued:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 1)
        return started, sorted(finished)

    async def test_limit_caps_tasks_running_at_once(self):
        started, finished = await self.serve(2)
        self.assertEqual(started, 2)
        self.assertEqual(finished, [0, 1, 2, 3, 4])

    async def test_zero_runs_every_queued_task_at_once(self):
        started, finished = await self.serve(0)
        self.assertEqual(started, 5)
        self.assertEqual(finished, [0, 1, 2, 3, 4])

    async def test_stopping_cancels_running_tasks(self):
        executor = RunExecutor.__new__(RunExecutor)
        cancelled = asyncio.Event()
        leases = [1]

        async def claim():
            return leases.pop() if leases else None

        async def execute(_lease):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        executor.claim, executor.execute = claim, execute
        task = asyncio.create_task(executor.serve(max_concurrent=4, poll_seconds=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

class WorkerHealthContract(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_cancels_inflight_io_and_retains_recoverable_checkpoint(self) -> None:
        source = FixedSourceAdapter.from_file(Path(__file__).parent / "fixtures/m1_fixed_source.json")
        llm = FixedLLMAdapter(source.fixture.llm_responses)
        started = asyncio.Event()
        stopped = asyncio.Event()
        closed = AsyncMock()
        calls = 0

        async def gated_chat(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return await llm.chat_json(*args, **kwargs)

        execution = WorkflowExecution(
            llm=type("GatedLLM", (), {"chat_json": staticmethod(gated_chat)})(),
            source=source, close=closed,
            request=ResearchWorkflowRequest(
                question=source.fixture.question,
                query_budget=QueryLoopBudget(
                    target_min=1, target_max=50, retrieval_pool_min=1, query_portfolio_enabled=False,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            app = create_app(database, execution_resolver=lambda *_: execution)
            uow = SQLiteRunUnitOfWork(database)
            try:
                run = RunSubmissionService(uow).create_run(source.fixture.question, source.name, {})
                lifespan = app.router.lifespan_context(app)
                await lifespan.__aenter__()
                try:
                    await asyncio.wait_for(started.wait(), 5)
                finally:
                    await asyncio.wait_for(lifespan.__aexit__(None, None, None), 2)
                self.assertTrue(stopped.is_set())
                closed.assert_awaited_once()
                self.assertEqual(uow.get_run(run.id).status, RunStatus.RUNNING)
                self.assertEqual(uow.get_checkpoint(run.id).step_key, "select_candidates")
            finally:
                uow.close()

    async def test_failed_worker_is_unready_and_shutdown_always_closes_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(Path(temp) / "paperseek.db", execution_resolver=lambda *_: None)
            original_close = SQLiteRunUnitOfWork.close
            with (
                patch.object(RunExecutor, "claim", AsyncMock(side_effect=RuntimeError("worker fault"))),
                patch.object(SQLiteRunUnitOfWork, "close", autospec=True, side_effect=original_close) as close,
            ):
                lifespan = app.router.lifespan_context(app)
                await lifespan.__aenter__()
                await asyncio.wait({app.state.worker_task}, timeout=2)
                self.assertTrue(app.state.worker_task.done())
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    self.assertEqual((await client.get("/health")).status_code, 200)
                    ready = await client.get("/ready")
                    self.assertEqual(ready.status_code, 503)
                    self.assertEqual(ready.json()["error"]["code"], "executor_unavailable")
                before_shutdown = close.call_count
                with self.assertRaisesRegex(RuntimeError, "worker fault"):
                    await lifespan.__aexit__(None, None, None)
                self.assertEqual(close.call_count, before_shutdown + 1)

    async def test_external_worker_mode_does_not_require_embedded_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(Path(temp) / "paperseek.db", accept_submissions_without_worker=True)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    self.assertEqual((await client.get("/ready")).status_code, 200)

class RunBlockingCancellationContract(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_thread_does_not_replace_the_cancellation(self) -> None:
        started, release = threading.Event(), threading.Event()

        def fail_after_release() -> None:
            started.set()
            release.wait(5)
            raise RuntimeError("storage failed")

        task = asyncio.create_task(run_blocking(fail_after_release))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        # A worker loop that catches storage errors would otherwise never stop.
        with self.assertRaises(asyncio.CancelledError):
            await task
