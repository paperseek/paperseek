"""Process-wide source pacing and credential-scoped cooldowns, including retries.

Runs may execute on separate threads with their own event loops, so the shared
state is guarded by a thread lock and every wait happens on the caller's loop.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import threading
import time
from urllib.parse import parse_qsl, urlsplit
from typing import Any

import httpx

from paperseek_core.http_retry import retry_delay
from paperseek_core.sources.contracts import SourceError


@dataclass
class _Window:
    next_at: float = 0.0
    busy: bool = False
    cooldowns: dict[str, float] = field(default_factory=dict)


_lock = threading.Lock()
_windows: dict[tuple[str, str, bytes], _Window] = {}
PACED_SOURCES = frozenset({'arxiv', 'pubmed', 'semanticscholar', 'openalex', 'crossref', 'wos', 'googlescholar', 'paperhub'})
# arXiv also requires a single connection; another request waits for the current one in short steps.
_BUSY_POLL = 0.05


def _interval(source: str, credential: str) -> float:
    return {'arxiv': 3.1, 'semanticscholar': 1.2, 'pubmed': 0.11 if credential else 0.34}.get(source, 0.0)


def rate_limit_backoff(source: str) -> float:
    """Base wait after a 429: at least two pacing intervals, never under two seconds."""
    return max(2.0, 2 * _interval(source, ''))


class SourceRateLimit:
    def __init__(self, source: str, url: str, params: Mapping[str, Any] | None,
                 headers: Mapping[str, str], interval: float | None = None) -> None:
        self.source = source
        parts = urlsplit(url)
        parameters = dict(parse_qsl(parts.query)) | dict(params or {})
        lower_headers = {key.lower(): value for key, value in headers.items()}
        credential = str(parameters.get('api_key') or parameters.get('apikey') or
                         lower_headers.get('x-api-key') or lower_headers.get('x-apikey') or
                         lower_headers.get('authorization') or '')
        # Neither the registry nor diagnostics retain plaintext credentials.
        key = (source, parts.netloc, hashlib.sha256(credential.encode()).digest())
        with _lock:
            if len(_windows) >= 1024:
                now = time.monotonic()
                for old_key, old in list(_windows.items()):
                    if not old.busy and old.next_at <= now and all(t <= now for t in old.cooldowns.values()):
                        del _windows[old_key]
            self.window = _windows.setdefault(key, _Window())
        self.scope = ('bulk' if source == 'semanticscholar' and parts.path.endswith('/bulk') else
                      'semantic' if source == 'openalex' and 'search.semantic' in parameters else 'standard')
        self.interval = max(0, _interval(source, credential) if interval is None else interval)
        self.exclusive = source == 'arxiv'

    def _reserve(self, attempt: int) -> float | None:
        """Claim the next send slot; returns the wait, or None while another arXiv request runs."""
        window = self.window
        with _lock:
            now = time.monotonic()
            cooldown = window.cooldowns.get(self.scope, 0)
            if attempt == 1 and cooldown > now:
                raise SourceError(self.source, 'Source is cooling down after a rate limit.', status=429,
                    retry_details={'source_cooldown': True, 'retry_after_seconds': round(cooldown - now, 3)})
            if self.exclusive:
                if window.busy:
                    return None
                window.busy = True
            start = max(now, window.next_at, cooldown)
            window.next_at = start + self.interval
            return start - now

    @asynccontextmanager
    async def sending(self, attempt: int, wait: Callable[[float], Awaitable[None]]) -> AsyncIterator[None]:
        while (delay := self._reserve(attempt)) is None:
            await wait(_BUSY_POLL)
            # Yield even when an injected wait returns at once, so the running request can finish.
            await asyncio.sleep(0)
        try:
            if delay > 0:
                await wait(delay)
            yield
        finally:
            if self.exclusive:
                with _lock:
                    self.window.busy = False
                    # Pace from the end of a slow response as well as from its start.
                    self.window.next_at = max(self.window.next_at, time.monotonic() + self.interval)

    def observe(self, response: httpx.Response, attempt: int) -> None:
        if response.status_code == 429:
            until = time.monotonic() + retry_delay(response, attempt, base=rate_limit_backoff(self.source))
            with _lock:
                self.window.cooldowns[self.scope] = max(until, self.window.cooldowns.get(self.scope, 0))


def reset_source_rate_limits() -> None:
    """Forget all pacing and cooldowns; for tests that need a fresh process state."""
    with _lock:
        _windows.clear()


def source_rate_limit(source: str, url: str, params: Mapping[str, Any] | None,
                      headers: Mapping[str, str], interval: float | None = None) -> SourceRateLimit | None:
    return SourceRateLimit(source, url, params, headers, interval) if source in PACED_SOURCES else None
