"""Zero-result recovery must preserve intent and never disguise upstream failures."""
import asyncio
import gzip
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fixtures.query import (
    FixtureLLM,
    RecordingObserver,
    SequenceSource,
    StopAfterCheckpoint,
    source_result,
)

from paperseek_app.execution import CancellationScope
from paperseek_core.errors import QuerySourceError
from paperseek_core.execution import RequestContext
from paperseek_core.models import (
    PaperRecord,
    QueryLoopBudget,
    QueryLoopRequest,
    QueryLoopResumeState,
    SearchPlan,
)
from paperseek_core.query_loop import QueryLoopPipeline
from paperseek_core.sources.contracts import SourceError, SourceSearchRequest
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.pubmed import PubMedSource
from paperseek_core.sources.rate_limit import (
    rate_limit_backoff,
    reset_source_rate_limits,
)

APPROVED = json.dumps({'core_concepts_preserved': True, 'required_relation_preserved': True,
                       'constraints_preserved': True})


class FreshSourceLimits(unittest.IsolatedAsyncioTestCase):
    """Source pacing is process-wide; each test starts without earlier cooldowns."""
    def setUp(self):
        reset_source_rate_limits()


class EmptyRecoveryContract(FreshSourceLimits):
    def request(self, source='openalex'):
        return QueryLoopRequest(question='Therapy for adult depression, excluding children',
            budget=QueryLoopBudget(query_portfolio_enabled=False, max_iterations=1, adaptive_max_iterations=1, field_ids=('field-unchanged',)),
            resume=QueryLoopResumeState(question='Therapy for adult depression, excluding children', source=source,
                current_query='TS=(therapy AND depression AND adult AND "optional phrase" NOT children)' if source == 'wos' else 'therapy AND depression AND adult AND "optional phrase" NOT children',
                search_plan=SearchPlan(intent='Therapy for adult depression', core_concepts=['therapy', 'depression'],
                    must_have_facets=['adult'], optional_facets=['optional phrase'],
                    required_relation='therapy treats depression', boundaries=['exclude children'])))

    async def test_all_sources_get_one_checked_recovery_preserving_filters(self):
        for name in ('openalex', 'arxiv', 'crossref', 'semanticscholar', 'pubmed', 'wos', 'googlescholar', 'paperhub'):
            with self.subTest(source=name):
                request = self.request(name)
                candidate = 'therapy AND depression AND adult NOT children'
                if name == 'wos':
                    candidate = f'TS=({candidate})'
                elif name in {'crossref', 'semanticscholar'}:
                    candidate = 'therapy depression adult'
                    question = 'Therapy for adult depression'
                    request = request.model_copy(update={'question': question, 'resume': request.resume.model_copy(update={
                        'question': question, 'current_query': 'therapy depression adult optional phrase',
                        'search_plan': request.resume.search_plan.model_copy(update={'boundaries': []}),
                    })})
                elif name in {'googlescholar', 'paperhub'}:
                    candidate = 'therapy depression adult -children'
                    request = request.model_copy(update={'resume': request.resume.model_copy(update={
                        'current_query': 'therapy depression adult "optional phrase" -children',
                    })})
                source = SequenceSource(source_result([], total=0), source_result([PaperRecord(uid='p', title='Adult depression therapy')], total=1))
                source.name = name
                llm = FixtureLLM(json.dumps({'query': candidate}), APPROVED)
                result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(request)
                self.assertEqual(len(source.requests), 2)
                self.assertEqual(result.total, 1)
                self.assertEqual(result.final_query, candidate)
                self.assertEqual(source.requests[-1].field_ids, ('field-unchanged',))
                self.assertIn('query_empty_recovered', result.warnings)
                for call in llm.calls:
                    text = str(call['messages'])
                    self.assertIn('adult', text)
                    if name not in {'crossref', 'semanticscholar'}:
                        self.assertIn('exclude children', text)
                    self.assertIn('therapy treats depression', text)

    async def test_unsafe_or_invalid_recovery_is_not_executed(self):
        for verdict in ('{}', '{"core_concepts_preserved":"true"}',
                        json.dumps({'core_concepts_preserved': True, 'required_relation_preserved': True, 'constraints_preserved': False})):
            with self.subTest(verdict=verdict):
                source = SequenceSource(source_result([], total=0))
                result = await QueryLoopPipeline(FixtureLLM('{"query":"depression"}', verdict), source, RecordingObserver()).run(self.request())
                self.assertEqual(len(source.requests), 1)
                self.assertEqual(result.total, 0)
                self.assertIn('query_empty_recovery_rejected', result.warnings)

    async def test_checkpoint_resume_runs_the_checked_query_without_asking_again(self):
        observer = RecordingObserver(stop_phase='next_query')
        source = SequenceSource(source_result([], total=0))
        with self.assertRaises(StopAfterCheckpoint):
            await QueryLoopPipeline(FixtureLLM('{"query":"therapy AND depression AND adult NOT children"}', APPROVED), source, observer).run(self.request())
        request = self.request().model_copy(update={'resume': QueryLoopResumeState.model_validate(observer.checkpoints[-1].state)})
        recovery_source = SequenceSource(source_result([], total=0))
        llm = FixtureLLM('{"query":"therapy AND depression AND adults"}', '{}')
        result = await QueryLoopPipeline(llm, recovery_source, RecordingObserver()).run(request)
        self.assertEqual([item.query for item in recovery_source.requests], ['therapy AND depression AND adult NOT children'])
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(result.total, 0)
        self.assertIn('query_empty_unresolved', result.warnings)

    async def test_recovery_is_bounded(self):
        queries = [f'therapy AND depression AND adult{"s" * index} NOT children' for index in range(1, 6)]
        llm = FixtureLLM(*[item for query in queries for item in (json.dumps({'query': query}), APPROVED)])
        source = SequenceSource(*(source_result([], total=0) for _ in range(6)))
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(self.request())
        self.assertEqual(len(source.requests), 3)
        self.assertEqual(result.total, 0)
        self.assertIn('query_empty_unresolved', result.warnings)
        self.assertEqual([row.action for row in result.history], ['empty_recovery'] * 3)

    async def test_failed_recovery_request_keeps_the_empty_result(self):
        source = SequenceSource(source_result([], total=0), SourceError('openalex', 'limited', status=429))
        llm = FixtureLLM('{"query":"therapy AND depression AND adult NOT children"}', APPROVED)
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(self.request())
        self.assertEqual(result.total, 0)
        self.assertEqual(result.final_query, self.request().resume.current_query)
        self.assertIn('query_empty_recovery_failed', result.warnings)

    async def test_fenced_intent_check_is_accepted(self):
        source = SequenceSource(source_result([], total=0), source_result([PaperRecord(uid='p', title='Adult depression therapy')], total=1))
        llm = FixtureLLM('{"query":"therapy AND depression AND adult NOT children"}', 'Checked:\n```json\n' + APPROVED + '\n```')
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(self.request())
        self.assertEqual(result.total, 1)
        self.assertIn('query_empty_recovered', result.warnings)

    async def test_rate_limit_is_not_treated_as_zero_results(self):
        source = SequenceSource(SourceError('openalex', 'limited', status=429))
        llm = FixtureLLM()
        with self.assertRaises(QuerySourceError):
            await QueryLoopPipeline(llm, source, RecordingObserver()).run(self.request())
        self.assertFalse(llm.calls)

    async def test_repeated_recovery_stops_and_source_feedback_reaches_proposal(self):
        request = self.request('pubmed')
        source = SequenceSource(source_result([], total=0).model_copy(update={
            'query_feedback': ['PubMed quotedphrasesnotfound: optional phrase'],
        }))
        source.name = 'pubmed'
        llm = FixtureLLM(json.dumps({'query': request.resume.current_query}), json.dumps({'query': request.resume.current_query}))
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(request)
        self.assertEqual(len(source.requests), 1)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn('quotedphrasesnotfound', str(llm.calls[0]['messages']))
        self.assertIn('already executed and returned 0', str(llm.calls[1]['messages']))
        self.assertIn('query_empty_recovery_rejected', result.warnings)

    async def test_query_rate_limit_retains_previously_retrieved_candidates(self):
        record = PaperRecord(uid='p', title='Adult depression therapy')
        source = SequenceSource(source_result([record], total=10000), SourceError('openalex', 'limited', status=429))
        request = self.request()
        request = request.model_copy(update={'budget': QueryLoopBudget(query_portfolio_enabled=False)})
        result = await QueryLoopPipeline(FixtureLLM('{"query":"therapy AND depression AND adult NOT children"}'), source, RecordingObserver()).run(request)
        self.assertEqual(result.primary_hits, [record])
        self.assertIn('query_source_unavailable_candidates_retained', result.warnings)


