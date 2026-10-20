from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest


MCP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_ROOT / "src"))


class M4MCPTransportSecurityContract(unittest.TestCase):
    def test_private_token_auth_does_not_advertise_a_nonexistent_oauth_issuer(self):
        import httpx
        from paperseek_mcp.cli import parse_config
        from paperseek_mcp.transports import create_runtime

        async def exercise():
            runtime = create_runtime(parse_config(
                ["--transport", "streamable-http"],
                environment={"PAPERSEEK_MCP_SERVICE_TOKEN": "private-test-token"},
            ), object())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=runtime.http_application),
                base_url="http://127.0.0.1:8766",
            ) as client:
                missing = await client.post("/mcp", json={})
                wrong = await client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
                self.assertEqual(missing.status_code, 401)
                self.assertEqual(wrong.status_code, 401)
                self.assertNotIn("resource_metadata", missing.headers["www-authenticate"])
                metadata = await client.get(
                    "/.well-known/oauth-protected-resource/mcp",
                    headers={"Authorization": "Bearer private-test-token"},
                )
                self.assertEqual(metadata.status_code, 404)

        asyncio.run(exercise())

    def test_http_rate_boundary_rejects_the_first_excess_request(self):
        from paperseek_mcp.transports import BoundedHTTPApplication

        async def exercise() -> list[int]:
            async def application(scope, receive, send):
                del scope, receive
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})

            bounded = BoundedHTTPApplication(
                application,
                max_body_size=1024,
                max_concurrency=2,
                requests_per_minute=2,
            )
            statuses = []
            for _ in range(3):
                messages = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    messages.append(message)

                await bounded(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/mcp",
                        "headers": [],
                    },
                    receive,
                    send,
                )
                statuses.append(messages[0]["status"])
            return statuses

        self.assertEqual(asyncio.run(exercise()), [204, 204, 429])

    def test_local_cli_defaults_to_stdio_and_loopback(self):
        from paperseek_mcp.cli import parse_config

        config = parse_config([], environment={})

        self.assertEqual(config.transport, "stdio")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8766)
        self.assertEqual(config.path, "/mcp")
        self.assertEqual(config.service_token, "")

    def test_non_loopback_rejects_missing_or_incomplete_security_configuration(self):
        from paperseek_mcp.cli import parse_config

        with self.assertRaisesRegex(ValueError, "service token"):
            parse_config(
                ["--transport", "streamable-http", "--host", "0.0.0.0"],
                environment={},
            )
        with self.assertRaisesRegex(ValueError, "HTTPS public URL"):
            parse_config(
                ["--transport", "streamable-http", "--host", "0.0.0.0"],
                environment={"PAPERSEEK_MCP_SERVICE_TOKEN": "secret"},
            )
        with self.assertRaisesRegex(ValueError, "allowed Host"):
            parse_config(
                ["--transport", "streamable-http", "--host", "0.0.0.0"],
                environment={
                    "PAPERSEEK_MCP_SERVICE_TOKEN": "secret",
                    "PAPERSEEK_MCP_PUBLIC_URL": "https://mcp.example.test/mcp",
                },
            )
        with self.assertRaisesRegex(ValueError, "allowed Origin"):
            parse_config(
                ["--transport", "streamable-http", "--host", "0.0.0.0"],
                environment={
                    "PAPERSEEK_MCP_SERVICE_TOKEN": "secret",
                    "PAPERSEEK_MCP_PUBLIC_URL": "https://mcp.example.test/mcp",
                    "PAPERSEEK_MCP_ALLOWED_HOSTS": "mcp.example.test",
                },
            )

    def test_non_loopback_runtime_fixes_auth_host_origin_body_and_load_boundaries(self):
        from paperseek_mcp.cli import parse_config
        from paperseek_mcp.transports import (
            MAX_CONCURRENT_HTTP_REQUESTS,
            MAX_HTTP_REQUEST_BODY_BYTES,
            MAX_HTTP_REQUESTS_PER_MINUTE,
            create_runtime,
        )

        config = parse_config(
            ["--transport", "streamable-http", "--host", "0.0.0.0"],
            environment={
                "PAPERSEEK_MCP_SERVICE_TOKEN": "correct-token",
                "PAPERSEEK_MCP_PUBLIC_URL": "https://mcp.example.test/mcp",
                "PAPERSEEK_MCP_ALLOWED_HOSTS": "mcp.example.test",
                "PAPERSEEK_MCP_ALLOWED_ORIGINS": "https://mcp.example.test",
            },
        )
        runtime = create_runtime(config, object())

        self.assertEqual(runtime.transport, "streamable-http")
        self.assertEqual(runtime.security.allowed_hosts, ["mcp.example.test"])
        self.assertEqual(runtime.security.allowed_origins, ["https://mcp.example.test"])
        self.assertEqual(runtime.http_application.max_body_size, MAX_HTTP_REQUEST_BODY_BYTES)
        self.assertEqual(runtime.http_application.max_concurrency, MAX_CONCURRENT_HTTP_REQUESTS)
        self.assertEqual(runtime.http_application.requests_per_minute, MAX_HTTP_REQUESTS_PER_MINUTE)
        accepted = asyncio.run(runtime.token_verifier.verify_token("correct-token"))
        rejected = asyncio.run(runtime.token_verifier.verify_token("wrong-token"))
        self.assertEqual(accepted.client_id, "paperseek-mcp-service")
        self.assertIsNone(rejected)

    def test_streamable_http_and_legacy_sse_share_one_server_tool_registry(self):
        from paperseek_mcp.cli import parse_config
        from paperseek_mcp.tools import TOOL_NAMES
        from paperseek_mcp.transports import create_runtime

        environment = {}
        streamable = create_runtime(
            parse_config(["--transport", "streamable-http"], environment=environment),
            object(),
        )
        legacy = create_runtime(
            parse_config(["--transport", "sse-legacy"], environment=environment),
            object(),
        )

        streamable_names = [tool.name for tool in asyncio.run(streamable.server.list_tools())]
        legacy_names = [tool.name for tool in asyncio.run(legacy.server.list_tools())]
        self.assertEqual(streamable_names, list(TOOL_NAMES))
        self.assertEqual(legacy_names, list(TOOL_NAMES))
        self.assertEqual(streamable.path, "/mcp")
        self.assertEqual(legacy.sse_path, "/sse")
        self.assertEqual(legacy.message_path, "/messages/")


if __name__ == "__main__":
    unittest.main()
