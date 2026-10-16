from __future__ import annotations

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from paperseek_app.execution import CancellationScope
from paperseek_core.errors import LLMRequestError
from paperseek_core.http_retry import failure_details
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.retrieval_clients import (
    ExternalEmbeddingAdapter,
    ExternalRerankerAdapter,
)
from paperseek_core.models import PaperRecord
from paperseek_core.sources.contracts import SourceSearchRequest
from paperseek_core.sources.google_scholar import GoogleScholarSource
from paperseek_core.sources.http import AsyncHttpTransport


class ControlledAsyncHttpServer:
    def __init__(self, actions: list[int | str | tuple[int, object]]) -> None:
        self.actions = list(actions)
        self.requests: list[dict[str, object]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "ControlledAsyncHttpServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release.set()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        assert self._server is not None and self._server.sockets
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            header_lines = raw_headers.decode("latin-1").split("\r\n")
            method, path, _ = header_lines[0].split(" ", 2)
            headers = {
                name.strip().lower(): value.strip()
                for line in header_lines[1:]
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.requests.append(
                {
                    "method": method,
                    "path": path,
                    "headers": headers,
                    "body": json.loads(body) if body else None,
                }
            )
            action = self.actions.pop(0)
            if action == "disconnect":
                return
            if action == "block":
                self.entered.set()
                await self.release.wait()
                status = 200
            elif isinstance(action, tuple):
                status = action[0]
            else:
                status = int(action)
            payload = action[1] if isinstance(action, tuple) else (
                self._payload(path) if status == 200 else {"error": f"controlled-{status}"})
            content = json.dumps(payload).encode("utf-8")
            reason = "OK" if status == 200 else "Controlled Error"
            writer.write(
                (
                    f"HTTP/1.1 {status} {reason}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(content)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                + content
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    @staticmethod
    def _payload(path: str) -> dict:
        if path.endswith("/chat/completions"):
            return {"choices": [{"message": {"content": "{}"}}]}
        if path.endswith("/responses"):
            return {"output_text": "{}"}
        if path.endswith("/v1/messages"):
            return {"content": [{"type": "text", "text": "{}"}]}
        if path.endswith("/embeddings"):
            return {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 1, "embedding": [1.0, 0.0]},
                ]
            }
        if path.endswith(("/rerank", "/reranking")):
            return {"results": [{"index": 0, "relevance_score": 0.75}]}
        if path.endswith("/scholar"):
            return {"organic": [{"id": "gs-1", "title": "Controlled Scholar result"}]}
        raise AssertionError(path)


def _auth(request: dict[str, object], name: str = "authorization") -> str:
    return str(request["headers"][name])  # type: ignore[index]


def _record() -> PaperRecord:
    return PaperRecord(uid="paper-1", title="Controlled retrieval document")


class M4KeyPoolRetryContract(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        delay = patch("paperseek_core.http_retry.retry_delay", return_value=0)
        delay.start()
        self.addCleanup(delay.stop)

    async def test_every_protocol_preserves_the_requested_stage_output_limit(self) -> None:
        for api_type, field in (("openai_chat", "max_tokens"),
                                ("openai_responses", "max_output_tokens"),
                                ("anthropic_messages", "max_tokens")):
            with self.subTest(api_type=api_type):
                limits = (32, 2048, 4096, 8192)  # Diagnostic, query, planning, large ranking batch.
                async with ControlledAsyncHttpServer([200] * len(limits)) as server:
                    adapter, client = await self._llm(server, api_type=api_type, keys="controlled-key")
                    try:
                        for limit in limits:
                            self.assertEqual(await adapter.chat_json(
                                [{"role": "user", "content": "Return JSON."}], max_tokens=limit), "{}")
                    finally:
                        await client.aclose()
                self.assertEqual([request["body"][field] for request in server.requests], list(limits))

    async def test_provider_failures_have_safe_codes_and_bounded_attempts(self) -> None:
        cases = [
            (401, "provider_authentication_failed"), (403, "provider_authentication_failed"),
            (402, "provider_quota_exhausted"), (429, "provider_rate_limited"),
            ((429, {"error": {"code": "insufficient_quota", "message": "private-detail"}}), "provider_quota_exhausted"),
            ((429, {"error": {"type": "insufficient_quota", "message": "private-detail"}}), "provider_quota_exhausted"),
            ((429, {"error": {"message": "insufficient_quota private-detail"}}), "provider_rate_limited"),
            ((429, ["private-detail"]), "provider_rate_limited"),
            (500, "provider_unavailable"), (503, "provider_unavailable"),
            (404, "provider_request_rejected"), ("disconnect", "provider_connection_failed"),
            ((200, {"error": "private-detail"}), "provider_invalid_response"),
        ]
        for action, code in cases:
            with self.subTest(action=action):
                attempts = 5 if code in {"provider_rate_limited", "provider_unavailable", "provider_connection_failed"} else 1
                async with ControlledAsyncHttpServer([action] * attempts) as server:
                    adapter, client = await self._llm(server, keys="private-key")
                    try:
                        with self.assertRaises(LLMRequestError) as caught:
                            await adapter.chat_json([{"role": "user", "content": "Return JSON."}])
                        self.assertEqual(caught.exception.code, code)
                        self.assertNotIn("private-", str(caught.exception))
                        self.assertNotIn(server.base_url, str(caught.exception))
                    finally:
                        await client.aclose()
                self.assertEqual(len(server.requests), attempts)

    async def _llm(
        self,
        server: ControlledAsyncHttpServer,
        *,
        api_type: str = "openai_responses",
        keys: str = "key-a,key-b,key-c,key-d",
        cancellation: CancellationScope | None = None,
        provider: str = "",
    ) -> tuple[HttpLLMAdapter, httpx.AsyncClient]:
        client = httpx.AsyncClient(trust_env=False)
        return HttpLLMAdapter(
            client=client,
            cancellation=cancellation or CancellationScope(),
            api_type=api_type,
            base_url=server.base_url,
            model="controlled-model",
            api_key=keys,
            provider=provider,
        ), client

    async def test_explicitly_local_provider_shortens_the_retry_budget(self) -> None:
        async with ControlledAsyncHttpServer(["disconnect", "disconnect"]) as server:
            adapter, client = await self._llm(server, api_type="openai_chat", provider="ollama")
            try:
                with self.assertRaises(LLMRequestError) as caught:
                    await adapter.chat_json([{"role": "user", "content": "Return JSON."}])
            finally:
                await client.aclose()
        self.assertEqual(caught.exception.code, "provider_connection_failed")
        self.assertEqual(failure_details(caught.exception).get("attempts"), 2)
        self.assertEqual(len(server.requests), 2)

    async def test_llm_pool_splits_deduplicates_and_rotates_each_request(self) -> None:
        async with ControlledAsyncHttpServer([200, 200, 200]) as server:
            adapter, client = await self._llm(
                server,
                keys=" key-a, key-b;key-c\nkey-a ",
            )
            try:
                for _ in range(3):
                    self.assertEqual(
                        await adapter.chat_json([{"role": "user", "content": "Return JSON."}]),
                        "{}",
                    )
            finally:
                await client.aclose()
        self.assertEqual(
            [_auth(request) for request in server.requests],
            ["Bearer key-a", "Bearer key-b", "Bearer key-c"],
        )

    async def test_all_llm_protocols_switch_keys_only_for_retryable_status(self) -> None:
        for api_type, header in (
            ("openai_chat", "authorization"),
            ("openai_responses", "authorization"),
            ("anthropic_messages", "x-api-key"),
        ):
            for status in (401, 429, 500):
                with self.subTest(api_type=api_type, status=status):
                    async with ControlledAsyncHttpServer([status, 200]) as server:
                        adapter, client = await self._llm(server, api_type=api_type, keys="key-a,key-b")
                        try:
                            self.assertEqual(
                                await adapter.chat_json([{"role": "user", "content": "Return JSON."}]),
                                "{}",
                            )
                        finally:
                            await client.aclose()
                    expected = ["key-a", "key-b"]
                    actual = [_auth(request, header).removeprefix("Bearer ") for request in server.requests]
                    self.assertEqual(actual, expected)

    async def test_llm_400_does_not_switch_key_and_transient_failure_can_reach_fourth_key(self) -> None:
        async with ControlledAsyncHttpServer([400, 200]) as server:
            adapter, client = await self._llm(server)
            try:
                with self.assertRaises(LLMRequestError):
                    await adapter.chat_json([{"role": "user", "content": "Return JSON."}])
            finally:
                await client.aclose()
        self.assertEqual([_auth(request) for request in server.requests], ["Bearer key-a"])

        async with ControlledAsyncHttpServer([500, 500, 429, 200]) as server:
            adapter, client = await self._llm(server)
            try:
                self.assertEqual(await adapter.chat_json([{"role": "user", "content": "Return JSON."}]), "{}")
            finally:
                await client.aclose()
        self.assertEqual(
            [_auth(request) for request in server.requests],
            ["Bearer key-a", "Bearer key-b", "Bearer key-c", "Bearer key-d"],
        )

    async def test_llm_transport_error_switches_key_but_cancellation_never_retries(self) -> None:
        async with ControlledAsyncHttpServer(["disconnect", 200]) as server:
            adapter, client = await self._llm(server, keys="key-a,key-b")
            try:
                self.assertEqual(
                    await adapter.chat_json([{"role": "user", "content": "Return JSON."}]),
                    "{}",
                )
            finally:
                await client.aclose()
        self.assertEqual(
            [_auth(request) for request in server.requests],
            ["Bearer key-a", "Bearer key-b"],
        )

        cancellation = CancellationScope()
        async with ControlledAsyncHttpServer(["block", 200]) as server:
            adapter, client = await self._llm(
                server,
                keys="key-a,key-b",
                cancellation=cancellation,
            )
            task = asyncio.create_task(
                adapter.chat_json([{"role": "user", "content": "Return JSON."}])
            )
            await asyncio.wait_for(server.entered.wait(), timeout=1)
            cancellation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            server.release.set()
            await asyncio.sleep(0.05)
            await client.aclose()
        self.assertEqual([_auth(request) for request in server.requests], ["Bearer key-a"])

    async def test_embedding_and_reranker_recover_within_shared_attempt_budget(self) -> None:
        for kind in ("embedding", "reranker"):
            with self.subTest(kind=kind):
                async with ControlledAsyncHttpServer([500, 500, 500, 200]) as server:
                    client = httpx.AsyncClient(trust_env=False)
                    if kind == "embedding":
                        adapter = ExternalEmbeddingAdapter(
                            client,
                            CancellationScope(),
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b,key-c,key-d",
                        )
                        operation = adapter.embedding_scores("question", [_record()])
                    else:
                        adapter = ExternalRerankerAdapter(
                            client,
                            CancellationScope(),
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b,key-c,key-d",
                        )
                        operation = adapter.rerank("question", [_record()])
                    try:
                        await operation
                    finally:
                        await client.aclose()
                self.assertEqual(
                    [_auth(request) for request in server.requests],
                    ["Bearer key-a", "Bearer key-b", "Bearer key-c", "Bearer key-d"],
                )

    async def test_embedding_and_reranker_do_not_rotate_on_400(self) -> None:
        for kind in ("embedding", "reranker"):
            with self.subTest(kind=kind):
                async with ControlledAsyncHttpServer([400, 200]) as server:
                    client = httpx.AsyncClient(trust_env=False)
                    if kind == "embedding":
                        adapter = ExternalEmbeddingAdapter(
                            client,
                            CancellationScope(),
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b",
                        )
                        operation = adapter.embedding_scores("question", [_record()])
                    else:
                        adapter = ExternalRerankerAdapter(
                            client,
                            CancellationScope(),
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b",
                        )
                        operation = adapter.rerank("question", [_record()])
                    try:
                        with self.assertRaises(RuntimeError):
                            await operation
                    finally:
                        await client.aclose()
                self.assertEqual([_auth(request) for request in server.requests], ["Bearer key-a"])

    async def test_embedding_and_reranker_rotate_on_retryable_status_and_transport(self) -> None:
        for kind in ("embedding", "reranker"):
            for first_action in (401, 429, 500, "disconnect"):
                with self.subTest(kind=kind, first_action=first_action):
                    async with ControlledAsyncHttpServer([first_action, 200]) as server:
                        client = httpx.AsyncClient(trust_env=False)
                        if kind == "embedding":
                            adapter = ExternalEmbeddingAdapter(
                                client,
                                CancellationScope(),
                                provider="cstcloud",
                                model="controlled-model",
                                base_url=server.base_url,
                                api_key="key-a,key-b",
                            )
                            operation = adapter.embedding_scores("question", [_record()])
                        else:
                            adapter = ExternalRerankerAdapter(
                                client,
                                CancellationScope(),
                                provider="cstcloud",
                                model="controlled-model",
                                base_url=server.base_url,
                                api_key="key-a,key-b",
                            )
                            operation = adapter.rerank("question", [_record()])
                        try:
                            await operation
                        finally:
                            await client.aclose()
                    self.assertEqual(
                        [_auth(request) for request in server.requests],
                        ["Bearer key-a", "Bearer key-b"],
                    )

    async def test_embedding_and_reranker_rotate_start_key_and_do_not_retry_cancel(self) -> None:
        for kind in ("embedding", "reranker"):
            with self.subTest(kind=kind, behavior="rotate"):
                async with ControlledAsyncHttpServer([200, 200]) as server:
                    client = httpx.AsyncClient(trust_env=False)
                    if kind == "embedding":
                        adapter = ExternalEmbeddingAdapter(
                            client,
                            CancellationScope(),
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b",
                        )
                        operations = (
                            adapter.embedding_scores("question", [_record()]),
                            adapter.embedding_scores("question", [_record()]),
                        )
                    else:
                        adapter = ExternalRerankerAdapter(
                            client,
                            CancellationScope(),
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b",
                        )
                        operations = (
                            adapter.rerank("question", [_record()]),
                            adapter.rerank("question", [_record()]),
                        )
                    try:
                        for operation in operations:
                            await operation
                    finally:
                        await client.aclose()
                self.assertEqual(
                    [_auth(request) for request in server.requests],
                    ["Bearer key-a", "Bearer key-b"],
                )

            with self.subTest(kind=kind, behavior="cancel"):
                cancellation = CancellationScope()
                async with ControlledAsyncHttpServer(["block", 200]) as server:
                    client = httpx.AsyncClient(trust_env=False)
                    if kind == "embedding":
                        adapter = ExternalEmbeddingAdapter(
                            client,
                            cancellation,
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b",
                        )
                        operation = adapter.embedding_scores("question", [_record()])
                    else:
                        adapter = ExternalRerankerAdapter(
                            client,
                            cancellation,
                            provider="cstcloud",
                            model="controlled-model",
                            base_url=server.base_url,
                            api_key="key-a,key-b",
                        )
                        operation = adapter.rerank("question", [_record()])
                    task = asyncio.create_task(operation)
                    await asyncio.wait_for(server.entered.wait(), timeout=1)
                    cancellation.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=1)
                    server.release.set()
                    await asyncio.sleep(0.05)
                    await client.aclose()
                self.assertEqual([_auth(request) for request in server.requests], ["Bearer key-a"])

    async def test_google_scholar_source_keeps_frozen_pool_rotation(self) -> None:
        async with ControlledAsyncHttpServer([200, 200]) as server:
            client = httpx.AsyncClient(trust_env=False)
            transport = AsyncHttpTransport(client=client, sleep=lambda _seconds: asyncio.sleep(0))
            source = GoogleScholarSource(
                transport=transport,
                api_key="serper-a, serper-b;serper-c\nserper-a",
            )
            try:
                with patch.object(GoogleScholarSource, "BASE_URL", f"{server.base_url}/scholar"):
                    for _ in range(2):
                        result = await source.search(SourceSearchRequest(query="AI governance", limit=1))
                        self.assertEqual(result.hits[0].title, "Controlled Scholar result")
            finally:
                await client.aclose()
        self.assertEqual(
            [_auth(request, "x-api-key") for request in server.requests],
            ["serper-a", "serper-b"],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
