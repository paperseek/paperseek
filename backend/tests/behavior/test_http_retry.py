from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import httpx
from fixtures.retrieval import RecordingObserver, record
from paperseek_app.execution import CancellationScope
from paperseek_core.candidate_selection import CandidateSelectionPipeline
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.sources.contracts import SourceSearchRequest
from paperseek_core.sources.google_scholar import GoogleScholarSource
from paperseek_core.sources.http import AsyncHttpTransport

import asyncio
import json
from collections import Counter
from email.utils import format_datetime


from paperseek_core.citation import CitationPipeline
from paperseek_core.decision_client import SystemOneAdapter
from paperseek_core.execution import NEVER_CANCELLED, RequestContext
from paperseek_core.http_retry import failure_details, request_with_retries, retry_delay
from paperseek_core.ranking import RankingPipeline, RankingRequest
from paperseek_core.retrieval_clients import (
    ExternalEmbeddingAdapter,
    ExternalRerankerAdapter,
)
from paperseek_core.sources.contracts import SourceError
from paperseek_core.sources.wos import WosSource


class HttpRetryContract(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_key_is_not_reused_after_a_later_transient_failure(self):
        requests = []
        async def handle(request):
            requests.append(request.headers['Authorization'])
            status = 401 if len(requests) == 1 else 503 if len(requests) <= 4 else 200
            return httpx.Response(status, json={'output_text': '{}'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = HttpLLMAdapter(client=client, cancellation=NEVER_CANCELLED, api_type='openai_responses',
                                model='fixture', base_url='https://fixture.invalid', api_key='bad-key,key-b,key-c')
            with patch('paperseek_core.http_retry.retry_delay', return_value=0):
                self.assertEqual(await llm.chat_json([{'role': 'user', 'content': 'q'}]), '{}')
        self.assertEqual(requests, ['Bearer bad-key', 'Bearer key-b', 'Bearer key-c', 'Bearer key-b', 'Bearer key-c'])

    async def test_one_key_recovers_transient_failure_in_all_model_protocols(self):
        for kind in ('openai_chat', 'openai_responses', 'anthropic_messages', 'embedding', 'rerank'):
            with self.subTest(kind=kind):
                requests = []
                async def handle(request):
                    requests.append(request)
                    if len(requests) <= 3:
                        return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
                    return httpx.Response(200, json={
                        'choices': [{'message': {'content': '{}'}}], 'output_text': '{}',
                        'content': [{'type': 'text', 'text': '{}'}],
                        'data': [{'index': 0, 'embedding': [1, 0]}, {'index': 1, 'embedding': [1, 0]}],
                        'results': [{'index': 0, 'relevance_score': 0.8}],
                    })
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    args = dict(model='fixture', base_url='https://fixture.invalid/v1', api_key='single-secret')
                    with patch('paperseek_core.http_retry.retry_delay', return_value=0):
                        if kind == 'embedding':
                            await ExternalEmbeddingAdapter(client, NEVER_CANCELLED, provider='fixture', **args).embedding_scores('q', [record('1')])
                        elif kind == 'rerank':
                            await ExternalRerankerAdapter(client, NEVER_CANCELLED, provider='fixture', **args).rerank('q', [record('1')])
                        else:
                            await HttpLLMAdapter(client=client, cancellation=NEVER_CANCELLED, api_type=kind, **args).chat_json([{'role': 'user', 'content': 'q'}])
                self.assertEqual(len(requests), 4)
                self.assertEqual(len({request.content for request in requests}), 1)

    async def test_decision_retries_only_the_failed_batch_and_retains_successes(self):
        counts = Counter()
        async def handle(request):
            questions = json.loads(request.content)['questions']
            batch = tuple(questions)
            counts[batch] += 1
            if 'uid:8' in batch and counts[batch] <= 3:
                return httpx.Response(503)
            return httpx.Response(200, json={'model': 'SemIf', 'answers': {key: {'type': 'score', 'score': 2} for key in questions}})
        observer = RecordingObserver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = SystemOneAdapter(AsyncHttpTransport(client=client, sleep=AsyncMock()), provider='siliconflow',
                                       model='SemIf', base_url='https://fixture.invalid/v1', api_key='secret')
            result = await CandidateSelectionPipeline(observer, decision=adapter).run(
                question='research', candidates=[record(str(i)) for i in range(16)], provenance={},
                embedding_state=None, pool_max=20, ranking_limit=8,
            )
        self.assertEqual(sorted(counts.values()), [1, 4])
        self.assertEqual(result.actual_mode, 'decision')
        self.assertFalse(result.warnings)
        self.assertEqual(sum(row.step_key == 'selection.decision' for row in observer.checkpoints), 2)

    async def test_exhausted_decision_failure_reports_safe_status_code_and_attempts(self):
        observer = RecordingObserver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503, json={
            'code': 20015, 'message': 'private-secret-and-prompt',
        }))) as client:
            adapter = SystemOneAdapter(AsyncHttpTransport(client=client, sleep=AsyncMock()), provider='siliconflow',
                                       model='SemIf', base_url='https://fixture.invalid/v1', api_key='secret')
            result = await CandidateSelectionPipeline(observer, decision=adapter).run(
                question='private-question', candidates=[record('1')], provenance={}, embedding_state=None, pool_max=20, ranking_limit=8,
            )
        event = next(row for row in observer.events if row.event_type == 'selection.decision_degraded')
        self.assertEqual((event.payload['http_status'], event.payload['attempts'], event.payload['provider_error_code']), (503, 5, 20015))
        self.assertEqual(result.actual_mode, 'local_fusion')
        self.assertNotIn('private-', str(event.payload))
        self.assertNotIn('secret', str(event.payload))

    async def test_permanent_errors_do_not_repeat_and_transients_are_bounded(self):
        for status, body, expected in ((400, {}, 1), (401, {}, 1), (403, {}, 1), (402, {}, 1),
                                      (429, {'error': {'code': 'insufficient_quota'}}, 1), (429, {}, 5), (529, {}, 5)):
            send = AsyncMock(return_value=httpx.Response(status, json=body))
            response, attempts = await request_with_retries(send, cancellation=NEVER_CANCELLED, sleep=AsyncMock())
            self.assertEqual((send.call_count, attempts), (expected, expected))
            self.assertEqual(response.extensions['paperseek_retry']['attempts'], expected)

    async def test_transport_error_retries_with_one_key_and_keeps_safe_diagnostics(self):
        send = AsyncMock(side_effect=httpx.ConnectError('private endpoint secret'))
        with self.assertRaises(httpx.ConnectError) as caught:
            await request_with_retries(send, cancellation=NEVER_CANCELLED, sleep=AsyncMock())
        self.assertEqual(send.call_count, 5)
        self.assertEqual(failure_details(caught.exception)['attempts'], 5)
        self.assertNotIn('private', str(failure_details(caught.exception)))

    async def test_retry_after_seconds_date_and_wait_budget(self):
        self.assertEqual(retry_delay(httpx.Response(429, headers={'Retry-After': '32'}), 1), 32)
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        delay = retry_delay(httpx.Response(503, headers={'Retry-After': format_datetime(when)}), 1)
        self.assertTrue(28 <= delay <= 30)
        with patch('paperseek_core.http_retry.random.uniform', return_value=0):
            self.assertEqual([retry_delay(None, i) for i in range(1, 5)], [1, 2, 4, 8])
        send = AsyncMock(return_value=httpx.Response(429, headers={'Retry-After': '120'}))
        sleep = AsyncMock()
        response, attempts = await request_with_retries(send, cancellation=NEVER_CANCELLED, sleep=sleep)
        self.assertEqual(attempts, 1)
        self.assertTrue(response.extensions['paperseek_retry']['retry_wait_budget_exhausted'])
        sleep.assert_not_called()

    async def test_cancel_during_backoff_does_not_send_again(self):
        cancellation = CancellationScope()
        entered = asyncio.Event()
        drained = asyncio.Event()
        async def sleep(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        send = AsyncMock(return_value=httpx.Response(429))
        task = asyncio.create_task(request_with_retries(send, cancellation=cancellation, sleep=sleep))
        await asyncio.wait_for(entered.wait(), 1)
        cancellation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        self.assertTrue(drained.is_set())
        self.assertEqual(send.call_count, 1)

    async def test_source_deadline_still_bounds_retry_wait(self):
        import time
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': '32'}))) as client:
            transport = AsyncHttpTransport(client=client, context=RequestContext(step_key='source', deadline_at=time.monotonic()+0.05))
            with self.assertRaises(SourceError) as caught:
                await transport.request('fixture', 'GET', 'https://fixture.invalid')
        self.assertEqual(caught.exception.failure_kind.value, 'step_deadline')

    async def test_wos_recovers_transient_server_failure(self):
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(503 if len(calls) == 1 else 200, json={'metadata': {'total': 0}, 'hits': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = WosSource(transport=AsyncHttpTransport(client=client, sleep=AsyncMock()), api_key='secret')
            await source.search(SourceSearchRequest(query='TS=(research)'))
        self.assertEqual(len(calls), 2)

    async def test_final_ranking_does_not_multiply_exhausted_request_retries(self):
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(503)
        observer = RecordingObserver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            llm = HttpLLMAdapter(client=client, cancellation=NEVER_CANCELLED, api_type='openai_responses',
                                model='fixture', base_url='https://fixture.invalid', api_key='secret')
            with patch('paperseek_core.http_retry.retry_delay', return_value=0):
                result = await RankingPipeline(llm, observer).run(RankingRequest(
                    question='research', documents=[record(str(i)) for i in range(4)], batch_size=2, concurrency=4,
                ))
        self.assertEqual(len(calls), 10)  # Two original batches, five attempts each; no re-splitting/replay.
        self.assertTrue(result.warnings)
        failures = [row for row in observer.events if row.event_type == 'ranking.batch_failed']
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(row.payload['attempts'] == 5 for row in failures))

    async def test_citation_does_not_restart_traversal_after_request_retry_exhaustion(self):
        failure = SourceError('openalex', 'unavailable', status=503, retry_details={'attempts': 3})
        group = ExceptionGroup('TaskGroup failure', [failure])
        self.assertEqual(failure_details(group)['attempts'], 3)
        self.assertFalse(CitationPipeline._retryable(group))


class CredentialRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_errors_do_not_consume_key_authentication_budget(self):
        calls = []

        def respond(request):
            calls.append(request.headers["authorization"])
            return httpx.Response(
                [503, 401, 200][len(calls) - 1],
                json={"choices": [{"message": {"content": "{}"}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = HttpLLMAdapter(
                client=client,
                cancellation=CancellationScope(),
                api_type="openai_chat",
                base_url="https://test.invalid",
                model="test",
                api_key="keyA,keyB",
            )
            with patch("paperseek_core.http_retry.retry_delay", return_value=0):
                self.assertEqual(await adapter.chat_json([]), "{}")
        self.assertEqual(calls, ["Bearer keyA", "Bearer keyB", "Bearer keyA"])

    async def test_source_key_rotation_survives_mixed_transient_and_auth_failures(self):
        calls = []

        def respond(request):
            calls.append(request.headers["x-api-key"])
            return httpx.Response(
                [503, 401, 200][len(calls) - 1],
                json={"organic": [{"id": "1", "title": "test"}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            source = GoogleScholarSource(
                transport=AsyncHttpTransport(client=client, sleep=AsyncMock()),
                api_key="keyA,keyB",
            )
            result = await source.search(SourceSearchRequest(query="test", limit=1))
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(calls, ["keyA", "keyB", "keyA"])
