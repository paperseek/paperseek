from __future__ import annotations

import asyncio
import json


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
