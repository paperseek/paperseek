"""Shared connectivity checks with deployment-supplied clients and source adapters."""

import time
from collections.abc import Callable

import httpx

from paperseek_core.query_compiler import CHINESE_QUERY_SOURCES
from paperseek_core.decision_prompt import decision_question, decision_state
from paperseek_core.execution import RequestContext
from paperseek_core.models import PaperRecord
from paperseek_core.ports import SourcePort
from paperseek_core.sources import AsyncHttpTransport
from paperseek_core.sources.contracts import SourceError, SourceSearchRequest
from paperseek_core.sources.proxy import ProxyPolicy, ProxyPoolTransport

PROBE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _probe_code(exc: BaseException) -> str:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "connection_failed"
    return "request_failed"


async def probe_connection(
    kind: str, route: dict[str, str], api_key: str, *,
    client_factory: Callable[[list[dict]], httpx.AsyncClient],
) -> dict[str, object]:
    """One small real request; any 2xx answer proves endpoint, model and key.

    A deployment that restricts outbound requests supplies its own client factory.
    """
    base = route["base_url"].rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    prompt = "Reply with OK."
    requests: list[tuple[str, dict]] = []
    if kind == "llm" and route["api_type"] == "anthropic_messages":
        headers = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
        requests = [(f"{base}/v1/messages", {"model": route["model"], "max_tokens": 16,
                                            "messages": [{"role": "user", "content": prompt}]})]
    elif kind == "llm" and route["api_type"] == "openai_responses":
        body = {"model": route["model"], "input": prompt, "max_output_tokens": 16}
        requests = [(f"{base}/responses", body), (f"{base}/responses", {"model": route["model"], "input": prompt})]
    elif kind == "llm":
        body = {"model": route["model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 16}
        requests = [(f"{base}/chat/completions", body),
                    (f"{base}/chat/completions", {key: value for key, value in body.items() if key != "max_tokens"})]
    elif kind == "embedding":
        model = route["model"].replace(";", ",").split(",")[0].strip()
        body = {"model": model, "input": ["connectivity check"], "encoding_format": "float"}
        if route["provider"] == "nvidia":
            body["input_type"] = "query"
        requests = [(f"{base}/embeddings", body)]
    elif kind == "reranker":
        model = route["model"].replace(";", ",").split(",")[0].strip()
        if route["provider"] == "nvidia":
            url = base if base.endswith(("/reranking", "/ranking")) else f"{base}/reranking"
            requests = [(url, {"model": model, "query": {"text": "connectivity check"},
                               "passages": [{"text": "connectivity check"}]})]
        else:
            requests = [(f"{base}/rerank", {"model": model, "query": "connectivity check",
                                             "documents": ["connectivity check"], "top_n": 1})]
    else:
        requests = [(f"{base}/systemone", {
            "model": route["model"],
            "state": decision_state("connectivity check", ""),
            "questions": {"probe": decision_question(PaperRecord(uid="probe", title="Connectivity check"))},
        })]
    started = time.perf_counter()
    try:
        async with client_factory([route]) as client:
            for index, (url, body) in enumerate(requests):
                response = await client.post(url, json=body, headers=headers, timeout=PROBE_TIMEOUT)
                if response.status_code not in {400, 422} or index == len(requests) - 1:
                    break
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never raised
        return {"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000), "code": _probe_code(exc)}
    latency = int((time.perf_counter() - started) * 1000)
    if 200 <= response.status_code < 300:
        return {"ok": True, "latency_ms": latency, "code": ""}
    return {"ok": False, "latency_ms": latency, "code": f"http_{response.status_code}"}


async def probe_source(
    source_factory: Callable[[AsyncHttpTransport], SourcePort],
    *, proxy: ProxyPolicy | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT,
                transport=ProxyPoolTransport(proxy) if proxy and proxy.enabled else None,
                trust_env=not (proxy and proxy.enabled)) as client:
            transport = AsyncHttpTransport(client=client, context=RequestContext(
                step_key="settings.source_test", deadline_at=time.monotonic() + 30,
            ))
            source = source_factory(transport)
            query = "机器学习" if getattr(source, "name", "") in CHINESE_QUERY_SOURCES else "machine learning"
            await source.search(SourceSearchRequest(query=query, limit=1))
    except SourceError as exc:
        code = f"http_{exc.status}" if exc.status else str(getattr(exc, "failure_kind", "") or "request_failed")
        return {"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000), "code": code}
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never raised
        return {"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000), "code": _probe_code(exc)}
    return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000), "code": ""}
