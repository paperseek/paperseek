from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from paperseek_app.execution import CancellationScope, warm_up_model
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.diagnostics import run_configuration_diagnostics
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.candidate_selection import CandidateSelectionPipeline
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.models import (
    RetrievalEmbeddingState,
    RetrievalRerankItem,
    RetrievalRerankResult,
)
from paperseek_core.document_identity import document_key
from paperseek_core.sources.contracts import SourceSearchRequest
from paperseek_core.sources.google_scholar import GoogleScholarSource
from paperseek_core.sources.http import AsyncHttpTransport
from m2_multi_lane_retrieval_contract import RecordingObserver, record
from m4_key_pool_retry_contract import ControlledAsyncHttpServer


class ArchitectureCleanupContract(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return CommunityDeploymentSettings(
            llm_api_type="openai_chat",
            llm_base_url="https://api.deepseek.com",
            llm_model="test",
            llm_api_key="deployment-key",
            openalex_api_key="",
            semantic_scholar_api_key="",
            pubmed_api_key="",
            wos_api_key="",
            google_scholar_api_key="",
            contact_email="",
        )

    async def test_credentials_are_reused_only_for_same_provider_and_endpoint(self):
        for provider, base, explicit, expected in (
            ("siliconflow", "https://api.siliconflow.cn/v1", "", ""),
            ("custom", "https://api.deepseek.com", "", ""),
            ("custom", "https://custom.example/v1", "explicit-key", "explicit-key"),
            ("openai", "https://custom.example/v1", "", ""),
            ("openai", "https://api.openai.com/v1/", "", "llm-key"),
        ):
            with self.subTest(provider=provider, base=base):
                config = {
                    "source": "arxiv",
                    "llm": {"provider": "openai"},
                    "retrieval": {
                        "mode": "embedding_rerank",
                        "embedding": {
                            "provider": provider,
                            "model": "test",
                            "base_url": base,
                        },
                        "reranker": {
                            "provider": "custom",
                            "model": "test",
                            "base_url": "https://other.example/v1",
                        },
                    },
                }
                credentials = {
                    "llm": {"api_key": "llm-key"},
                    "embedding": {"api_key": explicit},
                }
                resolver = CommunityExecutionResolver(
                    self.settings(), SimpleNamespace(resolve=lambda _: credentials)
                )
                execution = resolver(
                    SimpleNamespace(
                        id="test",
                        source="arxiv",
                        question="test",
                        config_snapshot=config,
                        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                    ),
                    CancellationScope(),
                )
                try:
                    self.assertEqual(
                        execution.embedding.api_keys.keys,
                        (expected,) if expected else (),
                    )
                    self.assertFalse(execution.reranker.api_keys.available)
                    checks = run_configuration_diagnostics(
                        {**config, "credentials": credentials}, self.settings()
                    )["checks"]
                    states = {row["id"]: row["status"] for row in checks}
                    self.assertEqual(
                        states["retrieval.embedding"], "pass" if expected else "fail"
                    )
                    self.assertEqual(states["retrieval.reranker"], "fail")
                finally:
                    await execution.close()

    async def test_deployment_key_is_not_sent_to_overridden_llm_endpoint(self):
        for route, expected in (
            ({"provider": "deepseek"}, True),
            ({"provider": "deepseek", "base_url": "https://other.example"}, False),
            ({"provider": "openai"}, False),
        ):
            resolver = CommunityExecutionResolver(
                self.settings(), SimpleNamespace(resolve=lambda _: {})
            )
            config = {"llm": route}
            execution = resolver(
                SimpleNamespace(
                    id="test",
                    source="arxiv",
                    question="test",
                    config_snapshot=config,
                    deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                ),
                CancellationScope(),
            )
            try:
                self.assertEqual(execution.llm.api_keys.available, expected)
                checks = run_configuration_diagnostics(
                    {"source": "arxiv", **config}, self.settings()
                )["checks"]
                self.assertEqual(
                    next(c["status"] for c in checks if c["id"] == "llm.api_key"),
                    "pass" if expected else "fail",
                )
            finally:
                await execution.close()

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

    async def test_local_models_bypass_system_proxy(self):
        async with ControlledAsyncHttpServer([200]) as server:
            with patch.dict(
                os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}
            ):
                resolver = CommunityExecutionResolver(
                    self.settings(), SimpleNamespace(resolve=lambda _: {})
                )
                execution = resolver(
                    SimpleNamespace(
                        id="local",
                        source="arxiv",
                        question="test",
                        config_snapshot={
                            "llm": {"provider": "ollama", "base_url": server.base_url}
                        },
                        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                    ),
                    CancellationScope(),
                )
                try:
                    self.assertEqual(await execution.llm.chat_json([]), "{}")
                    self.assertNotIn("authorization", server.requests[0]["headers"])
                finally:
                    await execution.close()

    async def test_local_execution_warms_the_model_up_and_never_fails_on_it(self):
        async with ControlledAsyncHttpServer([200, 200]) as server:
            resolver = CommunityExecutionResolver(
                self.settings(), SimpleNamespace(resolve=lambda _: {})
            )
            execution = resolver(
                SimpleNamespace(
                    id="warmup",
                    source="arxiv",
                    question="test",
                    config_snapshot={
                        "llm": {"provider": "ollama", "base_url": server.base_url}
                    },
                    deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                ),
                CancellationScope(),
            )
            try:
                self.assertIsNotNone(execution.warmup)
                await warm_up_model(execution.warmup)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(server.requests[0]["body"]["max_tokens"], 1)
            finally:
                await execution.close()

        async def failing() -> None:
            raise RuntimeError("model load failed")

        # A warm-up is an optimisation: the first real call reports the real failure.
        await warm_up_model(failing)
        await warm_up_model(None)

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
