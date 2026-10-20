from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from paperseek_app.errors import RunOwnershipLost, RunPersistenceError, RunStateError
from paperseek_app.execution import WorkflowExecution, run_blocking
from paperseek_app.models import PendingEvent, RunStatus
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_app.worker import RunExecutor
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_core.models import QueryLoopBudget, ResearchWorkflowRequest

FIXTURE = Path(__file__).parent / "fixtures" / "m1_fixed_source.json"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class TrackingSource:
    def __init__(self, *, block_on_search: int | None = None) -> None:
        self.delegate = FixedSourceAdapter.from_file(FIXTURE)
        self.name = self.delegate.name
        self.capabilities = self.delegate.capabilities
        self.fixture = self.delegate.fixture
        self.block_on_search = block_on_search
        self.search_calls = 0
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, request):
        self.search_calls += 1
        if self.search_calls == self.block_on_search:
            self.blocked.set()
            await self.release.wait()
        return await self.delegate.search(request)

    async def traverse_citations(self, request):
        return await self.delegate.traverse_citations(request)



class GatedLLM:
    def __init__(self, responses: list[str], *, block_on_call: int = 1) -> None:
        self.delegate = FixedLLMAdapter(responses)
        self.block_on_call = block_on_call
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096):
        self.calls += 1
        if self.calls == self.block_on_call:
            self.started.set()
            await self.release.wait()
        return await self.delegate.chat_json(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )


class M3CrashLeaseCheckpointRecoveryContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.database = self.data_dir / "paperseek.db"
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.clock = MutableClock()
        migrate_database(self.database)
        self.uow = SQLiteRunUnitOfWork(self.database, clock=self.clock)
        store = FileArtifactStore(self.data_dir)
        self.submission_service = RunSubmissionService(self.uow)
        self.execution_service = RunExecutionService(self.uow, store)
        self.query_service = RunQueryService(self.uow, store)

    async def asyncTearDown(self) -> None:
        self.uow.close()
        self.temp.cleanup()

    def _request(self) -> ResearchWorkflowRequest:
        return ResearchWorkflowRequest(
            question=self.fixture["question"],
            query_budget=QueryLoopBudget(
                target_min=1,
                target_max=50,
                retrieval_pool_min=1,
                query_portfolio_enabled=False,
            ),
            citation_enabled=False,
        )

    def _create(self, *, deadline_seconds: int = 3600):
        return self.submission_service.create_run(
            self.fixture["question"],
            self.fixture["source"],
            {"fixture_id": self.fixture["fixture_id"]},
            deadline_seconds=deadline_seconds,
        )

    async def _crash_after_selection_checkpoint(self) -> tuple[str, TrackingSource]:
        run = self._create()
        source = TrackingSource()
        llm = GatedLLM(self.fixture["llm_responses"], block_on_call=3)

        def resolve(_run, _cancellation):
            return WorkflowExecution(
                llm=llm,
                source=source,
                request=self._request(),
            )

        executor = RunExecutor(
            self.execution_service,
            resolve,
            owner_id="worker-before-crash",
            lease_seconds=30,
            heartbeat_seconds=1,
        )
        task = asyncio.create_task(executor.run_once())
        await asyncio.wait_for(llm.started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        detail = self.query_service.get_run(run.id)
        self.assertEqual(detail.status, RunStatus.RUNNING)
        self.assertEqual(self.execution_service.get_checkpoint(run.id).step_key, "select_candidates")
        return run.id, source

    async def test_expired_lease_is_reclaimed_and_stale_generation_is_fenced(self) -> None:
        run = self._create()
        stale = self.execution_service.claim_next_run("worker-a", lease_seconds=30)
        self.assertEqual(stale.run_id, run.id)
        self.clock.advance(31)

        current = self.execution_service.claim_next_run("worker-b", lease_seconds=30)

        self.assertEqual(current.run_id, run.id)
        self.assertEqual(current.lease_generation, stale.lease_generation + 1)
        self.assertEqual(self.query_service.get_run(run.id).attempt, 2)
        with self.assertRaises(RunOwnershipLost):
            self.uow.append_events_and_checkpoint(
                stale,
                [PendingEvent(event_type="worker.late", stage="executor")],
                None,
                None,
            )

    async def test_progress_is_durable_before_blocked_llm_finishes(self) -> None:
        run = self._create()
        llm = GatedLLM(self.fixture["llm_responses"])
        source = TrackingSource()
        executor = RunExecutor(
            self.execution_service,
            lambda *_: WorkflowExecution(llm=llm, source=source, request=self._request()),
            owner_id="progress-worker",
        )
        task = asyncio.create_task(executor.run_once())
        try:
            await asyncio.wait_for(llm.started.wait(), timeout=3)
            async with asyncio.timeout(3):
                while not any(e.event_type == "query.started" for e in await run_blocking(self.uow.list_events, run.id)):
                    await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertIsNone(self.uow.get_checkpoint(run.id))
            self.submission_service.request_cancel(run.id)
            self.assertEqual((await asyncio.wait_for(task, 3)).status, RunStatus.CANCELLED)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_slow_final_artifact_keeps_lease_until_results_are_committed(self) -> None:
        self._create()
        started, release, renewed = threading.Event(), threading.Event(), threading.Event()
        store = self.execution_service.artifact_store
        original_put, original_renew = store.put, self.uow.renew_lease

        def put(artifact):
            if artifact.kind == "final_result":
                started.set()
                if not release.wait(30):
                    raise TimeoutError("Test did not release final artifact storage.")
            return original_put(artifact)

        expected_expiry = self.clock() + timedelta(seconds=50)

        def renew(*args):
            result = original_renew(*args)
            # Observe the committed renewal, not an in-flight call or a mutable target.
            if result.expires_at >= expected_expiry:
                renewed.set()
            return result

        executor = RunExecutor(
            self.execution_service,
            lambda *_: WorkflowExecution(llm=FixedLLMAdapter(self.fixture["llm_responses"]),
                source=TrackingSource(), request=self._request()),
            owner_id="slow-finalization-worker", lease_seconds=30, heartbeat_seconds=0.05,
        )
        with patch.object(store, "put", side_effect=put), patch.object(self.uow, "renew_lease", side_effect=renew):
            task = asyncio.create_task(executor.run_once())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 30))
                self.clock.advance(20)
                self.assertTrue(await asyncio.to_thread(renewed.wait, 30))
                self.clock.advance(20)
                self.assertIsNone(await run_blocking(self.execution_service.claim_next_run, "competing-worker", 30))
                release.set()
                detail = await asyncio.wait_for(task, 30)
                self.assertIn(detail.status, {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS})
                self.assertEqual(detail.attempt, 1)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_workflow_deadline_stops_llm_without_waiting_for_heartbeat(self) -> None:
        self._create()
        llm = GatedLLM(self.fixture["llm_responses"])
        executor = RunExecutor(
            self.execution_service,
            lambda *_: WorkflowExecution(
                llm=llm, source=TrackingSource(), request=self._request(),
                deadline_at=time.monotonic() + 0.2,
            ), owner_id="deadline-worker", heartbeat_seconds=20,
        )
        detail = await asyncio.wait_for(executor.run_once(), timeout=3)
        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "run_deadline_exceeded")
        self.assertFalse(llm.release.is_set())

    async def test_crash_during_final_upload_resumes_exact_result_without_model_calls(self) -> None:
        run = self._create()
        store = self.execution_service.artifact_store
        original_put = store.put
        committed = []

        def put(artifact):
            if artifact.kind == "export_json":
                raise asyncio.CancelledError
            stored = original_put(artifact)
            if artifact.kind == "final_result":
                committed.append(stored)
            return stored

        source = TrackingSource()
        executor = RunExecutor(self.execution_service,
            lambda *_: WorkflowExecution(llm=FixedLLMAdapter(self.fixture["llm_responses"]),
                source=source, request=self._request()), owner_id="crashed-finalizer", lease_seconds=30)
        with patch.object(store, "put", side_effect=put):
            with self.assertRaises(asyncio.CancelledError):
                await executor.run_once()
        self.assertEqual(len(committed), 1)
        original = store.read(committed[0])
        calls = source.search_calls
        self.clock.advance(31)
        with patch.object(source, "search", side_effect=AssertionError("Must not repeat completed retrieval")):
            executor.resolve = lambda *_: WorkflowExecution(llm=FixedLLMAdapter([]), source=source, request=self._request())
            result = await executor.run_once()
        self.assertIn(result.status, {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS})
        self.assertEqual(result.attempt, 2)
        self.assertEqual(source.search_calls, calls)
        self.assertEqual(store.read(committed[0]), original)

    async def test_cancelled_storage_call_drains_before_resource_cleanup(self) -> None:
        started = threading.Event()
        release = threading.Event()
        written = threading.Event()

        def write():
            started.set()
            release.wait(timeout=5)
            written.set()

        task = asyncio.create_task(run_blocking(write))
        try:
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(written.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_sqlite_write_lock_does_not_block_event_loop(self) -> None:
        self._create()
        locked = threading.Event()
        release = threading.Event()
        unlocked = threading.Event()
        claiming = threading.Event()

        def hold_lock():
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(timeout=5)
                connection.rollback()
                unlocked.set()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        await asyncio.to_thread(locked.wait, 2)
        llm = GatedLLM(self.fixture["llm_responses"])
        executor = RunExecutor(
            self.execution_service,
            lambda *_: WorkflowExecution(llm=llm, source=TrackingSource(), request=self._request()),
            owner_id="lock-worker",
        )
        original_claim = self.execution_service.claim_next_run

        def claim(*args, **kwargs):
            claiming.set()
            return original_claim(*args, **kwargs)

        with patch.object(self.execution_service, "claim_next_run", side_effect=claim):
            task = asyncio.create_task(executor.run_once())
            try:
                self.assertTrue(await asyncio.to_thread(claiming.wait, 3))
                self.assertFalse(unlocked.is_set(), "The event loop must run while SQLite is blocked.")
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.to_thread(holder.join)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_concurrent_lane_checkpoint_failure_becomes_failed_run(self) -> None:
        self._create()
        original = self.execution_service.append_checkpoint

        def fail_lane(lease, events, checkpoint):
            if checkpoint.step_key == "retrieve.lane":
                raise RunPersistenceError("synthetic lane write failure")
            return original(lease, events, checkpoint)

        source = TrackingSource()
        executor = RunExecutor(
            self.execution_service,
            lambda *_: WorkflowExecution(
                llm=FixedLLMAdapter(self.fixture["llm_responses"]), source=source, request=self._request(),
            ), owner_id="lane-failure-worker",
        )
        with patch.object(self.execution_service, "append_checkpoint", side_effect=fail_lane):
            detail = await executor.run_once()
        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "checkpoint_commit_failed")

    async def test_cancel_during_configuration_failure_finishes_cancel_not_failed(self) -> None:
        run = self._create()

        def resolve(_run, _cancellation):
            self.submission_service.request_cancel(run.id)
            raise ValueError("The credential became unavailable during cancellation.")

        executor = RunExecutor(self.execution_service, resolve, owner_id="worker")
        result = await executor.run_once()
        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(result.error_code, "")
        self.assertEqual(self.uow.list_events(run.id)[-1].event_type, "run.cancelled")

    async def test_cancelled_worker_crash_releases_subject_and_fences_late_writes(self) -> None:
        run = self.submission_service.submit_run(
            subject="community", idempotency_key="first", request_digest="first",
            question="Cancel a crashed worker", source="openalex", deadline_seconds=60,
        )
        credentials = EncryptedRunCredentialStore(self.database)
        credentials.save(run.id, {"llm": {"api_key": "test-only-secret"}})
        lease = self.execution_service.claim_next_run("crashed", lease_seconds=30)
        self.submission_service.request_cancel(run.id)
        self.assertIsNone(self.execution_service.claim_next_run("replacement"))
        self.assertEqual(self.uow.get_run(run.id).status, RunStatus.CANCELLING)

        self.clock.advance(61)
        self.assertIsNone(self.execution_service.claim_next_run("replacement"))
        self.assertIsNone(self.execution_service.claim_next_run("replacement"))
        self.assertEqual(self.uow.get_run(run.id).status, RunStatus.CANCELLED)
        self.assertEqual(credentials.resolve(run.id), {})
        self.assertEqual(
            [event.event_type for event in self.uow.list_events(run.id)].count("run.cancelled"), 1,
        )
        with self.assertRaises(RunStateError):
            self.uow.complete_cancel(lease)
        with self.assertRaises(RunStateError):
            self.uow.append_events_and_checkpoint(
                lease, [PendingEvent(event_type="worker.late", stage="executor")], None, None,
            )
        following = self.submission_service.submit_run(
            subject="community", idempotency_key="second", request_digest="second",
            question="Next search", source="openalex",
        )
        self.assertEqual(self.execution_service.claim_next_run("replacement").run_id, following.id)

    async def test_cancel_recovery_racing_with_old_worker_is_safe(self) -> None:
        run = self._create()
        lease = self.execution_service.claim_next_run("old", lease_seconds=30)
        self.submission_service.request_cancel(run.id)
        self.clock.advance(31)
        complete = self.uow.complete_cancel

        def recover_then_complete(token):
            self.assertIsNone(self.execution_service.claim_next_run("replacement"))
            return complete(token)

        with patch.object(self.uow, "complete_cancel", side_effect=recover_then_complete):
            detail = self.execution_service.fail_claimed_run(lease, "run_configuration_invalid")
        self.assertEqual(detail.status, RunStatus.CANCELLED)
        self.assertEqual(detail.error_code, "")
        self.assertEqual(
            [event.event_type for event in self.uow.list_events(run.id)].count("run.cancelled"), 1,
        )

    async def test_corrupt_or_missing_checkpoint_fails_only_its_run(self) -> None:
        for damage in ("corrupt", "missing"):
            with self.subTest(damage=damage):
                run_id, _ = await self._crash_after_selection_checkpoint()
                checkpoint = self.uow.get_checkpoint(run_id)
                artifact = self.uow.get_artifact(checkpoint.state_artifact_id)
                path = self.data_dir / artifact.storage_key
                if damage == "missing":
                    path.unlink()
                else:
                    path.write_bytes(b"corrupted checkpoint")
                self.clock.advance(31)
                following = self._create()
                closed = []

                def resolve(run, _cancellation):
                    async def close():
                        closed.append(run.id)

                    return WorkflowExecution(
                        llm=FixedLLMAdapter(self.fixture["llm_responses"]),
                        source=TrackingSource(), request=self._request(), close=close,
                    )

                executor = RunExecutor(self.execution_service, resolve, owner_id="replacement")
                failed = await executor.run_once()
                self.assertEqual(failed.id, run_id)
                self.assertEqual(failed.status, RunStatus.FAILED)
                self.assertEqual(failed.error_code, "artifact_persistence_failed")
                completed = await executor.run_once()
                self.assertEqual(completed.id, following.id)
                self.assertEqual(completed.status, RunStatus.COMPLETED)
                self.assertEqual(closed, [run_id, following.id])

    async def test_configuration_failure_from_stale_worker_cannot_fail_replacement(self) -> None:
        run = self._create()

        def resolve(_run, _cancellation):
            self.clock.advance(61)
            self.execution_service.claim_next_run("replacement")
            raise ValueError("The old lease cannot load its credentials.")

        executor = RunExecutor(self.execution_service, resolve, owner_id="old")
        result = await executor.run_once()
        self.assertEqual(result.status, RunStatus.RUNNING)
        self.assertEqual(result.lease_owner, "replacement")
        self.assertEqual(result.error_code, "")
        self.assertFalse(any(event.event_type == "run.failed" for event in self.uow.list_events(run.id)))

    async def test_two_workers_competing_for_one_run_have_one_claim_winner(self) -> None:
        run = self._create()
        barrier = threading.Barrier(2)

        def claim(owner: str):
            barrier.wait(timeout=2)
            return self.execution_service.claim_next_run(owner, lease_seconds=30)

        outcomes = await asyncio.gather(
            asyncio.to_thread(claim, "worker-a"),
            asyncio.to_thread(claim, "worker-b"),
            return_exceptions=True,
        )

        self.assertFalse([item for item in outcomes if isinstance(item, Exception)])
        leases = [item for item in outcomes if item is not None]
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].run_id, run.id)
        self.assertEqual(self.query_service.get_run(run.id).attempt, 1)


    async def test_heartbeat_loss_stops_locally_without_failed_terminal_write(self) -> None:
        run = self._create()
        llm = GatedLLM(self.fixture["llm_responses"])

        def resolve(_run, _cancellation):
            return WorkflowExecution(llm=llm, source=TrackingSource(), request=self._request())

        executor = RunExecutor(
            self.execution_service,
            resolve,
            owner_id="losing-worker",
            lease_seconds=30,
            heartbeat_seconds=0.02,
        )
        with patch.object(
            self.uow,
            "renew_lease",
            side_effect=RunOwnershipLost("controlled lease loss"),
        ):
            detail = await asyncio.wait_for(executor.run_once(), timeout=0.5)

        self.assertEqual(detail.id, run.id)
        self.assertEqual(detail.status, RunStatus.RUNNING)
        self.assertNotIn(
            "run.failed",
            [event.event_type for event in self.query_service.list_events(run.id)],
        )

    async def test_cancel_monitor_database_failure_stops_upstream_before_its_timeout(self) -> None:
        run = self._create()
        llm = GatedLLM(self.fixture["llm_responses"])
        request_closed, client_closed = asyncio.Event(), asyncio.Event()
        original_chat, original_read = llm.chat_json, self.uow.get_run
        failure = RunPersistenceError("controlled database unavailable")

        async def tracked_chat(*args, **kwargs):
            try:
                return await original_chat(*args, **kwargs)
            finally:
                request_closed.set()

        async def close_client():
            client_closed.set()

        def read_until_request_started(run_id):
            if llm.started.is_set():
                raise failure
            return original_read(run_id)

        llm.chat_json = tracked_chat
        executor = RunExecutor(self.execution_service,
            lambda _run, _scope: WorkflowExecution(llm=llm, source=TrackingSource(),
                request=self._request(), close=close_client),
            owner_id="monitor-failure-worker", cancellation_poll_seconds=0.01,
            lease_seconds=60, heartbeat_seconds=20)
        with patch.object(self.uow, "get_run", side_effect=read_until_request_started):
            task = asyncio.create_task(executor.run_once())
            try:
                await asyncio.wait_for(llm.started.wait(), timeout=2)
                # wait() observes without cancelling: a watchdog must not manufacture the failure.
                done, _ = await asyncio.wait({task}, timeout=0.5)
                self.assertIn(task, done, "Cancellation monitor exited but upstream work is still waiting")
                with self.assertRaises(RunPersistenceError) as raised:
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIs(raised.exception, failure)
        self.assertTrue(llm.started.is_set())
        self.assertTrue(request_closed.is_set())
        self.assertTrue(client_closed.is_set())
        self.assertEqual(llm.calls, 1)
        self.assertFalse(llm.release.is_set())
        self.assertEqual(original_read(run.id).status, RunStatus.RUNNING)
        self.assertEqual(self.uow.get_results(run.id), [])
        self.assertEqual(self.uow.list_artifacts(run.id), [])
        self.assertFalse(any(event.event_type in {"run.completed", "run.failed"}
                             for event in self.uow.list_events(run.id)))

    async def test_process_rebuild_reclaims_and_resumes_last_complete_stage(self) -> None:
        run_id, crashed_source = await self._crash_after_selection_checkpoint()
        self.assertEqual(crashed_source.search_calls, 1)
        self.clock.advance(31)
        self.uow.close()

        rebuilt_uow = SQLiteRunUnitOfWork(self.database, clock=self.clock)
        self.uow = rebuilt_uow
        rebuilt_store = FileArtifactStore(self.data_dir)
        rebuilt_execution = RunExecutionService(rebuilt_uow, rebuilt_store)
        rebuilt_query = RunQueryService(rebuilt_uow, rebuilt_store)
        resumed_source = TrackingSource()

        def resolve(_run, _cancellation):
            return WorkflowExecution(
                llm=FixedLLMAdapter(self.fixture["llm_responses"][2:]),
                source=resumed_source,
                request=self._request(),
            )

        detail = await RunExecutor(
            rebuilt_execution,
            resolve,
            owner_id="worker-after-crash",
            lease_seconds=30,
            heartbeat_seconds=1,
        ).run_once()

        self.assertEqual(detail.id, run_id)
        self.assertEqual(detail.status, RunStatus.COMPLETED)
        self.assertEqual(detail.attempt, 2)
        self.assertEqual(resumed_source.search_calls, 0)
        self.assertIn(
            "query.resumed",
            [event.event_type for event in rebuilt_query.list_events(run_id)],
        )
        recovery_event = next(
            event for event in rebuilt_query.list_events(run_id)
            if event.event_type == "workflow.recovery"
        )
        self.assertEqual(
            recovery_event.payload,
            {"query": 1, "retrieval": 1, "citation": 0, "selection": 1, "ranking": 0},
        )
        audit = rebuilt_query.get_run_audit(run_id)
        self.assertEqual(audit.recovery["query"], 1)
        self.assertEqual(audit.recovery["retrieval"], 1)
        self.assertEqual(audit.recovery["selection"], 1)
        self.assertEqual(audit.recovery["total"], 3)
        # The recovered final result must retain the original analysis corpus,
        # not silently fall back to the scored result subset.
        corpus = json.loads(rebuilt_query.get_bibliometric_corpus(run_id).content)
        self.assertGreaterEqual(len(corpus['papers']), detail.result_count)
        self.assertEqual(corpus['scope'], 'retrieved_candidates')

    async def test_incompatible_checkpoint_fails_instead_of_guessing_state(self) -> None:
        run_id, _ = await self._crash_after_selection_checkpoint()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE run_checkpoints SET workflow_version = 'unsupported.v0' WHERE run_id = ?",
                (run_id,),
            )
            connection.commit()
        self.clock.advance(31)

        detail = await RunExecutor(
            self.execution_service,
            lambda _run, _cancellation: WorkflowExecution(
                llm=FixedLLMAdapter(self.fixture["llm_responses"][2:]),
                source=TrackingSource(),
                request=self._request(),
            ),
            owner_id="version-worker",
            heartbeat_seconds=1,
        ).run_once()

        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "incompatible_checkpoint")

    async def test_incompatible_checkpoint_schema_fails_explicitly(self) -> None:
        run_id, _ = await self._crash_after_selection_checkpoint()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE run_checkpoints SET schema_version = 99 WHERE run_id = ?",
                (run_id,),
            )
            connection.commit()
        self.clock.advance(31)

        detail = await RunExecutor(
            self.execution_service,
            lambda _run, _cancellation: WorkflowExecution(
                llm=FixedLLMAdapter(self.fixture["llm_responses"][2:]),
                source=TrackingSource(),
                request=self._request(),
            ),
            owner_id="schema-worker",
            heartbeat_seconds=1,
        ).run_once()

        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "incompatible_checkpoint")

    async def test_three_reclaims_then_recovery_exhaustion_and_deadline_failure(self) -> None:
        run = self._create()
        claims = []
        for index in range(4):
            lease = self.execution_service.claim_next_run(f"worker-{index}", lease_seconds=30)
            claims.append(lease)
            self.assertEqual(lease.run_id, run.id)
            self.clock.advance(31)

        self.assertIsNone(self.execution_service.claim_next_run("worker-exhausted", lease_seconds=30))
        exhausted = self.query_service.get_run(run.id)
        self.assertEqual(exhausted.attempt, 4)
        self.assertEqual(exhausted.status, RunStatus.FAILED)
        self.assertEqual(exhausted.error_code, "executor_recovery_exhausted")

        deadline_run = self._create(deadline_seconds=10)
        self.assertEqual(deadline_run.deadline_at, self.clock() + timedelta(seconds=10))
        self.clock.advance(11)
        self.assertIsNone(self.execution_service.claim_next_run("deadline-worker", lease_seconds=30))
        deadline = self.query_service.get_run(deadline_run.id)
        self.assertEqual(deadline.status, RunStatus.FAILED)
        self.assertEqual(deadline.error_code, "run_deadline_exceeded")


if __name__ == "__main__":
    unittest.main()
