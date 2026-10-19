"""Deployment-owned source proxies stay isolated from model traffic and credentials."""

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.proxy import SourceProxyTransport, parse_source_proxies
from paperseek_core.sources.contracts import SourceError, SourceSearchRequest
from paperseek_core.execution import NEVER_CANCELLED
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.settings import CommunityDeploymentSettings


class SourceProxyConfigurationTest(unittest.TestCase):
    def test_single_and_pool_configuration_and_sanitized_validation(self):
        secret = "http://user:private-password@proxy.example:8080/"
        self.assertEqual(parse_source_proxies(json.dumps({"pubmed": secret, "arxiv": [secret, secret]})),
                         {"pubmed": (secret,), "arxiv": (secret,)})
        self.assertEqual(parse_source_proxies(""), {})
        for value in ("broken json with private-password", [], "null", [secret], {"llm": secret},
                      {"pubmed": ["socks5://user:private-password@host:80"]},
                      {"pubmed": ["http://user:private-password@host:bad"]}, {"pubmed": [secret + "?token=private-password"]}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    parse_source_proxies(value)
                self.assertNotIn("private-password", str(caught.exception))


class SourceProxyTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_community_configuration_routes_only_sources_and_stays_out_of_run_state(self):
        route = "http://user:private-password@proxy.example:80"
        calls = []
        def factory(*, proxy=None, **_kwargs):
            def handle(request):
                calls.append((proxy, str(request.url)))
                return httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})
            return httpx.MockTransport(handle)
        with patch.dict("os.environ", {"PAPERSEEK_SOURCE_PROXIES": json.dumps({"pubmed": route})}, clear=True):
            settings = CommunityDeploymentSettings.from_environment()
        self.assertNotIn("private-password", repr(settings))
        self.assertNotIn("source_proxies", settings.model_dump())
        run = SimpleNamespace(id="fixture", source="pubmed", question="research", config_snapshot={},
                              deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1))
        credentials = Mock(resolve=Mock(return_value={}))
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=factory), \
             patch("paperseek_community.composition.create_http_client",
                   side_effect=lambda _: httpx.AsyncClient(transport=factory(), trust_env=False)):
            execution = CommunityExecutionResolver(settings, credentials)(run, NEVER_CANCELLED)
            try:
                await execution.source.search(SourceSearchRequest(query="therapy"))
                await execution.llm.client.get("https://model.example/probe")
            finally:
                await execution.close()
        self.assertEqual([proxy for proxy, _ in calls], [route, None])
        self.assertNotIn("private-password", str(run.config_snapshot))

    async def test_source_pool_rotates_on_bounded_retry_and_keeps_other_traffic_direct(self):
        calls = []
        def factory(*, proxy=None, **_kwargs):
            def handle(request):
                calls.append((proxy, request))
                if proxy == "http://bad.example:80":
                    raise httpx.ConnectError("unreachable fixture", request=request)
                return httpx.Response(200)
            return httpx.MockTransport(handle)
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=factory), \
             patch("paperseek_core.sources.proxy.random.randrange", return_value=0):
            async with httpx.AsyncClient(transport=SourceProxyTransport({"pubmed": (
                    "http://bad.example:80", "http://good.example:80")}), trust_env=False) as client:
                transport = AsyncHttpTransport(client=client, sleep=AsyncMock())
                response, details = await transport.request("pubmed", "GET", "https://source.example/search")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(details["attempts"], 2)
                await transport.request("crossref", "GET", "https://source.example/works")
                await client.post("https://model.example/chat", json={"prompt": "test"})
        self.assertEqual([proxy for proxy, _ in calls], ["http://bad.example:80", "http://good.example:80", None, None])
        self.assertTrue(all("Proxy-Authorization" not in request.headers for _, request in calls))

    async def test_proxy_407_never_falls_back_to_direct_or_retries(self):
        calls = []
        def factory(*, proxy=None, **_kwargs):
            def handle(request):
                calls.append(proxy)
                return httpx.Response(407)
            return httpx.MockTransport(handle)
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=factory):
            async with httpx.AsyncClient(transport=SourceProxyTransport({"pubmed": ("http://proxy.example:80",)})) as client:
                response, details = await AsyncHttpTransport(client=client).request("pubmed", "GET", "https://source.example")
        self.assertEqual(response.status_code, 407)
        self.assertEqual(details["attempts"], 1)
        self.assertEqual(calls, ["http://proxy.example:80"])

    async def test_https_connect_407_is_classified_without_sensitive_proxy_text(self):
        requests = []
        def factory(**_kwargs):
            def handle(request):
                requests.append(request)
                raise httpx.ProxyError("407 Proxy Authentication Required with private-password", request=request)
            return httpx.MockTransport(handle)
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=factory):
            async with httpx.AsyncClient(transport=SourceProxyTransport({"pubmed": ("http://proxy.example:80",)})) as client:
                with self.assertRaises(SourceError) as caught:
                    await AsyncHttpTransport(client=client).request("pubmed", "GET", "https://source.example")
        self.assertEqual(caught.exception.status, 407)
        self.assertNotIn("private-password", str(caught.exception))
        self.assertEqual(len(requests), 1)

    async def test_real_http_proxy_authentication_is_only_sent_to_proxy(self):
        received = []
        async def proxy(reader, writer):
            received.append((await reader.readuntil(b"\r\n\r\n")).decode())
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            route = f"http://fixture-user:fixture-password@127.0.0.1:{port}"
            async with httpx.AsyncClient(transport=SourceProxyTransport({"pubmed": (route,)}), trust_env=False) as client:
                response, details = await AsyncHttpTransport(client=client).request("pubmed", "GET", "http://origin.invalid/search")
        self.assertEqual(response.text, "OK")
        self.assertIn("GET http://origin.invalid/search HTTP/1.1", received[0])
        credential = base64.b64encode(b"fixture-user:fixture-password").decode()
        self.assertIn("Proxy-Authorization: Basic " + credential, received[0])
        self.assertNotIn("Proxy-Authorization", response.request.headers)
        self.assertNotIn("fixture-password", str(details))


if __name__ == "__main__":
    unittest.main()
