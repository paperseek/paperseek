"""Deployment-owned HTTP proxy pools for explicitly tagged source requests."""

from collections.abc import Mapping
import asyncio
import json
import random
from urllib.parse import urlsplit

import httpx

from paperseek_core.sources.rate_limit import PACED_SOURCES
from paperseek_core.sources.contracts import SourceError


def parse_source_proxies(value: object) -> dict[str, tuple[str, ...]]:
    """Validate without ever including a proxy URL or password in an error."""
    try:
        if isinstance(value, str) and not value.strip():
            return {}
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict) or parsed.keys() - PACED_SOURCES:
            raise ValueError
        result = {}
        for source, entries in parsed.items():
            entries = [entries] if isinstance(entries, str) else entries
            if not isinstance(entries, (list, tuple)):
                raise ValueError
            urls = []
            for entry in entries:
                if not isinstance(entry, str) or not entry.strip():
                    raise ValueError
                url = entry.strip()
                parts = urlsplit(url)
                if (parts.scheme not in {"http", "https"} or not parts.hostname or
                    parts.path not in {"", "/"} or parts.query or parts.fragment or
                    any(char.isspace() for char in url)):
                    raise ValueError
                if parts.port is not None and not 1 <= parts.port <= 65535:
                    raise ValueError
                urls.append(url)
            if urls:
                result[source] = tuple(dict.fromkeys(urls))
        return result
    except (ValueError, TypeError):
        raise ValueError("PAPERSEEK_SOURCE_PROXIES must map supported source IDs to HTTP(S) proxy URLs or URL lists.") from None


class SourceProxyTransport(httpx.AsyncBaseTransport):
    """Rotate configured proxies; retries, pacing and cooldowns stay upstream."""

    def __init__(self, routes: Mapping[str, tuple[str, ...]]) -> None:
        self._routes = parse_source_proxies(dict(routes))
        # Spread new tasks across the pool, then round-robin each HTTP attempt.
        self._next = {source: random.randrange(len(urls)) for source, urls in self._routes.items()}
        self._direct = httpx.AsyncHTTPTransport(trust_env=False)
        self._proxies: dict[str, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        source = request.extensions.get("paperseek_source")
        urls = self._routes.get(source, ())
        if not urls:
            return await self._direct.handle_async_request(request)
        index = self._next[source]
        self._next[source] = (index + 1) % len(urls)
        url = urls[index]
        if url not in self._proxies:
            self._proxies[url] = httpx.AsyncHTTPTransport(proxy=url, trust_env=False)
        # No implicit direct fallback: a broken or unauthenticated proxy is an
        # explicit source failure. HTTPX keeps proxy auth out of origin headers.
        try:
            return await self._proxies[url].handle_async_request(request)
        except httpx.ProxyError as error:
            # HTTPX raises for a rejected HTTPS CONNECT instead of returning a
            # response. Preserve the authentication status without its message.
            if str(error).split(" ", 1)[0] == "407":
                raise SourceError(source, "Source proxy authentication failed.", status=407) from None
            raise

    async def aclose(self) -> None:
        results = await asyncio.gather(self._direct.aclose(),
            *(transport.aclose() for transport in self._proxies.values()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
