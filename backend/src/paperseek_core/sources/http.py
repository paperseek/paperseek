from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import asyncio
from contextlib import nullcontext
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from paperseek_core.execution import RequestContext
from paperseek_core.http_retry import TRANSIENT_STATUSES, cancellable_request, failure_details, request_with_retries, wait_for_retry
from paperseek_core.sources.contracts import SourceError, SourceFailureKind
from paperseek_core.sources.rate_limit import rate_limit_backoff, source_rate_limit


def split_api_keys(value: str) -> tuple[str, ...]:
    normalized = str(value or "").replace("\r", "\n").replace(",", "\n").replace(";", "\n")
    return tuple(dict.fromkeys(part.strip() for part in normalized.split("\n") if part.strip()))


def _redacted_url(url: str) -> str:
    parts = urlsplit(str(url))
    sensitive = {"api_key", "apikey", "key", "token", "x-api-key"}
    query = [
        (key, "***" if key.lower() in sensitive else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class AsyncHttpTransport:
    """Small injectable async HTTP boundary shared by concrete source adapters."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        context: RequestContext | None = None,
    ) -> None:
        self._owned_client = client is None
        self._client = client
        self._sleep = sleep
        self._context = context or RequestContext(step_key="source.request")

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def wait(self, seconds: float) -> None:
        await wait_for_retry(seconds, self._context.cancellation,
                             lambda delay: self._wait_with_context(delay, self._context, "source", "", 0))

    async def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | Callable[[int], Mapping[str, str]] | None = None,
        timeout: float | httpx.Timeout = 45,
        query: str = "",
        attempts: int = 3,
        retry_statuses: frozenset[int] = TRANSIENT_STATUSES,
        retry_transport: bool = True,
        retry_auth: Callable[[], bool] | None = None,
        min_interval: float | None = None,
        response_status: Callable[[httpx.Response], int | None] | None = None,
        context: RequestContext | None = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        request_context = context or self._context
        started = time.perf_counter()
        if self._client is None:
            self._client = httpx.AsyncClient()
        client = self._client
        attempted = 0
        response_headers_received = False

        async def send(attempt: int) -> httpx.Response:
            nonlocal attempted, response_headers_received
            attempted = attempt
            response_headers_received = False

            async def trace(name: str, _info: dict[str, Any]) -> None:
                nonlocal response_headers_received
                if name.endswith("receive_response_headers.complete"):
                    response_headers_received = True

            kwargs = {
                "params": dict(params) if params is not None else None,
                "json": json,
                **({"data": dict(data)} if data is not None else {}),
                "headers": dict((headers(attempt) if callable(headers) else headers) or {}),
                "timeout": timeout,
            }
            if isinstance(client, httpx.AsyncClient):
                kwargs["extensions"] = {"trace": trace, "paperseek_source": source}
            try:
                remaining = self._remaining(request_context)
                deadline_scope = (
                    asyncio.timeout(remaining)
                    if remaining is not None
                    else nullcontext()
                )
                async with deadline_scope:
                    limiter = source_rate_limit(source, url, params, kwargs['headers'], min_interval)

                    async def paced_request():
                        if limiter is None:
                            return await client.request(method, url, **kwargs)
                        async with limiter.sending(attempt, lambda delay: self._wait_with_context(
                            delay, request_context, source, query, attempted,
                        )):
                            result = await client.request(method, url, **kwargs)
                            status = response_status(result) if response_status is not None else None
                            if status is not None and status != result.status_code:
                                # The body is already decoded, so drop the encoding headers that describe the wire form.
                                headers = [(name, value) for name, value in result.headers.multi_items()
                                           if name.lower() not in {'content-encoding', 'content-length', 'transfer-encoding'}]
                                result = httpx.Response(status, headers=headers, content=result.content,
                                    request=result.request, extensions={**result.extensions, 'paperseek_upstream_status': result.status_code})
                            limiter.observe(result, attempt)
                            return result

                    response = await cancellable_request(
                        paced_request, request_context.cancellation,
                    )
            except TimeoutError as exc:
                raise self._deadline_error(source, query, attempted) from exc
            return response

        try:
            response, attempted = await request_with_retries(
                send, cancellation=request_context.cancellation, attempts=attempts,
                retry_statuses=retry_statuses, retry_transport=retry_transport,
                retry_auth=retry_auth,
                sleep=lambda delay: self._wait_with_context(delay, request_context, source, query, attempted),
                rate_limit_delay=rate_limit_backoff(source),
            )
        except httpx.RequestError as exc:
            raise SourceError(
                source, f"Source request failed after {attempted} attempt(s).", query=query,
                failure_kind=self._failure_kind(exc, response_headers_received), retry_details=failure_details(exc),
            ) from exc
        return response, {
            "method": method.upper(), "url": _redacted_url(str(response.request.url) if response.request else url),
            "status": response.status_code, "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "attempts": attempted,
        }

    @staticmethod
    def _remaining(context: RequestContext) -> float | None:
        if context.deadline_at is None:
            return None
        remaining = context.deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request step deadline exceeded")
        return remaining

    async def _wait_with_context(
        self,
        seconds: float,
        context: RequestContext,
        source: str,
        query: str,
        attempted: int,
    ) -> None:
        context.cancellation.raise_if_cancelled()
        try:
            remaining = self._remaining(context)
            deadline_scope = asyncio.timeout(remaining) if remaining is not None else nullcontext()
            async with deadline_scope:
                await self._sleep(seconds)
        except TimeoutError as exc:
            raise self._deadline_error(source, query, attempted) from exc
        context.cancellation.raise_if_cancelled()

    @staticmethod
    def _deadline_error(source: str, query: str, attempted: int) -> SourceError:
        return SourceError(
            source,
            f"Source request step deadline exceeded after {attempted} attempt(s).",
            query=query,
            failure_kind=SourceFailureKind.STEP_DEADLINE,
        )

    @staticmethod
    def _failure_kind(
        exc: httpx.RequestError,
        response_headers_received: bool,
    ) -> SourceFailureKind:
        if isinstance(exc, httpx.ConnectTimeout):
            return SourceFailureKind.CONNECT_TIMEOUT
        if isinstance(exc, httpx.WriteTimeout):
            return SourceFailureKind.SEND_TIMEOUT
        if isinstance(exc, httpx.ReadTimeout):
            return (
                SourceFailureKind.READ_IDLE_TIMEOUT
                if response_headers_received
                else SourceFailureKind.FIRST_BYTE_TIMEOUT
            )
        if isinstance(exc, httpx.ConnectError):
            return SourceFailureKind.CONNECT_ERROR
        return SourceFailureKind.TRANSPORT_ERROR
