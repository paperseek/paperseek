from __future__ import annotations
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fixtures.retrieval import RecordingObserver, record
from paperseek_app.execution import CancellationScope
from paperseek_core.candidate_selection import CandidateSelectionPipeline
from paperseek_core.document_identity import document_key
from paperseek_core.models import (
    RetrievalEmbeddingState,
    RetrievalRerankItem,
    RetrievalRerankResult,
)

import asyncio

from fixtures.retrieval import FixtureSource, source_result
from fixtures.retrieval_client import FakeClient

from paperseek_core.candidate_selection import (
    embedding_provenance,
)
from paperseek_core.models import (
    CandidateSelectionResult,
    ResearchWorkflowResumeState,
    RetrievalEmbeddingResult,
    RetrievalRequest,
    SourceSearchMetadata,
)
from paperseek_core.ranking import RankingPipeline
from paperseek_core.retrieval import RetrievalPipeline
from paperseek_core.retrieval_clients import (
    ExternalEmbeddingAdapter,
    ExternalRerankerAdapter,
)
from paperseek_core.retrieval_fusion import fuse_candidates_rrf
from paperseek_core.sources.contracts import SourceCapabilities


class PaginationSelectionContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_first_page_size_is_preserved(self):
        first = [record(str(i)) for i in range(47)]
        source = FixtureSource({
            ('relevance', 2): source_result([record('new')], page=2, limit=50, total=51),
        }, capabilities=SourceCapabilities(source='fixture', max_page_size=100))
        result = await RetrievalPipeline(source, RecordingObserver()).run(RetrievalRequest(
            question='research', query='research', primary_hits=first + first[:3],
            primary_page=SourceSearchMetadata(page=1, limit=50, raw_count=50, total=51),
        ))
        self.assertEqual([(call.page, call.limit) for call in source.calls], [(2, 50)])
        self.assertEqual(len(result.documents), 48)

    async def test_repeated_pages_stop_at_scan_budget(self):
        first = [record(str(i)) for i in range(100)]
        source = FixtureSource({
            ('relevance', i): source_result(first, page=i, total=100000) for i in range(2, 8)
        }, capabilities=SourceCapabilities(source='fixture', max_page_size=100))
        observer = RecordingObserver()
        result = await RetrievalPipeline(source, observer).run(RetrievalRequest(
            question='research', query='research', primary_hits=first, lane_limit=500,
        ))
        self.assertEqual([call.page for call in source.calls], [2, 3, 4, 5])
        self.assertEqual(len(result.documents), 100)
        self.assertIn('retrieval_scan_budget_exhausted:relevance', result.warnings)
        stopped = next(e for e in observer.events if e.event_type == 'retrieval.lane_completed')
        self.assertEqual(stopped.payload['stop_reason'], 'scan_budget_exhausted')

    async def test_full_first_page_with_duplicates_still_fetches_next_page(self):
        unique = [record(str(i)) for i in range(97)]
        first = unique + unique[:3]
        second = [record(str(i)) for i in range(97, 197)]
        source = FixtureSource(
            {('relevance', 2): source_result(second, page=2, total=200)},
            capabilities=SourceCapabilities(source='fixture', max_page_size=100),
        )
        result = await RetrievalPipeline(source, RecordingObserver()).run(
            RetrievalRequest(question='research', query='research', primary_hits=first)
        )
        self.assertEqual([(call.page, call.limit) for call in source.calls], [(2, 100)])
        self.assertEqual(len(result.documents), 197)

    async def test_duplicate_page_does_not_hide_later_new_candidates(self):
        first = [record(str(i)) for i in range(100)]
        third = [record(str(i)) for i in range(100, 200)]
        source = FixtureSource({
            ('relevance', 2): source_result(first, page=2, total=300),
            ('relevance', 3): source_result(third, page=3, total=300),
        }, capabilities=SourceCapabilities(source='fixture', max_page_size=100))
        result = await RetrievalPipeline(source, RecordingObserver()).run(
            RetrievalRequest(question='research', query='research', primary_hits=first)
        )
        self.assertEqual([call.page for call in source.calls], [2, 3])
        self.assertEqual(len(result.documents), 200)

    async def test_reranker_scores_every_requested_document_across_batches(self):
        client = FakeClient([
            (200, {'results': [{'index': i, 'relevance_score': i / 1000} for i in range(200)]}),
            (200, {'results': [{'index': i, 'relevance_score': 0.9 + i / 1000} for i in range(5)]}),
        ])
        adapter = ExternalRerankerAdapter(
            client, CancellationScope(), provider='siliconflow', model='fixture',
            base_url='https://fixture.invalid/v1', api_key='fixture',
        )
        result = await adapter.rerank('research', [record(str(i)) for i in range(205)])
        self.assertEqual(len(result.items), 205)
        self.assertEqual(result.items[-1].document.uid, '204')
        self.assertEqual([len(call['json']['documents']) for call in client.calls], [200, 5])

    async def test_incomplete_or_invalid_rerank_response_is_rejected(self):
        for values in (
            [{'index': 0, 'score': 0.5}],
            [{'index': 0, 'score': 0.5}, {'index': 0, 'score': 0.7}],
            [{'index': 0, 'score': 0.5}, {'index': 1}],
            [{'index': 0, 'score': 0.5}, {'index': 2, 'score': 0.7}],
        ):
            with self.subTest(values=values):
                adapter = ExternalRerankerAdapter(
                    FakeClient([(200, {'results': values})]), CancellationScope(),
                    provider='siliconflow', model='fixture', base_url='https://fixture.invalid', api_key='fixture',
                )
                with self.assertRaises(RuntimeError):
                    await adapter.rerank('question', [record('a'), record('b')])

    async def test_fallback_model_rescores_every_rerank_batch(self):
        def response(count, score):
            return 200, {'results': [{'index': i, 'score': score} for i in range(count)]}
        client = FakeClient([response(200, 0.1), (400, {}), response(200, 0.8), response(1, 0.8)])
        adapter = ExternalRerankerAdapter(
            client, CancellationScope(), provider='fixture', model='first,second',
            base_url='https://fixture.invalid', api_key='fixture',
        )
        result = await adapter.rerank('question', [record(str(i)) for i in range(201)])
        self.assertEqual(result.model, 'second')
        self.assertEqual({item.score for item in result.items}, {0.8})
        self.assertEqual([call['json']['model'] for call in client.calls], ['first', 'first', 'second', 'second'])

    async def test_incremental_embedding_does_not_silently_switch_models(self):
        client = FakeClient([(400, {})])
        adapter = ExternalEmbeddingAdapter(
            client, CancellationScope(), provider='fixture', model='first,second',
            base_url='https://fixture.invalid', api_key='fixture',
        )
        with self.assertRaises(RuntimeError):
            await adapter.embedding_scores('question', [record('a')], model='first')
        self.assertEqual([call['json']['model'] for call in client.calls], ['first'])


