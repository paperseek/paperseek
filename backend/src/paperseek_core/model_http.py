"""Shared JSON model transport; wire formats stay in their protocol adapters."""

from __future__ import annotations

from collections.abc import Callable
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx

from paperseek_core.http_retry import MODEL_ATTEMPTS, cancellable_request, request_with_retries
from paperseek_core.key_pool import RequestKeys
from paperseek_core.ports import CancellationToken

# Providers that always mean a model served on the user's own machine.
LOCAL_MODEL_PROVIDERS = frozenset({"ollama", "custom", "local"})


def is_loopback_endpoint(base_url: str) -> bool:
    """A loopback endpoint never leaves the machine, so it needs no egress proxy."""
    try:
        hostname = urlsplit(str(base_url or "")).hostname or ""
    except ValueError:
        return False
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def is_local_model_provider(provider: str) -> bool:
    return str(provider or "").strip().lower() in LOCAL_MODEL_PROVIDERS


def is_local_model_endpoint(provider: str, base_url: str) -> bool:
    """Local runtimes load a model on first use and answer slower than hosted APIs."""
    return is_local_model_provider(provider) or is_loopback_endpoint(base_url)


def bearer_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def post_model_json(
    client: httpx.AsyncClient,
    cancellation: CancellationToken,
    url: str,
    body: dict,
    keys: tuple[str, ...],
    headers: Callable[[str], dict[str, str]] = bearer_headers,
    *,
    attempts: int = MODEL_ATTEMPTS,
    timeout: httpx.Timeout | None = None,
) -> tuple[httpx.Response, str]:
    rotation = RequestKeys(keys)

    async def send(_attempt: int) -> httpx.Response:
        request_headers = headers(rotation.next_key())
        if timeout is None:
            return await cancellable_request(
                lambda: client.post(url, json=body, headers=request_headers),
                cancellation,
            )
        return await cancellable_request(
            lambda: client.post(url, json=body, headers=request_headers, timeout=timeout),
            cancellation,
        )

    response, _ = await request_with_retries(
        send,
        cancellation=cancellation,
        attempts=attempts,
        retry_auth=rotation.reject_current,
    )
    return response, rotation.current
