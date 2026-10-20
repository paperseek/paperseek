from __future__ import annotations

from paperseek_community.settings import CommunityDeploymentSettings


def settings() -> CommunityDeploymentSettings:
    return CommunityDeploymentSettings(
        llm_api_type="openai_responses",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-5.4-mini",
        llm_api_key="",
        openalex_api_key="",
        semantic_scholar_api_key="",
        pubmed_api_key="",
        wos_api_key="",
        google_scholar_api_key="",
        contact_email="",
    )
