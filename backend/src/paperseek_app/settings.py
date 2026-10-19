"""Shared saved-connection contracts and validation, independent of storage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from paperseek_app.configuration import (
    DECISION_ROUTE_DEFAULTS,
    EMBEDDING_ROUTE_DEFAULTS,
    LLM_ROUTE_DEFAULTS,
    RERANKER_ROUTE_DEFAULTS,
)

ConnectionKind = Literal["llm", "embedding", "reranker", "decision"]
PreRankingMode = Literal["local_fusion", "embedding_rerank", "decision"]
ProxyScope = Literal["models", "ip_limited_sources", "other_sources"]
PROXY_SCOPE_DEFAULT: tuple[ProxyScope, ...] = ("models", "ip_limited_sources", "other_sources")
LLM_API_TYPES = ("openai_chat", "openai_responses", "anthropic_messages")
SOURCE_OPTIONS = {
    "openalex": ("openalex_email",),
    "crossref": ("crossref_email",),
    "pubmed": ("pubmed_tool", "pubmed_email"),
    "wos": ("wos_db",),
}


class SettingsError(ValueError):
    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: ConnectionKind
    provider: str = Field(min_length=1, max_length=64)
    api_type: str = Field(default="", max_length=64)
    base_url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=256)
    # None keeps the stored key on update; an empty string clears it.
    api_key: str | None = Field(default=None, max_length=4096, repr=False)


class ConnectionTestInput(ConnectionInput):
    id: str = Field(default="", max_length=64)


class DefaultsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_llm: str | None = Field(default=None, max_length=64)
    review_llm: str | None = Field(default=None, max_length=64)
    pre_ranking: PreRankingMode | None = None
    embedding: str | None = Field(default=None, max_length=64)
    reranker: str | None = Field(default=None, max_length=64)
    decision: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=64)


class SourceSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    options: dict[str, str] | None = None


class ProxySettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # One proxy URL per line. None keeps the saved addresses; an empty string removes them.
    urls: str | None = Field(default=None, max_length=8192, repr=False)
    scope: list[ProxyScope] = Field(default_factory=lambda: list(PROXY_SCOPE_DEFAULT), max_length=3)


class TestOutput(BaseModel):
    status: Literal["untested", "ok", "failed"]
    latency_ms: int | None = None
    code: str = ""
    tested_at: str | None = None


class ProbeOutput(BaseModel):
    ok: bool
    latency_ms: int | None = None
    code: str = ""


class ConnectionOutput(BaseModel):
    id: str
    kind: ConnectionKind
    provider: str
    api_type: str
    base_url: str
    model: str
    has_key: bool
    # The key's last four characters, so a saved key can be recognised without revealing it.
    key_hint: str = ""
    requires_key: bool
    test: TestOutput
    created_at: str
    updated_at: str


class SourceSettingOutput(BaseModel):
    id: str
    has_key: bool
    key_origin: Literal["", "settings", "environment"]
    key_hint: str = ""
    usable: bool
    options: dict[str, str]
    test: TestOutput


class DefaultsOutput(BaseModel):
    search_llm: str
    review_llm: str
    pre_ranking: PreRankingMode
    embedding: str
    reranker: str
    decision: str
    source: str


class ProxySettingsOutput(BaseModel):
    origin: Literal["", "settings", "environment"]
    # host:port of each saved proxy; user names and passwords are never returned.
    hosts: list[str]
    scope: list[ProxyScope]


class WorkspaceSettingsOutput(BaseModel):
    connections: list[ConnectionOutput]
    sources: list[SourceSettingOutput]
    defaults: DefaultsOutput
    proxy: ProxySettingsOutput | None = None


class SecretsClearOutput(BaseModel):
    connections: int
    source_keys: int
    proxies: int = 0



def _provider_table(kind: str) -> Mapping[str, tuple[str, ...]]:
    tables: dict[str, Mapping[str, tuple[str, ...]]] = {
        "llm": LLM_ROUTE_DEFAULTS,
        "embedding": {key: value for key, value in EMBEDDING_ROUTE_DEFAULTS.items() if key != "local"},
        "reranker": {key: value for key, value in RERANKER_ROUTE_DEFAULTS.items() if key},
        "decision": DECISION_ROUTE_DEFAULTS,
    }
    return tables[kind]


def normalize_route(kind: str, provider: str, api_type: str, base_url: str, model: str) -> dict[str, str]:
    """Fill provider presets and reject endpoints a run would refuse."""
    provider = provider.strip().lower()
    table = _provider_table(kind)
    if provider not in table:
        raise SettingsError("connection_provider_unsupported")
    if kind == "llm":
        default_api_type, default_base_url, default_model = table[provider]
        api_type = api_type.strip().lower() or default_api_type
        if api_type not in LLM_API_TYPES:
            raise SettingsError("connection_api_type_unsupported")
    else:
        default_model, default_base_url = table[provider]
        api_type = ""
    route = {
        "provider": provider,
        "api_type": api_type,
        "base_url": (base_url.strip() or default_base_url).rstrip("/"),
        "model": model.strip() or default_model,
    }
    url = urlsplit(route["base_url"])
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username
            or url.password or url.query or url.fragment):
        raise SettingsError("connection_base_url_invalid")
    if not route["model"]:
        raise SettingsError("connection_model_required")
    return route


def key_hint(key: str) -> str:
    """Last four characters of a key long enough not to reveal it."""
    return key[-4:] if len(key) >= 8 else ""


def requires_key(kind: str, provider: str) -> bool:
    return not (kind == "llm" and provider == "ollama")
