"""Shared Community transport policy for source and writing model clients."""
import os
from urllib.parse import urlsplit

import httpx

from paperseek_core.model_http import is_local_model_endpoint, is_loopback_endpoint

DEFAULT_READ_TIMEOUT_SECONDS = 180.0
# A locally served model may load on first use and generate far slower than a hosted API.
LOCAL_READ_TIMEOUT_SECONDS = 1800.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def model_timeout(routes: list[dict]) -> httpx.Timeout:
    """Local routes pay a model load and slow generation, so they need a longer read timeout."""
    local = any(
        is_local_model_endpoint(str(route.get("provider") or ""), str(route.get("base_url") or ""))
        for route in routes
    )
    return httpx.Timeout(
        connect=_env_seconds("PAPERSEEK_LLM_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS),
        read=_env_seconds(
            "PAPERSEEK_LLM_TIMEOUT_SECONDS",
            LOCAL_READ_TIMEOUT_SECONDS if local else DEFAULT_READ_TIMEOUT_SECONDS,
        ),
        write=30.0,
        pool=10.0,
    )


def create_http_client(routes: list[dict]) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    mounts = {}
    for route in routes:
        base_url = str(route.get("base_url") or "")
        if is_loopback_endpoint(base_url):
            url = urlsplit(base_url)
            mounts[f"{url.scheme}://{url.netloc}"] = httpx.AsyncHTTPTransport(limits=limits)
    return httpx.AsyncClient(timeout=model_timeout(routes), limits=limits, mounts=mounts)
