"""A deployment proxy pool follows its scope and never leaks proxy credentials."""

import asyncio
from contextlib import closing
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.proxy import (
    PROXY_SCOPES, ProxyPolicy, ProxyPoolTransport, parse_proxy_scope, parse_proxy_urls, traffic_kind,
)
from paperseek_core.sources.rate_limit import reset_source_rate_limits
from paperseek_core.sources.contracts import SourceError, SourceSearchRequest
from paperseek_core.execution import NEVER_CANCELLED
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_app.settings import ProxySettingsInput, SettingsError
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.persistence.migrate import migrate_database
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import WorkspaceSettingsStore


class ProxyConfigurationTest(unittest.TestCase):
    def test_pool_formats_scope_and_sanitized_validation(self):
        secret = "http://user:private-password@proxy.example:8080/"
        other = "http://proxy-b.example:8080"
        self.assertEqual(parse_proxy_urls(f"{secret}\n{other}\n{secret}"), (secret, other))
        self.assertEqual(parse_proxy_urls(json.dumps([secret, other])), (secret, other))
        self.assertEqual(parse_proxy_urls(f"{secret}, {other}"), (secret, other))
        self.assertEqual(parse_proxy_urls(""), ())
        for value in ("broken json with private-password", "[1]", {"pubmed": secret},
                      "socks5://user:private-password@host:80", "http://user:private-password@host:bad",
                      secret + "?token=private-password"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    parse_proxy_urls(value)
                self.assertNotIn("private-password", str(caught.exception))
        self.assertEqual(parse_proxy_scope(""), frozenset(PROXY_SCOPES))
        self.assertEqual(parse_proxy_scope("ip_limited_sources, models"), {"ip_limited_sources", "models"})
        with self.assertRaises(ValueError):
            parse_proxy_scope("everything")

    def test_sources_are_classified_by_how_they_limit_requests(self):
        def kind(source, url="https://source.example/search", headers=None):
            request = httpx.Request("GET", url, headers=headers,
                                    extensions={"paperseek_source": source} if source else {})
            return traffic_kind(request)
        self.assertEqual(kind(None), "models")
        self.assertEqual(kind("systemone"), "models")
        for source in ("arxiv", "crossref", "paperhub", "pubmed", "openalex"):
            self.assertEqual(kind(source), "ip_limited_sources", source)
        self.assertEqual(kind("pubmed", "https://source.example/search?api_key=fixture"), "other_sources")
        self.assertEqual(kind("openalex", "https://source.example/works?api_key=fixture"), "other_sources")
        self.assertEqual(kind("semanticscholar"), "other_sources")
        self.assertEqual(kind("semanticscholar", headers={"x-api-key": "fixture"}), "other_sources")
        for source in ("wos", "googlescholar"):
            self.assertEqual(kind(source), "other_sources", source)


class SavedProxySettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Path(self.temp.name) / "paperseek.db"
        migrate_database(database)
        self.database = database
        self.store = WorkspaceSettingsStore(database, EncryptedRunCredentialStore(database))

    def tearDown(self):
        self.temp.cleanup()

    def test_saved_proxies_are_encrypted_write_only_and_cleared_with_keys(self):
        secret = "http://user:private-password@proxy-a.example:8080"
        saved = self.store.save_proxy(ProxySettingsInput(urls=f"{secret}\nhttp://proxy-b.example", scope=["ip_limited_sources"]))
        self.assertEqual(saved, {"origin": "settings", "hosts": ["proxy-a.example:8080", "proxy-b.example"],
                                 "scope": ["ip_limited_sources"]})
        with closing(sqlite3.connect(self.database)) as connection:
            stored = str(connection.execute("SELECT value_json FROM workspace_settings WHERE key = 'proxy'").fetchone())
        self.assertNotIn("private-password", stored)
        # Omitted addresses are kept when only the scope changes.
        self.store.save_proxy(ProxySettingsInput(scope=["models", "other_sources"]))
        policy = self.store.proxy_policy()
        self.assertEqual(policy.urls, (secret, "http://proxy-b.example"))
        self.assertEqual(policy.scope, {"models", "other_sources"})
        with self.assertRaises(SettingsError) as caught:
            self.store.save_proxy(ProxySettingsInput(urls="socks5://private-password@host"))
        self.assertEqual(caught.exception.code, "proxy_url_invalid")
        self.assertEqual(self.store.clear_secrets()["proxies"], 2)
        self.assertEqual(self.store.proxy(), {"origin": "", "hosts": [], "scope": list(PROXY_SCOPES)})

    def test_environment_proxy_wins_and_is_read_only(self):
        self.store.save_proxy(ProxySettingsInput(urls="http://saved.example:80"))
        with patch.dict("os.environ", {"PAPERSEEK_PROXY_URLS": "http://env.example:80",
                                       "PAPERSEEK_PROXY_SCOPE": "ip_limited_sources"}, clear=True):
            deployment = CommunityDeploymentSettings.from_environment()
        self.assertEqual(self.store.proxy_policy(deployment).urls, ("http://env.example:80",))
        self.assertEqual(self.store.proxy(deployment)["origin"], "environment")
        with self.assertRaises(SettingsError) as caught:
            self.store.save_proxy(ProxySettingsInput(urls=""), deployment)
        self.assertEqual(caught.exception.code, "proxy_set_by_environment")


def proxy_factory(calls, response=None):
    def factory(*, proxy=None, **_kwargs):
        def handle(request):
            calls.append((proxy, request))
            if proxy == "http://bad.example:80":
                raise httpx.ConnectError("unreachable fixture", request=request)
            return response(request) if response else httpx.Response(200)
        return httpx.MockTransport(handle)
    return factory


class ProxyPoolTransportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_source_rate_limits()

    async def test_community_configuration_follows_the_scope_and_stays_out_of_run_state(self):
        route = "http://user:private-password@proxy.example:80"
        calls = []
        factory = proxy_factory(calls, lambda _request: httpx.Response(
            200, json={"esearchresult": {"count": "0", "idlist": []}}))
        with patch.dict("os.environ", {"PAPERSEEK_PROXY_URLS": route,
                                       "PAPERSEEK_PROXY_SCOPE": "ip_limited_sources"}, clear=True):
            settings = CommunityDeploymentSettings.from_environment()
        self.assertNotIn("private-password", repr(settings))
        self.assertNotIn("proxy", settings.model_dump())
        run = SimpleNamespace(id="fixture", source="pubmed", question="research", config_snapshot={},
                              deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1))
        credentials = Mock(resolve=Mock(return_value={}))
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=factory), \
             patch("paperseek_core.sources.proxy.getproxies", return_value={}):
            execution = CommunityExecutionResolver(settings, credentials)(run, NEVER_CANCELLED)
            try:
                await execution.source.search(SourceSearchRequest(query="therapy"))
                await execution.llm.client.get("https://model.example/probe")
            finally:
                await execution.close()
        self.assertEqual([proxy for proxy, _ in calls], [route, None])
        self.assertNotIn("private-password", str(run.config_snapshot))

    async def test_pool_rotates_on_retry_and_other_traffic_keeps_the_system_proxy(self):
        calls = []
        policy = ProxyPolicy(("http://bad.example:80", "http://good.example:80"), frozenset({"ip_limited_sources"}))
        system = "http://system.example:3128"
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=proxy_factory(calls)), \
             patch("paperseek_core.sources.proxy.random.randrange", return_value=0), \
             patch("paperseek_core.sources.proxy.getproxies", return_value={"https": system, "http": system}), \
             patch("paperseek_core.sources.proxy.proxy_bypass", return_value=False):
            async with httpx.AsyncClient(transport=ProxyPoolTransport(policy), trust_env=False) as client:
                transport = AsyncHttpTransport(client=client, sleep=AsyncMock())
                response, details = await transport.request("pubmed", "GET", "https://source.example/search")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(details["attempts"], 2)
                await transport.request("semanticscholar", "GET", "https://source.example/paper")
                await client.post("https://model.example/chat", json={"prompt": "test"})
                await client.post("http://127.0.0.1:8324/v1/chat", json={"prompt": "test"})
        self.assertEqual([proxy for proxy, _ in calls],
                         ["http://bad.example:80", "http://good.example:80", system, system, None])
        self.assertTrue(all("Proxy-Authorization" not in request.headers for _, request in calls))

    async def test_model_scope_never_sends_local_endpoints_through_the_pool(self):
        calls = []
        policy = ProxyPolicy(("http://pool.example:80",), frozenset({"models"}))
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=proxy_factory(calls)), \
             patch("paperseek_core.sources.proxy.getproxies", return_value={}):
            async with httpx.AsyncClient(transport=ProxyPoolTransport(policy), trust_env=False) as client:
                await client.post("https://model.example/chat")
                await client.post("http://localhost:11434/api/chat")
                await client.post("http://10.0.0.5:8000/v1/chat")
                await AsyncHttpTransport(client=client).request("arxiv", "GET", "https://source.example/api/query")
        self.assertEqual([proxy for proxy, _ in calls], ["http://pool.example:80", None, None, None])

    async def test_proxy_407_never_falls_back_to_direct_or_retries(self):
        calls = []
        def factory(*, proxy=None, **_kwargs):
            def handle(request):
                calls.append(proxy)
                return httpx.Response(407)
            return httpx.MockTransport(handle)
        with patch("paperseek_core.sources.proxy.httpx.AsyncHTTPTransport", side_effect=factory):
            async with httpx.AsyncClient(transport=ProxyPoolTransport(ProxyPolicy(("http://proxy.example:80",)))) as client:
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
            async with httpx.AsyncClient(transport=ProxyPoolTransport(ProxyPolicy(("http://proxy.example:80",)))) as client:
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
            async with httpx.AsyncClient(transport=ProxyPoolTransport(ProxyPolicy((route,))), trust_env=False) as client:
                response, details = await AsyncHttpTransport(client=client).request("pubmed", "GET", "http://origin.invalid/search")
        self.assertEqual(response.text, "OK")
        self.assertIn("GET http://origin.invalid/search HTTP/1.1", received[0])
        credential = base64.b64encode(b"fixture-user:fixture-password").decode()
        self.assertIn("Proxy-Authorization: Basic " + credential, received[0])
        self.assertNotIn("Proxy-Authorization", response.request.headers)
        self.assertNotIn("fixture-password", str(details))


if __name__ == "__main__":
    unittest.main()
