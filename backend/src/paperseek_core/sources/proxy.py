"""Deployment-owned HTTP proxy pool with a configurable scope.

The scope names three kinds of traffic:
- ``models``: language, embedding, rerank and decision model requests;
- ``ip_limited_sources``: sources that throttle by network address, so another
  address helps: arXiv, Crossref, PaperHub, and PubMed or OpenAlex without a key;
- ``other_sources``: sources that throttle by key or by a shared global pool,
  where a proxy only helps reachability.

Requests outside the scope keep the system proxy settings (HTTP(S)_PROXY, NO_PROXY).
Loopback and private model endpoints are never sent through the pool.
"""

from __future__ import annotations

from collections.abc import Iterable
import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
import json
import random
from urllib.parse import parse_qsl, urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx

from paperseek_core.sources.contracts import SourceError
from paperseek_core.sources.rate_limit import PACED_SOURCES

PROXY_SCOPES = ("models", "ip_limited_sources", "other_sources")
_ADDRESS_LIMITED = frozenset({"arxiv", "crossref", "paperhub"})
_ADDRESS_LIMITED_WITHOUT_KEY = frozenset({"pubmed", "openalex"})
_CREDENTIAL_PARAMS = frozenset({"api_key", "apikey"})
_CREDENTIAL_HEADERS = ("x-api-key", "x-apikey", "authorization")


def parse_proxy_urls(value: object) -> tuple[str, ...]:
    """Validate without ever including a proxy URL or password in an error."""
    try:
        if value is None or isinstance(value, str) and not value.strip():
            return ()
        if isinstance(value, str):
            text = value.strip()
            entries = json.loads(text) if text.startswith("[") else text.replace(",", "\n").splitlines()
        else:
            entries = value
        entries = [entries] if isinstance(entries, str) else entries
        if not isinstance(entries, (list, tuple)):
            raise ValueError
        urls = []
        for entry in entries:
            if not isinstance(entry, str):
                raise ValueError
            url = entry.strip()
            if not url:
                continue
            parts = urlsplit(url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.path not in {"", "/"} or parts.query or parts.fragment or
                any(char.isspace() for char in url)):
                raise ValueError
            if parts.port is not None and not 1 <= parts.port <= 65535:
                raise ValueError
            urls.append(url)
        return tuple(dict.fromkeys(urls))
    except (ValueError, TypeError):
        raise ValueError("Proxy addresses must be HTTP(S) proxy URLs, one per line.") from None


def parse_proxy_scope(value: object, default: Iterable[str] = PROXY_SCOPES) -> frozenset[str]:
    if value is None or isinstance(value, str) and not value.strip():
        return frozenset(default)
    items = value.replace(",", " ").split() if isinstance(value, str) else value
    if not isinstance(items, (list, tuple, set, frozenset)) or not all(isinstance(item, str) for item in items):
        raise ValueError("The proxy scope must list models, ip_limited_sources or other_sources.")
    scope = frozenset(item.strip() for item in items if item.strip())
    if scope - set(PROXY_SCOPES):
        raise ValueError("The proxy scope must list models, ip_limited_sources or other_sources.")
    return scope


@dataclass(frozen=True)
class ProxyPolicy:
    urls: tuple[str, ...] = ()
    scope: frozenset[str] = frozenset(PROXY_SCOPES)

    @property
    def enabled(self) -> bool:
        return bool(self.urls and self.scope)


def _local(host: str | None) -> bool:
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host.strip("[]"))
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _has_credential(request: httpx.Request) -> bool:
    if any(key.lower() in _CREDENTIAL_PARAMS and value for key, value in parse_qsl(request.url.query.decode())):
        return True
    return any(request.headers.get(name) for name in _CREDENTIAL_HEADERS)


def traffic_kind(request: httpx.Request) -> str:
    source = request.extensions.get("paperseek_source")
    if source not in PACED_SOURCES:
        return "models"
    if source in _ADDRESS_LIMITED or source in _ADDRESS_LIMITED_WITHOUT_KEY and not _has_credential(request):
        return "ip_limited_sources"
    return "other_sources"


class ProxyPoolTransport(httpx.AsyncBaseTransport):
    """Rotate the pool for in-scope traffic; retries, pacing and cooldowns stay upstream."""

    def __init__(self, policy: ProxyPolicy, *, limits: httpx.Limits | None = None, system_proxy: bool = True) -> None:
        self._policy = policy
        self._limits = limits or httpx.Limits(max_connections=32, max_keepalive_connections=16)
        # Spread processes across the pool, then round-robin each HTTP attempt.
        self._next = random.randrange(len(policy.urls)) if policy.urls else 0
        self._direct = httpx.AsyncHTTPTransport(limits=self._limits)
        # A deployment that controls its own egress passes system_proxy=False.
        self._system = ({scheme: url for scheme, url in getproxies().items() if scheme in {"http", "https"}}
                        if system_proxy else {})
        self._transports: dict[str, httpx.AsyncHTTPTransport] = {}

    def _via(self, url: str) -> httpx.AsyncHTTPTransport:
        if url not in self._transports:
            self._transports[url] = httpx.AsyncHTTPTransport(proxy=url, limits=self._limits)
        return self._transports[url]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        kind = traffic_kind(request)
        if self._policy.urls and kind in self._policy.scope and not (kind == "models" and _local(host)):
            url = self._policy.urls[self._next % len(self._policy.urls)]
            self._next = (self._next + 1) % len(self._policy.urls)
            # No implicit direct fallback: a broken or unauthenticated proxy is an
            # explicit failure. HTTPX keeps proxy auth out of origin headers.
            try:
                return await self._via(url).handle_async_request(request)
            except httpx.ProxyError as error:
                # HTTPX raises for a rejected HTTPS CONNECT instead of returning a
                # response. Preserve the authentication status without its message.
                if str(error).split(" ", 1)[0] == "407" and kind != "models":
                    raise SourceError(str(request.extensions.get("paperseek_source")),
                                      "Source proxy authentication failed.", status=407) from None
                raise
        system = self._system.get(request.url.scheme)
        if system and not _local(host) and not proxy_bypass(host):
            return await self._via(system).handle_async_request(request)
        return await self._direct.handle_async_request(request)

    async def aclose(self) -> None:
        results = await asyncio.gather(self._direct.aclose(),
            *(transport.aclose() for transport in self._transports.values()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
