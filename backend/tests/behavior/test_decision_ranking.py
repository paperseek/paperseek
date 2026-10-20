from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fixtures.retrieval import RecordingObserver, record
from fixtures.submission import settings
from pydantic import ValidationError

from paperseek_app.execution import CancellationScope
from paperseek_app.pre_ranking import resolve_retrieval_route
from paperseek_app.services import RunExecutionService, RunSubmissionService
from paperseek_app.submission import RunSubmission, prepare_run_submission
from paperseek_app.worker import RunExecutor
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.diagnostics import run_configuration_diagnostics
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_core.candidate_selection import CandidateSelectionPipeline
from paperseek_core.decision_client import SystemOneAdapter
from paperseek_core.decision_selection import decision_signature
from paperseek_core.document_identity import document_key
from paperseek_core.execution import NEVER_CANCELLED, RequestContext
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.models import (
    DecisionBatchCheckpoint,
    DecisionBatchResult,
    DecisionScore,
)
from paperseek_core.sources.http import AsyncHttpTransport


class DecisionProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_gpt_56_luna_uses_supported_reasoning_parameters_on_both_apis(self):
        for api_type, model in (("openai_responses", "gpt-5.6-luna"), ("openai_chat", "openai/gpt-5.6-luna")):
            requests = []
            def handle(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"output_text": "{}", "choices": [{"message": {"content": "{}"}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                llm = HttpLLMAdapter(client=client, cancellation=NEVER_CANCELLED, api_type=api_type,
                                     model=model, base_url="https://api.openai.com/v1", api_key="test-key")
                self.assertEqual(await llm.chat_json([{"role": "user", "content": "Return JSON"}]), "{}")
            self.assertNotIn("temperature", requests[0])
            self.assertEqual(requests[0].get("reasoning", {}).get("effort") or requests[0].get("reasoning_effort"), "low")

    async def test_both_endpoints_auth_retry_and_score_contract(self):
        for provider, host in (("typesafe", "api.typesafe.ai"), ("siliconflow", "api.siliconflow.cn")):
            calls = []
            async def handle(request):
                calls.append(request)
                self.assertEqual(request.url.path, "/v1/systemone")
                self.assertEqual(request.headers["Authorization"], "Bearer test-decision-secret")
                body = json.loads(request.content)
                self.assertEqual(body["questions"]["paper:1"]["type"], "score")
                if len(calls) == 1:
                    return httpx.Response(529 if provider == "typesafe" else 429)
                return httpx.Response(200, json={"model": "resolved-model", "answers": {
                    "paper:1": {"type": "score", "score": 2.4, "confidence": 0.8, "legend": {"0": "irrelevant"}},
                }, "usage": {"input_tokens": 100, "output_tokens": 1}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                adapter = SystemOneAdapter(AsyncHttpTransport(client=client, sleep=AsyncMock()), provider=provider,
                    model="configured-model", base_url=f"https://{host}/v1", api_key="test-decision-secret")
                result = await adapter.score({"research_question": "question"}, {"paper:1": {"type": "score"}})
            self.assertEqual(len(calls), 2)
            self.assertEqual(result.model, "resolved-model")
            self.assertEqual(result.scores["paper:1"].score, 2.4)
            self.assertEqual(result.input_tokens, 100)

    async def test_invalid_or_incomplete_answers_are_not_accepted(self):
        for answer in ({}, {"other": {"type": "score", "score": 2}}, {"p": {"type": "noul", "score": 2}},
                       {"p": {"type": "score", "score": True}}, {"p": {"type": "score", "score": 4}},
                       {"p": {"type": "score", "score": "2"}}, {"p": {"type": "score", "score": 2, "confidence": 2}}):
            with self.subTest(answer=answer):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"answers": answer}))) as client:
                    adapter = SystemOneAdapter(AsyncHttpTransport(client=client), provider="typesafe", model="jev-latest",
                                               base_url="https://api.typesafe.ai/v1", api_key="test-key")
                    with self.assertRaises(ValueError):
                        await adapter.score({}, {"p": {}})

    async def test_cancellation_drains_inflight_http_requests(self):
        cancellation = CancellationScope()
        entered, drained = asyncio.Event(), asyncio.Event()
        async def handle(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = AsyncHttpTransport(client=client, context=RequestContext(step_key="decision", cancellation=cancellation))
            adapter = SystemOneAdapter(transport, provider="typesafe", model="jev-latest", base_url="https://api.typesafe.ai/v1", api_key="test-key")
            task = asyncio.create_task(adapter.score({}, {"p": {}}))
            await asyncio.wait_for(entered.wait(), 1)
            cancellation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertTrue(drained.is_set())


class DecisionSelectionTest(unittest.IsolatedAsyncioTestCase):
    def adapter(self, score=None):
        async def call(state, questions):
            await asyncio.sleep(0)
            return DecisionBatchResult(model="jev-pinned", scores={key: DecisionScore(score=float(score(key) if score else 1)) for key in questions})
        return SimpleNamespace(provider="typesafe", model="jev-pinned", base_url="https://api.typesafe.ai/v1", score=AsyncMock(side_effect=call))

    async def select(self, adapter, observer=None, documents=None, *, ranking_limit=2, pool_max=30, **kwargs):
        documents = documents if documents is not None else [record(str(i)) for i in range(25)]
        return await CandidateSelectionPipeline(observer or RecordingObserver(), decision=adapter).run(
            question="research", search_plan="intent: Find useful adjacent evidence", candidates=documents,
            provenance={document_key(doc): {} for doc in documents}, embedding_state=None,
            pool_max=pool_max, ranking_limit=ranking_limit, **kwargs,
        )

    async def test_only_top_two_k_are_scored_and_tail_remains_in_local_order(self):
        adapter = self.adapter(lambda key: 3 if key == document_key(record("3")) else 0)
        observer = RecordingObserver()
        result = await self.select(adapter, observer)
        batches = [call.args[1] for call in adapter.score.call_args_list]
        self.assertEqual({key for batch in batches for key in batch}, {document_key(record(str(i))) for i in range(4)})
        self.assertTrue(all(len(batch) <= 8 for batch in batches))
        self.assertIn("3", [doc.uid for doc in result.documents[:2]])
        self.assertEqual([doc.uid for doc in result.documents[4:]], list(map(str, range(4, 25))))
        self.assertEqual(result.selected_count, 2)
        self.assertNotIn("decision_score", result.provenance_by_identity[document_key(record("4"))])
        self.assertEqual(result.actual_mode, "decision")
        self.assertEqual(len([row for row in observer.checkpoints if row.step_key == "selection.decision"]), 1)
        progress = next(row for row in observer.events if row.event_type == "selection.decision_completed")
        self.assertEqual((progress.payload["candidate_count"], progress.payload["scored_count"]), (4, 4))
        # Every missing abstract is explicitly represented as uncertainty, not scored locally as zero.
        self.assertTrue(all(q["instructions"]["abstract_missing"] for batch in batches for q in batch.values()))

    async def test_equal_scores_preserve_local_order(self):
        result = await self.select(self.adapter())
        self.assertEqual([doc.uid for doc in result.documents], list(map(str, range(25))))

    async def test_candidate_count_respects_two_k_pool_budget_and_available_documents(self):
        for count, pool_max, ranking_limit in ((0, 30, 2), (3, 30, 2), (4, 30, 2), (25, 30, 2), (25, 3, 2)):
            with self.subTest(count=count, pool_max=pool_max, ranking_limit=ranking_limit):
                adapter = self.adapter()
                result = await self.select(adapter, documents=[record(str(i)) for i in range(count)],
                                           pool_max=pool_max, ranking_limit=ranking_limit)
                self.assertEqual(sum(len(call.args[1]) for call in adapter.score.call_args_list),
                                 min(count, pool_max, 2 * ranking_limit))
                self.assertEqual(result.selected_count, min(count, pool_max, ranking_limit))

    async def test_citation_candidates_join_local_fusion_before_top_two_k_selection(self):
        documents = [record(str(i)) for i in range(25)]
        provenance = {document_key(doc): {"origins": ["citation_expansion" if doc.uid == "24" else "retrieval"]} for doc in documents}
        adapter = self.adapter()
        baseline = await CandidateSelectionPipeline(RecordingObserver()).run(
            question="research", candidates=documents, provenance=provenance, embedding_state=None,
            pool_max=10, ranking_limit=2,
        )
        result = await CandidateSelectionPipeline(RecordingObserver(), decision=adapter).run(
            question="research", candidates=documents, provenance=provenance, embedding_state=None,
            pool_max=10, ranking_limit=2,
        )
        scored_keys = {key for call in adapter.score.call_args_list for key in call.args[1]}
        self.assertEqual(scored_keys, {document_key(doc) for doc in baseline.documents[:4]})
        self.assertIn(document_key(documents[-1]), scored_keys)
        self.assertEqual(len(result.documents), 10)

    async def test_completed_selection_reuses_scores_but_rejects_the_old_full_pool_policy(self):
        adapter = self.adapter()
        result = await self.select(adapter)
        adapter.score.reset_mock()
        self.assertEqual(await self.select(adapter, resume=result), result)
        adapter.score.assert_not_called()
        legacy_signature = decision_signature(adapter)
        legacy_signature.pop("candidate_policy", None)
        with patch("paperseek_core.candidate_selection.decision_signature", return_value=legacy_signature):
            legacy_result = await self.select(adapter)
        with self.assertRaisesRegex(ValueError, "checkpoint does not match"):
            await self.select(adapter, resume=legacy_result)

    async def test_worker_persists_decision_batches_and_recovers_typed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paperseek.db"
            migrate_database(database)
            uow = SQLiteRunUnitOfWork(database)
            self.addCleanup(uow.close)
            source = FixedSourceAdapter.from_file(Path(__file__).parent / "fixtures/m1_fixed_source.json")
            prepared = prepare_run_submission(RunSubmission(question=source.fixture.question, citation_enabled=False,
                                                             retrieval={"mode": "decision"}), settings())
            run = RunSubmissionService(uow).create_run(prepared.question, "openalex", prepared.config_snapshot)
            credentials = EncryptedRunCredentialStore(database)
            credentials.save(run.id, {"decision": {"api_key": "transient-decision-secret"}})
            self.assertNotIn(b"transient-decision-secret", database.read_bytes())
            adapter = self.adapter()
            resolver = CommunityExecutionResolver(settings(), credentials)
            def resolve(detail, cancellation):
                execution = resolver(detail, cancellation)
                self.assertEqual(execution.decision.api_key, "transient-decision-secret")
                self.assertIsNone(execution.embedding)
                self.assertIsNone(execution.reranker)
                return replace(execution, llm=FixedLLMAdapter(source.fixture.llm_responses), source=source, decision=adapter,
                               request=execution.request.model_copy(update={"query_budget": execution.request.query_budget.model_copy(update={"query_portfolio_enabled": False})}))
            service = RunExecutionService(uow, FileArtifactStore(directory))
            completed = await RunExecutor(service, resolve, owner_id="decision-test").run_once()
            self.assertIn(completed.status.value, {"completed", "completed_with_warnings"})
            self.assertGreater(adapter.score.call_count, 0)
            resume = service._resume_state(completed)
            self.assertTrue(resume.decision_batches)
            self.assertEqual(resume.selection.actual_mode, "decision")
            self.assertTrue(all("decision_model" in value for value in resume.selection.provenance_by_identity.values()))
            self.assertNotIn("transient-decision-secret", str(resume))
            credentials.delete(run.id)
            self.assertEqual(credentials.resolve(run.id), {})
            uow.close()

    async def test_resume_reuses_only_completed_batches_and_checks_model_and_prompt_inputs(self):
        observer = RecordingObserver()
        await self.select(self.adapter(), observer, ranking_limit=10)
        saved = [DecisionBatchCheckpoint.model_validate(row.state) for row in observer.checkpoints if row.step_key == "selection.decision"][:1]
        adapter = self.adapter()
        await self.select(adapter, decision_batches=saved, ranking_limit=10)
        self.assertEqual(adapter.score.call_count, 2)
        self.assertEqual(sum(len(call.args[1]) for call in adapter.score.call_args_list), 12)
        adapter.model = "different-model"
        with self.assertRaisesRegex(ValueError, "checkpoints do not match"):
            await self.select(adapter, decision_batches=saved, ranking_limit=10)

    async def test_partial_failure_preserves_entire_local_order_and_stops_new_batches(self):
        adapter = self.adapter()
        adapter.score.side_effect = RuntimeError("test")
        observer = RecordingObserver()
        result = await self.select(adapter, observer, ranking_limit=10)
        self.assertLessEqual(adapter.score.call_count, 2)
        self.assertEqual([doc.uid for doc in result.documents], list(map(str, range(25))))
        self.assertEqual(result.actual_mode, "local_fusion")
        self.assertIn("retrieval_decision_failed_local_order_retained", result.warnings)
        self.assertTrue(any(row.event_type == "selection.decision_degraded" for row in observer.events))

    async def test_checkpoint_failure_propagates_instead_of_falling_back(self):
        observer = RecordingObserver()
        observer.on_checkpoint = AsyncMock(side_effect=OSError("storage unavailable"))
        with self.assertRaises(ExceptionGroup):
            await self.select(self.adapter(), observer)

    async def test_concurrency_is_bounded_and_cancellation_does_not_checkpoint_late(self):
        adapter = self.adapter()
        active, peak = 0, 0
        entered = asyncio.Event()
        async def call(*_):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            if active == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        adapter.score.side_effect = call
        observer = RecordingObserver()
        task = asyncio.create_task(self.select(adapter, observer, ranking_limit=10))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((peak, active), (2, 0))
        self.assertFalse(observer.checkpoints)

    async def test_mixed_resolved_models_cannot_be_fused(self):
        adapter = self.adapter()
        original = adapter.score.side_effect
        async def call(state, questions):
            result = await original(state, questions)
            return result.model_copy(update={"model": next(iter(questions))})
        adapter.score.side_effect = call
        result = await self.select(adapter, ranking_limit=10)
        self.assertEqual(result.actual_mode, "local_fusion")
        self.assertIn("retrieval_decision_model_changed_local_order_retained", result.warnings)


class PreRankingConfigurationTest(unittest.TestCase):
    def test_mode_routes_are_exclusive_and_old_external_routes_keep_their_mode(self):
        routes = {"embedding": {"provider": "siliconflow"}, "reranker": {"provider": "siliconflow"}, "decision": {"provider": "typesafe"}}
        self.assertEqual(resolve_retrieval_route({})["mode"], "local_fusion")
        self.assertEqual(resolve_retrieval_route({"embedding": {"provider": " LOCAL "}})["mode"], "local_fusion")
        self.assertEqual(resolve_retrieval_route(routes)["mode"], "embedding_rerank")
        for mode in ("local_fusion", "decision"):
            resolved = resolve_retrieval_route({**routes, "mode": mode})
            self.assertEqual(resolved["embedding"]["provider"], "local")
            self.assertEqual(resolved["reranker"]["provider"], "")
            self.assertEqual("decision" in resolved, mode == "decision")
        with self.assertRaises(ValidationError):
            RunSubmission(question="test", retrieval={"mode": "unknown"})

    def test_decision_credential_is_separate_from_llm_and_absent_from_snapshot(self):
        submission = RunSubmission(question="test", retrieval={"mode": "decision"}, credentials={"llm": {"api_key": "llm-secret"}})
        diagnostic = run_configuration_diagnostics(submission.model_dump(), settings())
        decision = next(row for row in diagnostic["checks"] if row["id"] == "retrieval.decision")
        self.assertEqual(decision["status"], "fail")
        submission.credentials.decision.api_key = "decision-secret"
        diagnostic = run_configuration_diagnostics(submission.model_dump(), settings())
        self.assertEqual(next(row for row in diagnostic["checks"] if row["id"] == "retrieval.decision")["status"], "pass")
        prepared = prepare_run_submission(submission, settings())
        self.assertNotIn("secret", str(prepared))
        self.assertEqual(prepared.config_snapshot["retrieval"]["decision"], {
            "provider": "typesafe", "model": "jev-latest", "base_url": "https://api.typesafe.ai/v1",
        })

    def test_decision_defaults_preserve_explicit_provider_and_model(self):
        self.assertEqual(resolve_retrieval_route({"mode": "decision"})["decision"], {
            "provider": "typesafe", "model": "jev-latest", "base_url": "https://api.typesafe.ai/v1",
        })
        for provider, model, base_url in (
            ("typesafe", "jev-1.13.0", "https://api.typesafe.ai/v1"),
            ("siliconflow", "SemIf", "https://api.siliconflow.cn/v1"),
        ):
            route = {"provider": provider, "model": model, "base_url": base_url}
            submission = RunSubmission(question="test", retrieval={"mode": "decision", "decision": route})
            prepared = prepare_run_submission(submission, settings())
            self.assertEqual(prepared.config_snapshot["retrieval"]["decision"], route)
