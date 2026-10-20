from __future__ import annotations

import httpx


class FakeClient:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url: str, *, json: dict, headers: dict) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "headers": headers})
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))
