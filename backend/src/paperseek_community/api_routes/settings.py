from __future__ import annotations

from fastapi import FastAPI, Query, Response

from paperseek_app.execution import run_blocking
from paperseek_app.settings import (
    ConnectionInput,
    ConnectionOutput,
    ConnectionTestInput,
    DefaultsInput,
    DefaultsOutput,
    ProbeOutput,
    ProxySettingsInput,
    ProxySettingsOutput,
    SecretsClearOutput,
    SettingsError,
    SourceSettingsInput,
    WorkspaceSettingsOutput,
    normalize_route,
)
from paperseek_app.settings_probes import (
    probe_connection,
    probe_source,
)
from paperseek_app.source_catalog import SOURCE_BY_ID
from paperseek_community.api_dependencies import ApiError
from paperseek_community.composition import build_source
from paperseek_community.model_transport import create_http_client
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import (
    WorkspaceSettingsStore,
)


def settings_error(exc: SettingsError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.code)


def register_settings_routes(
    app: FastAPI,
    *,
    settings: CommunityDeploymentSettings,
    store: WorkspaceSettingsStore,
) -> None:
    async def call(function, *args):
        try:
            return await run_blocking(function, *args)
        except SettingsError as exc:
            raise settings_error(exc) from exc

    @app.get("/api/v1/settings", response_model=WorkspaceSettingsOutput)
    async def get_settings() -> dict:
        return await call(store.snapshot, settings)

    @app.post("/api/v1/settings/connections", status_code=201, response_model=ConnectionOutput)
    async def create_connection(request: ConnectionInput) -> dict:
        return await call(store.create_connection, request)

    @app.put("/api/v1/settings/connections/{connection_id}", response_model=ConnectionOutput)
    async def update_connection(connection_id: str, request: ConnectionInput) -> dict:
        return await call(store.update_connection, connection_id, request)

    @app.delete("/api/v1/settings/connections/{connection_id}", status_code=204)
    async def delete_connection(connection_id: str, replacement: str = Query(default="", max_length=64)) -> Response:
        await call(store.delete_connection, connection_id, replacement)
        return Response(status_code=204)

    @app.delete("/api/v1/settings/secrets", response_model=SecretsClearOutput)
    async def clear_secrets() -> dict:
        """Remove every saved model connection and data-source key; submitted runs keep their own copies."""
        return await call(store.clear_secrets)

    @app.put("/api/v1/settings/proxy", response_model=ProxySettingsOutput)
    async def save_proxy(request: ProxySettingsInput) -> dict:
        """Save the network proxy pool; omitted addresses keep the saved ones."""
        return await call(store.save_proxy, request, settings)

    @app.post("/api/v1/settings/connections/test", response_model=ProbeOutput)
    async def test_draft_connection(request: ConnectionTestInput) -> dict:
        """Test unsaved sheet values; an omitted key reuses the saved one for the same endpoint."""
        try:
            route = normalize_route(request.kind, request.provider, request.api_type, request.base_url, request.model)
        except SettingsError as exc:
            raise settings_error(exc) from exc
        key = (request.api_key or "").strip()
        if request.api_key is None and request.id:
            saved, saved_key = await call(store.connection_secret, request.id, request.kind)
            if saved["provider"] == route["provider"] and saved["base_url"] == route["base_url"]:
                key = saved_key
        proxy = await call(store.proxy_policy, settings)
        return await probe_connection(request.kind, route, key, client_factory=lambda routes: create_http_client(routes, proxy))

    @app.post("/api/v1/settings/connections/{connection_id}/test", response_model=ProbeOutput)
    async def test_connection(connection_id: str) -> dict:
        route, key = await call(store.connection_secret, connection_id)
        connection = await call(store.connection, connection_id)
        proxy = await call(store.proxy_policy, settings)
        result = await probe_connection(str(connection["kind"]), route, key,
                                        client_factory=lambda routes: create_http_client(routes, proxy))
        await call(store.record_connection_test, connection_id, result)
        return result

    @app.put("/api/v1/settings/defaults", response_model=DefaultsOutput)
    async def save_defaults(request: DefaultsInput) -> dict:
        return await call(store.save_defaults, request)

    @app.put("/api/v1/settings/sources/{source_id}", status_code=204)
    async def save_source(source_id: str, request: SourceSettingsInput) -> Response:
        await call(store.save_source, source_id, request)
        return Response(status_code=204)

    @app.post("/api/v1/settings/sources/{source_id}/test", response_model=ProbeOutput)
    async def test_source(source_id: str) -> dict:
        if source_id not in SOURCE_BY_ID:
            raise ApiError(404, "source_not_found", "source_not_found")
        key, options = await call(store.source_secret, source_id)
        result = await probe_source(lambda transport: build_source(
            source_id, transport, key, wos_db=options.get("wos_db") or settings.wos_db,
            contacts={name: value for name, value in options.items() if name != "wos_db"}, settings=settings,
        ), proxy=await call(store.proxy_policy, settings))
        await call(store.record_source_test, source_id, result)
        return result