class EmbeddingFixture:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def embedding_scores(self, question, documents, *, model=None):
        self.calls.append(([doc.uid for doc in documents], model))
        if self.fail:
            raise RuntimeError('fixture failure')
        return RetrievalEmbeddingResult(scores=[0.7] * len(documents), model='fixture-model', provider='fixture')


class RerankerFixture:
    def __init__(self):
        self.calls = []
        self.partial = False

    async def rerank(self, question, documents):
        self.calls.append([doc.uid for doc in documents])
        return RetrievalRerankResult(items=[
            RetrievalRerankItem(document=doc, score=0.99 if doc.uid in ('299', 'new') else 0.01)
            for doc in (documents[:-1] if self.partial else documents)
        ], model='fixture-reranker', provider='fixture')


class CandidateSelectionContractTest(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, count=300):
        self.observer = RecordingObserver()
        self.embedding = EmbeddingFixture()
        self.reranker = RerankerFixture()
        self.pipeline = CandidateSelectionPipeline(
            self.observer, embedding=self.embedding, reranker=self.reranker,
        )
        base = [record(str(i), f'Privacy research {i}') for i in range(count)]
        self.state = await self.pipeline.embed('privacy', base, [])
        fused = fuse_candidates_rrf(
            'privacy', {'relevance': base}, embedding_scores_by_key=self.state.scores_by_identity,
        )
        self.provenance = {
            document_key(doc): {**fused.metadata_by_key[document_key(doc)],
                                **embedding_provenance(doc, self.state), 'origins': ['retrieval']}
            for doc in fused.documents
        }
        self.documents = [*fused.documents, record('new', 'Privacy research collaboration')]
        self.provenance[document_key(self.documents[-1])] = {'origins': ['citation_expansion']}

    async def select(self, **kwargs):
        return await self.pipeline.run(
            question='privacy', candidates=self.documents, provenance=self.provenance,
            embedding_state=self.state, pool_max=1000, ranking_limit=256, **kwargs,
        )

    async def test_incremental_scores_and_rerank_survive_citations_cutoff_and_resume(self):
        await self.prepare()
        result = await self.select()
        self.assertEqual(self.embedding.calls[1], (['new'], 'fixture-model'))
        self.assertEqual(len(self.reranker.calls), 1)
        self.assertEqual(set(self.reranker.calls[0]), {doc.uid for doc in self.documents})
        self.assertEqual({doc.uid for doc in result.documents[:2]}, {'299', 'new'})
        self.assertEqual(result.selected_count, 256)
        for doc in result.documents:
            self.assertEqual(result.provenance_by_identity[document_key(doc)]['retrieval_embedding_score'], 0.7)
        checkpoint = next(cp for cp in self.observer.checkpoints if cp.step_key == 'select_candidates')
        state = dict(checkpoint.state)
        state.pop('resume_from_stage')
        resumed = await self.select(resume=CandidateSelectionResult.model_validate(state))
        self.assertEqual(resumed, result)
        self.assertEqual(len(self.embedding.calls), 2)
        self.assertEqual(len(self.reranker.calls), 1)

    async def test_partial_rerank_falls_back_without_promoting_partial_scores(self):
        await self.prepare(6)
        self.reranker.partial = True
        result = await self.select()
        self.assertIn('retrieval_rerank_failed_rrf_order_retained', result.warnings)
        self.assertFalse(any('rerank_score' in p for p in result.provenance_by_identity.values()))

    async def test_incremental_embedding_failure_keeps_external_audit_and_uses_local_fusion(self):
        await self.prepare(6)
        self.embedding.fail = True
        result = await self.select()
        first = self.documents[0]
        detail = result.provenance_by_identity[document_key(first)]
        self.assertEqual(detail['external_embedding_score'], 0.7)
        self.assertNotEqual(detail['retrieval_embedding_score'], 0.7)
        self.assertEqual(detail['retrieval_similarity_method'], 'local_hash')
        self.assertIn('retrieval_embedding_failed_local_similarity_retained', result.warnings)

    async def test_selection_resume_rejects_changed_candidate_text(self):
        await self.prepare(6)
        result = await self.select()
        self.documents[0] = self.documents[0].model_copy(update={'title': 'changed'})
        with self.assertRaisesRegex(ValueError, 'checkpoint does not match'):
            await self.select(resume=result)

    async def test_cancelled_reranking_does_not_write_selection_checkpoint(self):
        await self.prepare(6)
        self.reranker.rerank = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.select()
        self.assertFalse(self.observer.checkpoints)

    def test_ranking_recovery_requires_persisted_candidate_selection(self):
        with self.assertRaisesRegex(ValueError, 'requires a completed candidate selection'):
            ResearchWorkflowResumeState(ranking_completed_batch_ids=['old-batch'])

    def test_llm_score_ties_preserve_candidate_selection_order(self):
        docs = [record('a'), record('b')]
        pipeline = RankingPipeline(AsyncMock(), RecordingObserver())
        ranked = pipeline._rank_from_scores(docs, [
            {'uid': 'b', 'score': 8}, {'uid': 'a', 'score': 8},
        ], {})
        self.assertEqual([item.document.uid for item in ranked], ['a', 'b'])



class SelectionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_records_successful_and_failed_components(self):
        documents = [record("1"), record("2")]
        for embedding_ok, rerank_ok, expected in (
            (False, False, "local_fusion"),
            (True, False, "embedding"),
            (False, True, "rerank"),
            (True, True, "embedding_rerank"),
        ):
            with self.subTest(embedding=embedding_ok, rerank=rerank_ok):
                observer = RecordingObserver()
                reranker = SimpleNamespace(
                    rerank=AsyncMock(
                        return_value=RetrievalRerankResult(
                            provider="test",
                            model="test",
                            items=[
                                RetrievalRerankItem(document=doc, score=i)
                                for i, doc in enumerate(documents)
                            ],
                        )
                    )
                )
                if not rerank_ok:
                    reranker.rerank.side_effect = RuntimeError("model unavailable")
                embedding_state = (
                    RetrievalEmbeddingState(
                        question="test",
                        provider="test",
                        model="test",
                        scores_by_identity={
                            document_key(doc): 0.8 for doc in documents
                        },
                        text_hashes={},
                    )
                    if embedding_ok
                    else None
                )
                pipeline = CandidateSelectionPipeline(
                    observer, embedding=SimpleNamespace(), reranker=reranker
                )
                result = await pipeline.run(
                    question="test",
                    candidates=documents,
                    provenance={},
                    embedding_state=embedding_state,
                    pool_max=10,
                    ranking_limit=5,
                )
                self.assertEqual(result.actual_mode, expected)
                self.assertEqual(
                    result.component_statuses,
                    {
                        "embedding": "succeeded" if embedding_ok else "failed",
                        "reranker": "succeeded" if rerank_ok else "failed",
                        "decision": "disabled",
                    },
                )
                self.assertEqual(observer.events[-1].payload["actual_mode"], expected)
                resumed = await pipeline.run(
                    question="test",
                    candidates=documents,
                    provenance={},
                    embedding_state=embedding_state,
                    pool_max=10,
                    ranking_limit=5,
                    resume=result,
                )
                self.assertEqual(resumed.component_statuses, result.component_statuses)
                self.assertEqual(reranker.rerank.await_count, 1)
