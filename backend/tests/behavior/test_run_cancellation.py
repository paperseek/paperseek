from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

from paperseek_app.errors import RunOwnershipLost, RunStateError
from paperseek_app.execution import CancellationScope, WorkflowExecution
from paperseek_app.models import ArtifactRole, LeaseToken, PendingEvent, RunStatus
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_app.worker import RunExecutor
from paperseek_community.api import create_app
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_core.execution import RequestContext
from paperseek_core.models import (
    CitationSourceRequest,
    CitationSourceResult,
    QueryLoopBudget,
    ResearchWorkflowRequest,
)
from paperseek_core.ports import CancellationToken
from paperseek_core.prompts import SYSTEM_RESULT_RANKING
from paperseek_core.sources.contracts import (
    SourceError,
    SourceFailureKind,
    SourceSearchMetadata,
    SourceSearchRequest,
    SourceSearchResult,
)
from paperseek_core.sources.http import AsyncHttpTransport

FIXTURE = Path(__file__).parent / "fixtures" / "m1_fixed_source.json"


class _SlowServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SlowHandler)
        self.started: dict[str, threading.Event] = {}
        self.finished: dict[str, threading.Event] = {}
        self.request_counts: dict[str, int] = {}
        self.delay_seconds = 0.35
        self.release = threading.Event()

    def event(self, collection: dict[str, threading.Event], key: str) -> threading.Event:
        return collection.setdefault(key, threading.Event())


class _SlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        key = self.path.lstrip("/")
        server: _SlowServer = self.server  # type: ignore[assignment]
        server.request_counts[key] = server.request_counts.get(key, 0) + 1
        server.event(server.started, key).set()
        payload = b'{"ok":true}'
        try:
            if key == "read-idle":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:1])
                self.wfile.flush()
                time.sleep(server.delay_seconds)
                self.wfile.write(payload[1:])
                self.wfile.flush()
            else:
                if key in {"token-cancel", "retrieval", "ranking", "citation"}:
                    if not server.release.wait(10):
                        return
                else:
                    time.sleep(server.delay_seconds)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            server.event(server.finished, key).set()

    def log_message(self, _format: str, *_args) -> None:
        return None


class _InjectedFailureClient:
    def __init__(self, failure: httpx.RequestError) -> None:
        self.failure = failure

    async def request(self, method: str, url: str, **_kwargs) -> httpx.Response:
        request = httpx.Request(method, url)
        if self.failure.request is None:
            self.failure.request = request
        raise self.failure


class _StageRequest:
    def __init__(self, base_url: str, stage: str, token: CancellationToken) -> None:
        self.stage = stage
        self.transport = AsyncHttpTransport(
            context=RequestContext(step_key=stage, cancellation=token),
        )
        self.url = f"{base_url}/{stage}"

    async def hit(self) -> None:
        await self.transport.request(
            "fixture",
            "GET",
            self.url,
            timeout=2.0,
            attempts=1,
        )

    async def aclose(self) -> None:
        await self.transport.aclose()


class _StageLLM:
    def __init__(self, delegate: FixedLLMAdapter, request: _StageRequest, stage: str) -> None:
        self.delegate = delegate
        self.request = request
        self.stage = stage

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        if self.stage == "ranking" and messages[0]["content"] == SYSTEM_RESULT_RANKING:
            await self.request.hit()
        return await self.delegate.chat_json(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )


class _StageSource:
    def __init__(self, stage: str, request: _StageRequest) -> None:
        self.delegate = FixedSourceAdapter.from_file(FIXTURE)
        self.fixture = self.delegate.fixture
        self.stage = stage
        self.request = request
        self.name = self.delegate.name
        self.capabilities = self.delegate.capabilities
        if stage == "retrieval":
            self.capabilities = self.capabilities.model_copy(
                update={"max_lane_limit": 101, "max_page_size": 100}
            )
        elif stage == "citation":
            self.capabilities = self.capabilities.model_copy(
                update={"supports_citations": True, "supports_citation_expansion": True}
            )

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        if self.stage != "retrieval":
            result = await self.delegate.search(request)
            return result

        if request.query != self.fixture.question:
            raise ValueError("The slow retrieval fixture received an unexpected query.")
        if request.page == 1:
            base = self.fixture.records[0]
            records = [
                base.model_copy(
                    update={
                        "uid": f"fixed-{index:03d}",
                        "title": f"Synthetic retrieval candidate {index:03d}",
                    }
                )
                for index in range(1, 101)
            ]
            return SourceSearchResult(
                metadata=SourceSearchMetadata(total=101, page=1, limit=100),
                hits=records,
            )
        await self.request.hit()
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=100, page=request.page, limit=request.limit),
            hits=[],
        )

    async def traverse_citations(
        self,
        _request: CitationSourceRequest,
    ) -> CitationSourceResult:
        await self.request.hit()
        return CitationSourceResult(frontier={"stop_reason": "fixture"})



class M3CancellationRequestLifecycleContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"NO_PROXY": "localhost,127.0.0.1,::1"}))
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.database = self.data_dir / "paperseek.db"
        migrate_database(self.database)
        self.uow = SQLiteRunUnitOfWork(self.database)
        store = FileArtifactStore(self.data_dir)
        self.submission_service = RunSubmissionService(self.uow)
        self.execution_service = RunExecutionService(self.uow, store)
        self.query_service = RunQueryService(self.uow, store)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.server = _SlowServer()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    async def asyncTearDown(self) -> None:
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.uow.close()
        self.temp.cleanup()

    def _create_run(self):
        return self.submission_service.create_run(
            self.fixture["question"],
            self.fixture["source"],
            {"fixture_id": self.fixture["fixture_id"]},
        )

    async def test_queued_cancel_is_atomic_idempotent_and_terminal(self) -> None:
        run = self._create_run()
        cancelled = self.submission_service.request_cancel(run.id)
        repeated = self.submission_service.request_cancel(run.id)

        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertEqual(repeated.status, RunStatus.CANCELLED)
        self.assertIsNotNone(cancelled.finished_at)
        self.assertEqual(
            [event.event_type for event in self.query_service.list_events(run.id)],
            ["run.created", "run.cancelled"],
        )
        with self.assertRaises(RunStateError):
            self.uow.claim_run(run.id, "late-worker", timedelta(seconds=60))

    async def test_running_cancel_fences_writes_and_only_executor_finishes_cancel(self) -> None:
        run = self._create_run()
        lease = self.uow.claim_run(run.id, "worker-a", timedelta(seconds=60))
        cancelling = self.submission_service.request_cancel(run.id)

        self.assertEqual(cancelling.status, RunStatus.CANCELLING)
        with self.assertRaises((RunOwnershipLost, RunStateError)):
            self.uow.append_events_and_checkpoint(
                lease,
                [PendingEvent(event_type="late.write", stage="fixture")],
                None,
                None,
            )
        cancelled = self.execution_service.complete_cancel(lease)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertEqual(
            [event.event_type for event in self.query_service.list_events(run.id)][-2:],
            ["run.cancelling", "run.cancelled"],
        )

    async def test_cancel_api_acknowledges_queued_and_running_states(self) -> None:
        app = create_app(self.database)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://paperseek.test") as client:
            queued = self._create_run()
            response = await asyncio.wait_for(client.post(f"/api/v1/runs/{queued.id}/cancel"), 5)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "cancelled")

            running = self._create_run()
            self.uow.claim_run(running.id, "api-worker", timedelta(seconds=60))
            response = await asyncio.wait_for(client.post(f"/api/v1/runs/{running.id}/cancel"), 5)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "cancelling")

    async def test_timeout_phases_are_stable_and_redacted(self) -> None:
        cases = (
            (
                httpx.ConnectTimeout("private connect detail", request=httpx.Request("GET", "http://fixture.invalid")),
                SourceFailureKind.CONNECT_TIMEOUT,
            ),
            (
                httpx.WriteTimeout("private send detail", request=httpx.Request("GET", "http://fixture.invalid")),
                SourceFailureKind.SEND_TIMEOUT,
            ),
        )
        for failure, expected in cases:
            with self.subTest(kind=expected):
                boundary = AsyncHttpTransport(client=_InjectedFailureClient(failure), sleep=lambda _: asyncio.sleep(0))
                with self.assertRaises(SourceError) as raised:
                    await boundary.request("fixture", "GET", "http://fixture.invalid", attempts=1)
                self.assertEqual(raised.exception.failure_kind, expected)
                self.assertNotIn("private", str(raised.exception))

        timeout = httpx.Timeout(connect=1.0, read=0.05, write=1.0, pool=1.0)
        boundary = AsyncHttpTransport()
        try:
            with self.assertRaises(SourceError) as first_byte:
                await boundary.request(
                    "fixture",
                    "GET",
                    f"{self.base_url}/first-byte",
                    timeout=timeout,
                    attempts=1,
                )
            self.assertEqual(first_byte.exception.failure_kind, SourceFailureKind.FIRST_BYTE_TIMEOUT)

            with self.assertRaises(SourceError) as read_idle:
                await boundary.request(
                    "fixture",
                    "GET",
                    f"{self.base_url}/read-idle",
                    timeout=timeout,
                    attempts=1,
                )
            self.assertEqual(read_idle.exception.failure_kind, SourceFailureKind.READ_IDLE_TIMEOUT)

            with self.assertRaises(SourceError) as deadline:
                await boundary.request(
                    "fixture",
                    "GET",
                    f"{self.base_url}/step-deadline",
                    timeout=httpx.Timeout(1.0),
                    attempts=1,
                    context=RequestContext(
                        step_key="deadline",
                        deadline_at=time.monotonic() + 0.05,
                    ),
                )
            self.assertEqual(deadline.exception.failure_kind, SourceFailureKind.STEP_DEADLINE)
        finally:
            await boundary.aclose()

    async def test_transport_token_cancels_a_real_slow_request_without_accepting_its_result(self) -> None:
        cancellation = CancellationScope()
        boundary = AsyncHttpTransport(
            context=RequestContext(step_key="token-cancel", cancellation=cancellation)
        )
        request = asyncio.create_task(
            boundary.request(
                "fixture",
                "GET",
                f"{self.base_url}/token-cancel",
                timeout=2.0,
                attempts=1,
            )
        )
        try:
            received = await asyncio.to_thread(
                self.server.event(self.server.started, "token-cancel").wait,
                2,
            )
            self.assertTrue(received)
            cancellation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(request, timeout=5)
            self.assertFalse(self.server.event(self.server.finished, "token-cancel").is_set())
            self.server.release.set()
            finished = await asyncio.to_thread(
                self.server.event(self.server.finished, "token-cancel").wait,
                2,
            )
            self.assertTrue(finished)
        finally:
            self.server.release.set()
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await boundary.aclose()

    async def test_real_http_stop_covers_retrieval_ranking_and_citation_without_late_writes(self) -> None:
        for stage in ("retrieval", "ranking", "citation"):
            with self.subTest(stage=stage):
                self.server.release.clear()
                stage_dir = self.data_dir / stage
                stage_dir.mkdir()
                database = stage_dir / "paperseek.db"
                migrate_database(database)
                uow = SQLiteRunUnitOfWork(database)
                store = FileArtifactStore(stage_dir)
                submission_service = RunSubmissionService(uow)
                execution_service = RunExecutionService(uow, store)
                query_service = RunQueryService(uow, store)
                requests: list[_StageRequest] = []

                def resolve(_run, token: CancellationToken) -> WorkflowExecution:
                    slow_request = _StageRequest(self.base_url, stage, token)
                    requests.append(slow_request)
                    source = _StageSource(stage, slow_request)
                    llm = _StageLLM(
                        FixedLLMAdapter(source.fixture.llm_responses),
                        slow_request,
                        stage,
                    )
                    budget = QueryLoopBudget(
                        target_min=1,
                        target_max=100 if stage == "retrieval" else 50,
                        retrieval_pool_min=1,
                        retrieval_lane_limit=101 if stage == "retrieval" else 500,
                        ranking_candidate_limit=101 if stage == "retrieval" else 256,
                        query_portfolio_enabled=False,
                    )
                    return WorkflowExecution(
                        llm=llm,
                        source=source,
                        request=ResearchWorkflowRequest(
                            question=source.fixture.question,
                            query_budget=budget,
                            citation_enabled=stage == "citation",
                        ),
                    )

                executor = RunExecutor(
                    execution_service,
                    resolve,
                    owner_id=f"worker-{stage}",
                    cancellation_poll_seconds=0.02,
                )
                run = submission_service.create_run(
                    self.fixture["question"],
                    self.fixture["source"],
                    {"stage": stage},
                )
                task = asyncio.create_task(executor.run_once())
                try:
                    received = await asyncio.to_thread(
                        self.server.event(self.server.started, stage).wait,
                        3,
                    )
                    self.assertTrue(received, f"{stage} never reached the real slow HTTP endpoint")
                    running = query_service.get_run(run.id)
                    stale_lease = LeaseToken(
                        run_id=run.id,
                        owner_id=running.lease_owner,
                        lease_generation=running.lease_generation,
                        expires_at=running.lease_expires_at,
                    )
                    app = create_app(database)
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://paperseek.test",
                    ) as client:
                        response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run.id}/cancel"), 5)
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json()["status"], "cancelling")

                    cancelled = await asyncio.wait_for(task, timeout=5)
                    self.assertEqual(cancelled.status, RunStatus.CANCELLED)
                    self.assertFalse(self.server.event(self.server.finished, stage).is_set())
                    self.server.release.set()
                    finished = await asyncio.to_thread(
                        self.server.event(self.server.finished, stage).wait,
                        2,
                    )
                    self.assertTrue(finished, "The controlled late response did not finish.")
                    self.assertEqual(
                        self.server.request_counts[stage],
                        1,
                        "Cancellation must not schedule a replacement request.",
                    )

                    events = query_service.list_events(run.id)
                    cancelling_index = next(
                        index for index, event in enumerate(events) if event.event_type == "run.cancelling"
                    )
                    self.assertEqual(
                        [event.event_type for event in events[cancelling_index + 1:]],
                        ["run.cancelled"],
                    )
                    self.assertEqual(query_service.get_results(run.id), [])
                    self.assertFalse(
                        any(
                            artifact.role is ArtifactRole.COMPLETION_REQUIRED
                            for artifact in query_service.list_artifacts(run.id)
                        )
                    )
                    with self.assertRaises((RunOwnershipLost, RunStateError)):
                        uow.append_events_and_checkpoint(
                            stale_lease,
                            [PendingEvent(event_type="late.http.write", stage=stage)],
                            None,
                            None,
                        )
                finally:
                    self.server.release.set()
                    if not task.done():
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    for request in requests:
                        await request.aclose()
                    uow.close()


if __name__ == "__main__":
    unittest.main()
