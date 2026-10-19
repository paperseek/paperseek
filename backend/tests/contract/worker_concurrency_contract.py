"""The worker runs queued tasks side by side up to the configured limit."""
import asyncio
import unittest
from types import SimpleNamespace

from paperseek_app.worker import RunExecutor


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


if __name__ == "__main__":
    unittest.main()
