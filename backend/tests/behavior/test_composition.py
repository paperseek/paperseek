from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fixtures.model_http import ControlledAsyncHttpServer

from paperseek_app.execution import CancellationScope
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.diagnostics import run_configuration_diagnostics
from paperseek_community.settings import CommunityDeploymentSettings


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
