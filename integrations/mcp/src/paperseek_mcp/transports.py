from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hmac
import time

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import RequestBodyLimitMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from paperseek_mcp.server import create_server
from paperseek_mcp.tools import PaperSeekApplication


MAX_HTTP_REQUEST_BODY_BYTES = 1024 * 1024
MAX_CONCURRENT_HTTP_REQUESTS = 16
MAX_HTTP_REQUESTS_PER_MINUTE = 120


@dataclass(frozen=True, slots=True)
class TransportConfig:
    transport: str
    database: str
    host: str
    port: int
    path: str
    sse_path: str
    message_path: str
    service_token: str
    public_url: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, token: str, resource: str) -> None:
        self._token = token
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8")):
            return None
        return AccessToken(
            token=token,
            client_id="paperseek-mcp-service",
            scopes=["paperseek:mcp"],
            resource=self._resource,
        )


class BoundedHTTPApplication:
    """Fixed HTTP body, concurrency, and rolling request-rate boundaries."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_size: int = MAX_HTTP_REQUEST_BODY_BYTES,
        max_concurrency: int = MAX_CONCURRENT_HTTP_REQUESTS,
        requests_per_minute: int = MAX_HTTP_REQUESTS_PER_MINUTE,
    ) -> None:
        self.max_body_size = max_body_size
        self.max_concurrency = max_concurrency
        self.requests_per_minute = requests_per_minute
        self._app = app
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._body_limited = RequestBodyLimitMiddleware(self._bounded, max_body_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._body_limited(scope, receive, send)

    async def _bounded(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        now = time.monotonic()
        async with self._rate_lock:
            cutoff = now - 60.0
            while self._request_times and self._request_times[0] <= cutoff:
                self._request_times.popleft()
            if len(self._request_times) >= self.requests_per_minute:
                response = Response(
                    "Request rate limit exceeded",
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
                await response(scope, receive, send)
                return
            self._request_times.append(now)
        if self._semaphore.locked():
            response = Response("Concurrent request limit exceeded", status_code=429)
            await response(scope, receive, send)
            return
        async with self._semaphore:
            await self._app(scope, receive, send)


class PrivateTokenApplication:
    """Protect private HTTP with a pre-shared token, without advertising OAuth."""

    def __init__(self, app: ASGIApp, verifier: StaticTokenVerifier) -> None:
        self._app = app
        self._authenticated = AuthenticationMiddleware(
            RequireAuthMiddleware(app, required_scopes=["paperseek:mcp"]),
            backend=BearerAuthBackend(verifier),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._authenticated(scope, receive, send)
        else:
            await self._app(scope, receive, send)


@dataclass(slots=True)
class MCPRuntime:
    server: MCPServer
    transport: str
    host: str
    port: int
    path: str
    sse_path: str
    message_path: str
    security: TransportSecuritySettings | None
    http_application: BoundedHTTPApplication | None
    token_verifier: StaticTokenVerifier | None

    async def serve(self) -> None:
        if self.transport == "stdio":
            await self.server.run_stdio_async()
            return
        import uvicorn

        if self.http_application is None:
            raise RuntimeError("The HTTP transport was not assembled.")
        server = uvicorn.Server(
            uvicorn.Config(
                self.http_application,
                host=self.host,
                port=self.port,
                log_level=self.server.settings.log_level.lower(),
            )
        )
        await server.serve()


def create_runtime(config: TransportConfig, application: PaperSeekApplication) -> MCPRuntime:
    token_verifier = None
    if config.transport != "stdio" and config.service_token:
        resource = config.public_url or f"http://{config.host}:{config.port}{config.path}"
        token_verifier = StaticTokenVerifier(config.service_token, resource)
    server = create_server(application)
    if config.transport == "stdio":
        return MCPRuntime(
            server=server,
            transport=config.transport,
            host=config.host,
            port=config.port,
            path=config.path,
            sse_path=config.sse_path,
            message_path=config.message_path,
            security=None,
            http_application=None,
            token_verifier=token_verifier,
        )

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=list(config.allowed_origins),
    )
    if config.transport == "streamable-http":
        http_app: ASGIApp = server.streamable_http_app(
            streamable_http_path=config.path,
            max_request_body_size=MAX_HTTP_REQUEST_BODY_BYTES,
            transport_security=security,
            host=config.host,
        )
    elif config.transport == "sse-legacy":
        http_app = server.sse_app(
            sse_path=config.sse_path,
            message_path=config.message_path,
            transport_security=security,
            host=config.host,
        )
    else:
        raise ValueError("The selected MCP transport is not supported.")
    if token_verifier is not None:
        http_app = PrivateTokenApplication(http_app, token_verifier)
    bounded = BoundedHTTPApplication(http_app)
    return MCPRuntime(
        server=server,
        transport=config.transport,
        host=config.host,
        port=config.port,
        path=config.path,
        sse_path=config.sse_path,
        message_path=config.message_path,
        security=security,
        http_application=bounded,
        token_verifier=token_verifier,
    )


__all__ = [
    "BoundedHTTPApplication",
    "MAX_CONCURRENT_HTTP_REQUESTS",
    "MAX_HTTP_REQUEST_BODY_BYTES",
    "MAX_HTTP_REQUESTS_PER_MINUTE",
    "MCPRuntime",
    "StaticTokenVerifier",
    "TransportConfig",
    "create_runtime",
]
