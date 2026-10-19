"""Shared Community transport policy for source and writing model clients."""
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx

from paperseek_core.sources.proxy import ProxyPolicy, ProxyPoolTransport

TIMEOUT = httpx.Timeout(connect=10, read=180, write=30, pool=10)


def create_http_client(routes: list[dict], proxy: ProxyPolicy | None = None) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    if proxy is not None and proxy.enabled:
        # The pool transport applies the system proxy settings itself to traffic outside its scope.
        return httpx.AsyncClient(timeout=TIMEOUT, limits=limits, trust_env=False,
                                 transport=ProxyPoolTransport(proxy, limits=limits))
    mounts = {}
    for route in routes:
        url = urlsplit(route.get("base_url") or "")
        try:
            loopback = url.hostname == "localhost" or ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if loopback:
            mounts[f"{url.scheme}://{url.netloc}"] = httpx.AsyncHTTPTransport(limits=limits)
    return httpx.AsyncClient(timeout=TIMEOUT, limits=limits, mounts=mounts)