class SourceResponseContract(FreshSourceLimits):
    async def test_pubmed_warnings_are_feedback_not_an_http_failure(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            'esearchresult': {'count': '0', 'idlist': [], 'warninglist': {'quotedphrasesnotfound': ['rare phrase']}}
        }))) as client:
            source = PubMedSource(transport=AsyncHttpTransport(client=client, sleep=AsyncMock()))
            result = await source.search(SourceSearchRequest(query='"rare phrase"'))
        self.assertIn('pubmed_query_warning', result.warnings)
        self.assertIn('rare phrase', str(result.query_feedback))

    async def test_pubmed_http_200_errors_are_never_empty_success(self):
        for index, (body, status) in enumerate((({'error': 'API rate limit exceeded'}, 429),
                     ({'error': 'Invalid api key'}, 401), ({}, None),
                     ({'esearchresult': {'errorlist': {'fieldsnotfound': ['badfield']}}}, 400))):
            with self.subTest(body=body):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
                    with self.assertRaises(SourceError) as caught:
                        await PubMedSource(api_key=f'fixture-{index}', transport=AsyncHttpTransport(client=client, sleep=AsyncMock())).search(SourceSearchRequest(query='q'))
                    self.assertEqual(caught.exception.status, status)

    async def test_pubmed_http_200_rate_limit_retries_before_reading_results(self):
        bodies = iter(({'error': 'API rate limit exceeded'}, {'esearchresult': {'count': '0', 'idlist': []}}))
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json=next(bodies))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = PubMedSource(transport=AsyncHttpTransport(client=client, sleep=AsyncMock()))
            result = await source.search(SourceSearchRequest(query='q'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.metadata.total, 0)
        self.assertEqual(source.last_response_info['attempts'], 2)


class SourceRateLimitContract(FreshSourceLimits):
    async def test_pacing_respects_cancellation_and_deadline_without_sending(self):
        calls = []
        entered = asyncio.Event()
        async def wait(_delay):
            entered.set()
            await asyncio.Event().wait()
        def handle(request):
            calls.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            cancellation = CancellationScope()
            transport = AsyncHttpTransport(client=client, sleep=wait,
                context=RequestContext(step_key='test', cancellation=cancellation))
            url = 'https://fixture.invalid/api/query'
            await transport.request('arxiv', 'GET', url)
            pending = asyncio.create_task(transport.request('arxiv', 'GET', url))
            await asyncio.wait_for(entered.wait(), 1)
            cancellation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
            with self.assertRaises(SourceError) as caught:
                await transport.request('arxiv', 'GET', url,
                    context=RequestContext(step_key='deadline', deadline_at=time.monotonic() + .02))
            self.assertEqual(caught.exception.failure_kind.value, 'step_deadline')
            self.assertEqual(len(calls), 1)
            # Both cancelled waiters released the source lock.
            await AsyncHttpTransport(client=client, sleep=AsyncMock()).request('arxiv', 'GET', url)
            self.assertEqual(len(calls), 2)

    async def test_429_cooldown_is_shared_between_clients_but_not_other_keys_or_bulk_siblings(self):
        requests = []
        async def handle(request):
            requests.append(request)
            return httpx.Response(429, headers={'Retry-After': '120'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            first = AsyncHttpTransport(client=client, sleep=AsyncMock())
            second = AsyncHttpTransport(client=client, sleep=AsyncMock())
            bulk = 'https://fixture.invalid/graph/v1/paper/search/bulk'
            await first.request('semanticscholar', 'GET', bulk, headers={'x-api-key': 'key-a'})
            with self.assertRaises(SourceError) as caught:
                await second.request('semanticscholar', 'GET', bulk, headers={'x-api-key': 'key-a'})
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(len(requests), 1)
            await second.request('semanticscholar', 'GET', bulk, headers={'x-api-key': 'key-b'})
            await second.request('semanticscholar', 'GET', bulk.removesuffix('/bulk'), headers={'x-api-key': 'key-a'})
            self.assertEqual(len(requests), 3)

    async def test_arxiv_retries_are_paced_and_connections_do_not_overlap(self):
        active = 0
        peak = 0
        count = 0
        waits = []
        async def wait(delay):
            waits.append(delay)
        async def handle(request):
            nonlocal active, peak, count
            active += 1
            peak = max(peak, active)
            count += 1
            current = count
            await asyncio.sleep(0)
            active -= 1
            return httpx.Response(503 if current == 1 else 200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = AsyncHttpTransport(client=client, sleep=wait)
            with patch('paperseek_core.http_retry.retry_delay', return_value=0):
                await asyncio.gather(*(transport.request('arxiv', 'GET', 'https://fixture.invalid/api/query') for _ in range(2)))
        self.assertEqual(peak, 1)
        self.assertEqual(count, 3)
        self.assertTrue(any(delay >= 3 for delay in waits))


    async def test_compressed_embedded_rate_limit_is_a_429_not_a_transport_error(self):
        calls = []
        def handle(request):
            calls.append(request)
            body = json.dumps({'error': 'API rate limit exceeded'}).encode()
            return httpx.Response(200, headers={'Content-Encoding': 'gzip', 'Content-Type': 'application/json'},
                                  content=gzip.compress(body))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = PubMedSource(transport=AsyncHttpTransport(client=client, sleep=AsyncMock()))
            with self.assertRaises(SourceError) as caught:
                await source.search(SourceSearchRequest(query='crispr', limit=5))
        self.assertEqual(caught.exception.status, 429)
        self.assertIsNone(caught.exception.failure_kind)
        # The first attempt starts the shared cooldown, which the retries wait out.
        self.assertEqual(len(calls), 3)

    async def test_arxiv_pacing_is_shared_by_runs_on_other_threads(self):
        sent = []
        def run_in_own_loop():
            async def search():
                def handle(request):
                    sent.append(time.monotonic())
                    return httpx.Response(200)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    await AsyncHttpTransport(client=client).request('arxiv', 'GET', 'https://fixture.invalid/api/query',
                                                                    min_interval=0.3)
            asyncio.run(search())
        await asyncio.gather(*(asyncio.to_thread(run_in_own_loop) for _ in range(3)))
        sent.sort()
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(later - earlier >= 0.28 for earlier, later in zip(sent, sent[1:])))

    def test_429_backoff_is_set_per_source(self):
        self.assertEqual(rate_limit_backoff('arxiv'), 6.2)
        self.assertEqual(rate_limit_backoff('openalex'), 2.0)


if __name__ == '__main__':
    unittest.main()
