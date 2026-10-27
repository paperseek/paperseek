from __future__ import annotations

import argparse
import asyncio
import json

import httpx2
from mcp import ClientSession
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


def _payload(result: object) -> dict:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", "")
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise AssertionError("MCP result did not contain a structured object")


async def _expect_rejected(url: str, headers: dict[str, str], label: str) -> None:
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=5.0) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                async with ClientSession(
                    *streams,
                    client_info=Implementation(name="paperseek-container-gate", version="1.0.0b5"),
                ) as session:
                    await session.initialize()
    except Exception:
        return
    raise AssertionError(f"{label} was accepted")


async def run(url: str, token: str, origin: str, host: str, run_id: str | None) -> None:
    await _expect_rejected(
        url,
        {"Authorization": "Bearer invalid", "Origin": origin, "Host": host},
        "invalid Bearer token",
    )
    await _expect_rejected(
        url,
        {"Authorization": f"Bearer {token}", "Origin": origin, "Host": "invalid.example"},
        "invalid Host",
    )
    await _expect_rejected(
        url,
        {"Authorization": f"Bearer {token}", "Origin": "https://invalid.example", "Host": host},
        "invalid Origin",
    )

    headers = {"Authorization": f"Bearer {token}", "Origin": origin, "Host": host}
    async with httpx2.AsyncClient(headers=headers, timeout=10.0) as client:
        oversized = await client.post(
            url,
            content=b"x" * (1024 * 1024 + 1),
            headers={"Content-Type": "application/octet-stream"},
        )
        if oversized.status_code != 413:
            raise AssertionError(f"oversized request returned {oversized.status_code}, not 413")

        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(
                *streams,
                client_info=Implementation(name="paperseek-container-gate", version="1.0.0b5"),
            ) as session:
                initialized = await session.initialize()
                if initialized.server_info.name != "paperseek":
                    raise AssertionError(initialized.server_info)
                if initialized.server_info.version != "1.0.0b5":
                    raise AssertionError(initialized.server_info)
                tools = await session.list_tools()
                names = [tool.name for tool in tools.tools]
                if names != EXPECTED_TOOLS:
                    raise AssertionError(names)
                sources = _payload(await session.call_tool("list_sources"))["sources"]
                if len(sources) != 11:
                    raise AssertionError(sources)
                if run_id is not None:
                    status = _payload(
                        await session.call_tool("get_search_status", {"run_id": run_id})
                    )
                    if status["status"] != "completed":
                        raise AssertionError(status)
                    results = _payload(
                        await session.call_tool(
                            "get_search_results", {"run_id": run_id, "page_size": 10}
                        )
                    )
                    if len(results["results"]) != 1:
                        raise AssertionError(results)
                    exported = _payload(
                        await session.call_tool(
                            "export_results", {"run_id": run_id, "format": "json"}
                        )
                    )
                    if exported["media_type"] != "application/json" or not exported["content"]:
                        raise AssertionError(exported)

    print(
        json.dumps(
            {"persisted_run": run_id, "sources": len(sources), "status": "ok", "tools": EXPECTED_TOOLS},
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.token, args.origin, args.host, args.run_id))


if __name__ == "__main__":
    main()
