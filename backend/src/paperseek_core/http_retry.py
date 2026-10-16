"""Bounded HTTP retries shared by source and model transports."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from paperseek_core.ports import CancellationToken

TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})
MODEL_ATTEMPTS = 5
# A local runtime re-sends the same slow request, so retrying it only spends the run deadline.
LOCAL_MODEL_ATTEMPTS = 2
MAX_RETRY_WAIT = 60.0
_QUOTA_CODES = {
    "insufficient_quota",
    "quota_exceeded",
    "billing_hard_limit_reached",
    "credit_balance_too_low",
}
_SAFE_CODES = _QUOTA_CODES | {
    "rate_limit_exceeded",
    "rate_limit_error",
    "overloaded_error",
    "server_error",
    "invalid_api_key",
    "invalid_request_error",
    "context_length_exceeded",
}
_logger = logging.getLogger(__name__)


def response_details(response: httpx.Response) -> dict:
    details: dict[str, object] = {"http_status": response.status_code}
    if not response.is_error:
        return details
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else {}
        code = error.get("code", error.get("type")) if isinstance(error, dict) else None
        if (
            type(code) is int
            or isinstance(code, str)
            and (code in _SAFE_CODES or code.isdecimal() and len(code) <= 12)
        ):
            details["provider_error_code"] = code
    except (ValueError, UnicodeError):
        pass
    return details


def failure_details(error: BaseException) -> dict:
    """Only return explicit, safe facts, never response text, URLs or headers."""
    current: BaseException | None = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            for member in current.exceptions:
                details = failure_details(member)
                if details:
                    return details
        recorded = getattr(current, "retry_details", None)
        if recorded:
            return dict(recorded)
        if isinstance(current, httpx.HTTPStatusError):
            return dict(
                current.response.extensions.get("paperseek_retry")
                or response_details(current.response)
            )
        status = getattr(current, "status", None)
        if status is not None:
            return {"http_status": status}
        current = current.__cause__
    return {}


def retry_delay(
    response: httpx.Response | None, attempt: int, *, base: float = 1.0
) -> float:
    if response is not None:
        value = response.headers.get("Retry-After", "").strip()
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    when = parsedate_to_datetime(value)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = (when - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = float("nan")
            if math.isfinite(delay):
                return max(0.0, delay)
    delay = min(16.0, base * 2 ** (attempt - 1))
    return delay + random.uniform(0, delay / 4)


async def cancellable_request[T](
    send: Callable[[], Awaitable[T]], cancellation: CancellationToken
) -> T:
    cancellation.raise_if_cancelled()
    waiting = asyncio.ensure_future(send())
    cancelled = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {waiting, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled in done:
            cancellation.raise_if_cancelled()
            raise asyncio.CancelledError
        result = await waiting
        cancellation.raise_if_cancelled()
        return result
    finally:
        for task in (waiting, cancelled):
            if not task.done():
                task.cancel()
        await asyncio.gather(waiting, cancelled, return_exceptions=True)


async def wait_for_retry(
    seconds: float,
    cancellation: CancellationToken,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    await cancellable_request(lambda: sleep(seconds), cancellation)


async def request_with_retries(
    send: Callable[[int], Awaitable[httpx.Response]],
    *,
    cancellation: CancellationToken,
    attempts: int = MODEL_ATTEMPTS,
    retry_statuses: frozenset[int] = TRANSIENT_STATUSES,
    retry_transport: bool = True,
    retry_auth: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    base_delay: float = 1.0,
) -> tuple[httpx.Response, int]:
    waited = 0.0
    for attempt in range(1, max(1, attempts) + 1):
        cancellation.raise_if_cancelled()
        response = None
        error = None
        try:
            response = await send(attempt)
        except httpx.RequestError as exc:
            error = exc
            retryable = retry_transport
            details: dict[str, object] = {"error_type": type(exc).__name__}
        else:
            details = response_details(response)
            retryable = (
                response.status_code in retry_statuses
                and details.get("provider_error_code") not in _QUOTA_CODES
            )
            if response.status_code == 401 and retry_auth is not None:
                retryable = retry_auth()
        cancellation.raise_if_cancelled()
        details.update(attempts=attempt, retryable=retryable)
        if error is not None:
            setattr(error, "retry_details", details)
        else:
            assert response is not None
            response.extensions["paperseek_retry"] = details
        if not retryable or attempt >= attempts:
            if error is not None:
                raise error
            assert response is not None
            return response, attempt
        delay = (
            0.0
            if response is not None and response.status_code == 401
            else retry_delay(response, attempt, base=base_delay)
        )
        if waited + delay > MAX_RETRY_WAIT:
            details["retry_wait_budget_exhausted"] = True
            if error is not None:
                raise error
            assert response is not None
            return response, attempt
        _logger.warning(
            "upstream_request_retry %s",
            {**details, "next_attempt": attempt + 1, "delay_seconds": round(delay, 3)},
        )
        await wait_for_retry(delay, cancellation, sleep or asyncio.sleep)
        waited += delay
    raise AssertionError("Retry loop exhausted without an outcome")
