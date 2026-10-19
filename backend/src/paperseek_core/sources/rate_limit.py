"""Process-local source pacing and credential-scoped cooldowns, including retries."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import time
from urllib.parse import parse_qsl, urlsplit
from weakref import WeakKeyDictionary
from typing import Any

import httpx

from paperseek_core.http_retry import retry_delay
from paperseek_core.sources.contracts import SourceError


@dataclass
class _Window:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_at: float = 0.0
    cooldowns: dict[str, float] = field(default_factory=dict)


_windows: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str, bytes], _Window]] = WeakKeyDictionary()
PACED_SOURCES = frozenset({'arxiv', 'pubmed', 'semanticscholar', 'openalex', 'crossref', 'wos', 'googlescholar', 'paperhub'})


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
        windows = _windows.setdefault(asyncio.get_running_loop(), {})
        if len(windows) >= 1024:
            now = time.monotonic()
            for old_key, old in list(windows.items()):
                if not old.lock.locked() and old.next_at <= now and all(t <= now for t in old.cooldowns.values()):
                    del windows[old_key]
        self.window = windows.setdefault(key, _Window())
        self.scope = ('bulk' if source == 'semanticscholar' and parts.path.endswith('/bulk') else
                      'semantic' if source == 'openalex' and 'search.semantic' in parameters else 'standard')
        defaults = {'arxiv': 3.1, 'semanticscholar': 1.2, 'pubmed': 0.11 if credential else 0.34}
        self.interval = max(0, defaults.get(source, 0) if interval is None else interval)

    @asynccontextmanager
    async def sending(self, attempt: int, wait: Callable[[float], Awaitable[None]]) -> AsyncIterator[None]:
        window = self.window
        await window.lock.acquire()
        locked = True
        try:
            remaining = window.cooldowns.get(self.scope, 0) - time.monotonic()
            if attempt == 1 and remaining > 0:
                raise SourceError(self.source, 'Source is cooling down after a rate limit.', status=429,
                    retry_details={'source_cooldown': True, 'retry_after_seconds': round(remaining, 3)})
            delay = max(window.next_at, window.cooldowns.get(self.scope, 0)) - time.monotonic()
            if delay > 0:
                await wait(delay)
            window.next_at = time.monotonic() + self.interval
            # arXiv explicitly requires a single connection as well as pacing.
            if self.source != 'arxiv':
                window.lock.release()
                locked = False
            yield
        finally:
            if locked:
                window.lock.release()

    def observe(self, response: httpx.Response, attempt: int) -> None:
        if response.status_code == 429:
            until = time.monotonic() + retry_delay(response, attempt, base=5.0)
            self.window.cooldowns[self.scope] = max(until, self.window.cooldowns.get(self.scope, 0))


def source_rate_limit(source: str, url: str, params: Mapping[str, Any] | None,
                      headers: Mapping[str, str], interval: float | None = None) -> SourceRateLimit | None:
    return SourceRateLimit(source, url, params, headers, interval) if source in PACED_SOURCES else None
