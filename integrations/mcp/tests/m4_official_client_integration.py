from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from typing import Any
import unittest
from uuid import uuid4

import httpx2
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation


EXPECTED_TOOLS = [
    "start_search",
    "get_search_status",
    "get_search_events",
    "get_search_results",
    "get_citation_graph",
    "cancel_search",
    "export_results",
    "list_sources",
    "get_source_capabilities",
]
CLIENT_INFO = Implementation(name="paperseek-m4-gate", version="1.0.0b3")
SERVICE_TOKEN = "m4-official-client-token"
CLIENT_ORIGIN = "https://client.example.test"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for the installed MCP integration gate.")
    return value


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", "")
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise AssertionError("The MCP tool result did not contain a structured object.")


class _HangingLLMHandler(BaseHTTPRequestHandler):
    request_received = threading.Event()
    release = threading.Event()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).request_received.set()
        type(self).release.wait(timeout=30)
        body = b'{"choices":[{"message":{"content":"{}"}}]}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                f"paperseek-mcp exited before port {port} opened\nstdout={stdout}\nstderr={stderr}"
            )
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"paperseek-mcp did not open port {port}.")


class InstalledMCPGate:
    def __init__(self) -> None:
        self.executable = Path(_required_environment("PAPERSEEK_MCP_EXECUTABLE")).resolve()
        self.paperseek_executable = Path(
            _required_environment("PAPERSEEK_EXECUTABLE")
        ).resolve()
        self.isolated_prefix = Path(
            _required_environment("PAPERSEEK_ISOLATED_PREFIX")
        ).resolve()
        self.database = Path(_required_environment("PAPERSEEK_MCP_DATABASE")).resolve()
        self.completed_run_id = _required_environment("PAPERSEEK_COMPLETED_RUN_ID")
        self.stdio_stderr: list[str] = []
        self.http_logs: list[Any] = []
        if not self.executable.is_file() or not self.paperseek_executable.is_file():
            raise AssertionError("The installed console scripts are missing.")

        self.llm_port = _free_port()
        _HangingLLMHandler.request_received.clear()
        _HangingLLMHandler.release.clear()
        self.llm_server = _ThreadingHTTPServer(
            ("127.0.0.1", self.llm_port),
            _HangingLLMHandler,
        )
        self.llm_thread = threading.Thread(target=self.llm_server.serve_forever, daemon=True)
        self.llm_thread.start()

    def environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.update(
            {
                "PAPERSEEK_DATABASE": str(self.database),
                "PAPERSEEK_LLM_API_TYPE": "openai_chat",
                "PAPERSEEK_LLM_BASE_URL": f"http://127.0.0.1:{self.llm_port}/v1",
                "PAPERSEEK_LLM_MODEL": "m4-hanging-model",
                "PAPERSEEK_LLM_API_KEY": "fixture-only",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        return environment

    def close(self) -> None:
        _HangingLLMHandler.release.set()
        self.llm_server.shutdown()
        self.llm_server.server_close()
        self.llm_thread.join(timeout=5)
        for log in self.http_logs:
            if sys.exc_info()[0] is not None:
                log.seek(0)
                print(log.read(), file=sys.stderr)
            log.close()

    @asynccontextmanager
    async def stdio_session(self):
        parameters = StdioServerParameters(
            command=str(self.executable),
            args=["--transport", "stdio", "--database", str(self.database)],
            cwd=self.isolated_prefix,
            env=self.environment(),
            encoding="utf-8",
            encoding_error_handler="strict",
        )
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            try:
                async with stdio_client(parameters, errlog=stderr) as streams:
                    async with ClientSession(*streams, client_info=CLIENT_INFO, read_timeout_seconds=30) as session:
                        initialize = await session.initialize()
                        yield session, initialize
            finally:
                stderr.seek(0)
                self.stdio_stderr.append(stderr.read())

    def start_http(self, transport: str, port: int) -> subprocess.Popen[str]:
        environment = self.environment()
        environment.update(
            {
                "PAPERSEEK_MCP_SERVICE_TOKEN": SERVICE_TOKEN,
                "PAPERSEEK_MCP_PUBLIC_URL": f"http://127.0.0.1:{port}/mcp",
                "PAPERSEEK_MCP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
                "PAPERSEEK_MCP_ALLOWED_ORIGINS": CLIENT_ORIGIN,
            }
        )
        # A Windows pipe can fill before this polling-heavy test stops the
        # server. File-backed logs cannot block its event loop on access logs.
        log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.http_logs.append(log)
        process = subprocess.Popen(
            [
                str(self.executable),
                "--transport",
                transport,
                "--database",
                str(self.database),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=self.isolated_prefix,
            env=environment,
            text=True,
            encoding="utf-8",
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        _wait_for_port(port, process)
        return process

    @staticmethod
    def stop_http(process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    @asynccontextmanager
    async def streamable_session(
        self,
        port: int,
        *,
        token: str = SERVICE_TOKEN,
        origin: str = CLIENT_ORIGIN,
        host: str | None = None,
    ):
        headers = {"Authorization": f"Bearer {token}", "Origin": origin}
        if host is not None:
            headers["Host"] = host
        async with httpx2.AsyncClient(headers=headers, timeout=5.0, trust_env=False) as client:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp",
                http_client=client,
            ) as streams:
                async with ClientSession(*streams, client_info=CLIENT_INFO, read_timeout_seconds=30) as session:
                    initialize = await session.initialize()
                    yield session, initialize

    @asynccontextmanager
    async def legacy_sse_session(self, port: int):
        headers = {
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "Origin": CLIENT_ORIGIN,
        }
        async with sse_client(
            f"http://127.0.0.1:{port}/sse",
            headers=headers,
            httpx_client_factory=lambda **kwargs: httpx2.AsyncClient(**kwargs, trust_env=False),
        ) as streams:
            async with ClientSession(*streams, client_info=CLIENT_INFO, read_timeout_seconds=30) as session:
                initialize = await session.initialize()
                yield session, initialize

    async def verify_session(self, session: ClientSession, initialize: Any) -> list[dict[str, Any]]:
        if initialize.server_info.name != "paperseek" or initialize.server_info.version != "1.0.0b3":
            raise AssertionError(initialize)
        tools = await session.list_tools()
        schemas = [tool.model_dump(mode="json", by_alias=True) for tool in tools.tools]
        if [tool["name"] for tool in schemas] != EXPECTED_TOOLS:
            raise AssertionError(schemas)
        sources = _payload(await session.call_tool("list_sources"))["sources"]
        if len(sources) != 8:
            raise AssertionError(sources)
        capability = _payload(
            await session.call_tool("get_source_capabilities", {"source": "openalex"})
        )
        if capability["id"] != "openalex":
            raise AssertionError(capability)
        results = _payload(
            await session.call_tool(
                "get_search_results",
                {"run_id": self.completed_run_id, "page_size": 1},
            )
        )
        if len(results["results"]) != 1:
            raise AssertionError(results)
        graph = _payload(
            await session.call_tool(
                "get_citation_graph",
                {"run_id": self.completed_run_id},
            )
        )
        if not {"status", "nodes", "edges"}.issubset(graph) or not isinstance(graph["nodes"], list):
            raise AssertionError(graph)
        exported = _payload(
            await session.call_tool(
                "export_results",
                {"run_id": self.completed_run_id, "format": "json"},
            )
        )
        if exported["media_type"] != "application/json" or not exported["content"]:
            raise AssertionError(exported)
        return schemas

    async def start_poll_cancel(self, session: ClientSession, key: str) -> tuple[str, int]:
        _HangingLLMHandler.request_received.clear()
        idempotency_key = f"{key}-{uuid4()}"
        started = _payload(
            await session.call_tool(
                "start_search",
                {
                    "question": f"Durable cancellation {idempotency_key}",
                    "source": "openalex",
                    "search_budget": "fast",
                    "idempotency_key": idempotency_key,
                },
            )
        )
        run_id = started["run_id"]
        deadline = asyncio.get_running_loop().time() + 10
        status = ""
        while asyncio.get_running_loop().time() < deadline:
            detail = _payload(
                await session.call_tool("get_search_status", {"run_id": run_id})
            )
            status = detail["status"]
            if status == "running" and _HangingLLMHandler.request_received.is_set():
                break
            if status in {"failed", "cancelled"}:
                raise AssertionError(f"Run became {status} before cancellation.")
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"Run never reached the controlled running state: {status}")

        cancel = _payload(await session.call_tool("cancel_search", {"run_id": run_id}))
        if cancel["status"] not in {"cancelling", "cancelled"}:
            raise AssertionError(cancel)
        while asyncio.get_running_loop().time() < deadline + 10:
            detail = _payload(
                await session.call_tool("get_search_status", {"run_id": run_id})
            )
            if detail["status"] == "cancelled":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("The durable run did not reach cancelled.")
        events = _payload(
            await session.call_tool(
                "get_search_events",
                {"run_id": run_id, "after_sequence": 0, "limit": 100},
            )
        )
        if events["next_sequence"] < 2:
            raise AssertionError(events)
        return run_id, int(events["next_sequence"])

    async def verify_http_boundaries(self, port: int) -> None:
        headers = {
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "Origin": CLIENT_ORIGIN,
        }
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx2.AsyncClient(headers=headers, timeout=5.0, trust_env=False) as client:
            oversized = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                content=b"x" * (1024 * 1024 + 1),
            )
            if oversized.status_code != 413:
                raise AssertionError(oversized.status_code)


class M4OfficialClientIntegration(unittest.TestCase):
    def test_isolated_wheels_and_all_official_transports(self):
        asyncio.run(self._run_gate())

    async def _run_gate(self) -> None:
        gate = InstalledMCPGate()
        try:
            self.assertEqual(importlib.metadata.version("paperseek"), "1.0.0b3")
            self.assertEqual(importlib.metadata.version("paperseek-mcp"), "1.0.0b3")
            self.assertEqual(importlib.metadata.version("mcp"), "2.0.0")
            requirements = importlib.metadata.requires("paperseek-mcp") or []
            self.assertIn("paperseek==1.0.0b3", requirements)
            self.assertIn("mcp==2.0.0", requirements)
            registry_metadata = json.loads(
                (
                    gate.isolated_prefix
                    / "share"
                    / "paperseek-mcp"
                    / "server.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(registry_metadata["name"], "io.github.paperseek/paperseek")
            self.assertEqual(registry_metadata["version"], "1.0.0-beta3")
            import paperseek_app
            import paperseek_mcp

            self.assertTrue(Path(paperseek_app.__file__).resolve().is_relative_to(gate.isolated_prefix))
            self.assertTrue(Path(paperseek_mcp.__file__).resolve().is_relative_to(gate.isolated_prefix))
            self.assertNotIn("PYTHONPATH", os.environ)

            async with gate.stdio_session() as (session, initialize):
                stdio_schemas = await gate.verify_session(session, initialize)
                stdio_run, stdio_sequence = await gate.start_poll_cancel(
                    session,
                    "official-stdio-cancel",
                )
            async with gate.stdio_session() as (session, _initialize):
                recovered = _payload(
                    await session.call_tool("get_search_status", {"run_id": stdio_run})
                )
                replay = _payload(
                    await session.call_tool(
                        "get_search_events",
                        {"run_id": stdio_run, "after_sequence": 0, "limit": 100},
                    )
                )
                self.assertEqual(recovered["status"], "cancelled")
                self.assertEqual(replay["next_sequence"], stdio_sequence)

            streamable_port = _free_port()
            process = gate.start_http("streamable-http", streamable_port)
            try:
                with self.assertRaises(Exception):
                    async with gate.streamable_session(
                        streamable_port,
                        token="wrong-token",
                    ):
                        pass
                with self.assertRaises(Exception):
                    async with gate.streamable_session(
                        streamable_port,
                        host="evil.example.test",
                    ):
                        pass
                with self.assertRaises(Exception):
                    async with gate.streamable_session(
                        streamable_port,
                        origin="https://evil.example.test",
                    ):
                        pass
                async with gate.streamable_session(streamable_port) as (session, initialize):
                    streamable_schemas = await gate.verify_session(session, initialize)
                    http_run, http_sequence = await gate.start_poll_cancel(
                        session,
                        "official-http-cancel",
                    )
                async with gate.streamable_session(streamable_port) as (session, _initialize):
                    recovered = _payload(
                        await session.call_tool("get_search_status", {"run_id": http_run})
                    )
                    replay = _payload(
                        await session.call_tool(
                            "get_search_events",
                            {"run_id": http_run, "after_sequence": 0, "limit": 100},
                        )
                    )
                    self.assertEqual(recovered["status"], "cancelled")
                    self.assertEqual(replay["next_sequence"], http_sequence)
                await gate.verify_http_boundaries(streamable_port)
            finally:
                gate.stop_http(process)

            sse_port = _free_port()
            process = gate.start_http("sse-legacy", sse_port)
            try:
                async with gate.legacy_sse_session(sse_port) as (session, initialize):
                    sse_schemas = await gate.verify_session(session, initialize)
                    sse_run, sse_sequence = await gate.start_poll_cancel(
                        session,
                        "official-sse-cancel",
                    )
                async with gate.legacy_sse_session(sse_port) as (session, _initialize):
                    recovered = _payload(
                        await session.call_tool("get_search_status", {"run_id": sse_run})
                    )
                    replay = _payload(
                        await session.call_tool(
                            "get_search_events",
                            {"run_id": sse_run, "after_sequence": 0, "limit": 100},
                        )
                    )
                    self.assertEqual(recovered["status"], "cancelled")
                    self.assertEqual(replay["next_sequence"], sse_sequence)
            finally:
                gate.stop_http(process)

            self.assertEqual(stdio_schemas, streamable_schemas)
            self.assertEqual(stdio_schemas, sse_schemas)
            self.assertTrue(gate.stdio_stderr)
            self.assertTrue(all('"jsonrpc"' not in text for text in gate.stdio_stderr))
        finally:
            gate.close()


if __name__ == "__main__":
    unittest.main()
