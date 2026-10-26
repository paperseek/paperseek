from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from paperseek_app.configuration import normalize_wos_db
from paperseek_core.sources.proxy import ProxyPolicy, parse_proxy_scope, parse_proxy_urls


class CommunityDeploymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    llm_api_type: Literal["openai_chat", "openai_responses", "anthropic_messages"]
    llm_base_url: str
    llm_model: str = Field(min_length=1)
    llm_api_key: str = Field(repr=False)
    openalex_api_key: str = Field(repr=False)
    semantic_scholar_api_key: str = Field(repr=False)
    pubmed_api_key: str = Field(repr=False)
    wos_api_key: str = Field(repr=False)
    google_scholar_api_key: str = Field(repr=False)
    wanfang_api_key: str = Field(default="", repr=False)
    contact_email: str
    wos_db: str = "WOS"
    proxy: ProxyPolicy = Field(default_factory=ProxyPolicy, repr=False, exclude=True)
    # Searches and reviews that run at the same time; 0 removes the limit.
    max_concurrent_runs: int = Field(default=4, ge=0)

    @field_validator("llm_base_url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("The model base URL must be an absolute HTTP(S) URL.")
        return value

    @classmethod
    def from_environment(cls) -> "CommunityDeploymentSettings":
        api_type = os.environ.get("PAPERSEEK_LLM_API_TYPE", "openai_responses").strip()
        default_base = (
            "https://api.anthropic.com"
            if api_type == "anthropic_messages"
            else "https://api.openai.com/v1"
        )
        return cls(
            llm_api_type=api_type,
            llm_base_url=os.environ.get("PAPERSEEK_LLM_BASE_URL", default_base).strip(),
            llm_model=os.environ.get("PAPERSEEK_LLM_MODEL", "gpt-5.6-luna").strip(),
            llm_api_key=os.environ.get("PAPERSEEK_LLM_API_KEY", "").strip(),
            openalex_api_key=os.environ.get("PAPERSEEK_OPENALEX_API_KEY", "").strip(),
            semantic_scholar_api_key=os.environ.get("PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY", "").strip(),
            pubmed_api_key=os.environ.get("PAPERSEEK_PUBMED_API_KEY", "").strip(),
            wos_api_key=os.environ.get("PAPERSEEK_WOS_API_KEY", "").strip(),
            google_scholar_api_key=os.environ.get("PAPERSEEK_GOOGLE_SCHOLAR_API_KEY", "").strip(),
            wanfang_api_key=os.environ.get("PAPERSEEK_WANFANG_API_KEY", "").strip(),
            contact_email=os.environ.get("PAPERSEEK_CONTACT_EMAIL", "").strip(),
            wos_db=normalize_wos_db(os.environ.get("PAPERSEEK_WOS_DB")),
            max_concurrent_runs=os.environ.get("PAPERSEEK_MAX_CONCURRENT_RUNS", "").strip() or 4,
            proxy=ProxyPolicy(parse_proxy_urls(os.environ.get("PAPERSEEK_PROXY_URLS")),
                              parse_proxy_scope(os.environ.get("PAPERSEEK_PROXY_SCOPE"))),
        )
