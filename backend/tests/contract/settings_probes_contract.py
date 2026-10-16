"""Connectivity checks preserve the deployment's transport and error boundary."""

import unittest

import httpx

from paperseek_app.settings_probes import probe_connection, probe_source
from paperseek_core.sources.contracts import SourceError


class SettingsProbesTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_probe_uses_the_supplied_egress_client(self):
        route = {"base_url": "https://model.example/v1", "model": "test", "api_type": "openai_chat"}
        seen = []

        def client_factory(routes):
            self.assertEqual(routes, [route])

            def handle(request):
                seen.append(str(request.url))
                self.assertEqual(request.headers["Authorization"], "Bearer fixture-secret")
                return httpx.Response(401)

            return httpx.AsyncClient(transport=httpx.MockTransport(handle))

        result = await probe_connection("llm", route, "fixture-secret", client_factory=client_factory)
        self.assertEqual(seen, ["https://model.example/v1/chat/completions"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "http_401")
        self.assertNotIn("fixture-secret", str(result))

    async def test_local_model_probe_retries_a_cold_start_timeout(self):
        route = {"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1",
                 "model": "gemma4:12b-it-qat", "api_type": "openai_chat"}
        calls = []

        def client_factory(_routes):
            def handle(request):
                calls.append(request)
                if len(calls) == 1:
                    raise httpx.ReadTimeout("cold start", request=request)
                return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

            return httpx.AsyncClient(transport=httpx.MockTransport(handle))

        result = await probe_connection("llm", route, "", client_factory=client_factory)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "")
        # The first attempt only triggered the model load, so the retry proves reachability.
        self.assertEqual(len(calls), 2)

    async def test_hosted_model_probe_does_not_retry_a_timeout(self):
        route = {"provider": "openai", "base_url": "https://model.example/v1",
                 "model": "test", "api_type": "openai_chat"}
        calls = []

        def client_factory(_routes):
            def handle(request):
                calls.append(request)
                raise httpx.ReadTimeout("fixture timeout", request=request)

            return httpx.AsyncClient(transport=httpx.MockTransport(handle))

        result = await probe_connection("llm", route, "fixture-secret", client_factory=client_factory)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "timeout")
        self.assertEqual(len(calls), 1)

    async def test_source_probe_uses_the_supplied_adapter(self):
        requests = []

        class Source:
            async def search(self, request):
                requests.append(request)

        result = await probe_source(lambda _transport: Source())
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].limit, 1)
        self.assertEqual(requests[0].query, "machine learning")

    async def test_source_probe_reports_failure_without_the_sensitive_message(self):
        class Source:
            async def search(self, _request):
                raise SourceError("fixture", "source unavailable with private key", status=401)

        result = await probe_source(lambda _transport: Source())
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "http_401")
        self.assertNotIn("private key", str(result))
