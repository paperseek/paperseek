from __future__ import annotations

from dataclasses import dataclass

from paperseek_core.disciplines import (
    list_source_filter_options,
    source_filter_label,
    source_filter_mode,
)


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    label: str
    api_key: str
    credential_input_label: str = ""
    credential_guidance_label: str = ""
    credential_guidance_url: str = ""
    status: str = "supported"
    default: bool = False
    citation_expansion: bool = False

    def descriptor(self) -> dict[str, object]:
        credential = None
        if self.api_key != "not_required":
            credential = {
                "input_label": self.credential_input_label,
                "guidance_label": self.credential_guidance_label,
                "guidance_url": self.credential_guidance_url,
            }
        return {
            "id": self.id,
            "label": self.label,
            "api_key": self.api_key,
            "status": self.status,
            "default": self.default,
            "credential": credential,
            "capabilities": {
                "citation_expansion": self.citation_expansion,
                "filter": {
                    "mode": source_filter_mode(self.id),
                    "label": source_filter_label(self.id),
                    "options": list_source_filter_options(self.id),
                },
            },
        }


SOURCES = (
    SourceDefinition(
        "openalex",
        "OpenAlex",
        "recommended",
        "OpenAlex API Key",
        "Get OpenAlex API key",
        "https://openalex.org/settings/api",
        status="default",
        default=True,
        citation_expansion=True,
    ),
    SourceDefinition("arxiv", "arXiv", "not_required"),
    SourceDefinition("crossref", "Crossref", "not_required"),
    SourceDefinition(
        "semanticscholar",
        "Semantic Scholar",
        "optional",
        "Semantic Scholar API Key",
        "Request Semantic Scholar API key",
        "https://www.semanticscholar.org/product/api#api-key-form",
    ),
    SourceDefinition(
        "pubmed",
        "PubMed",
        "optional",
        "PubMed API Key",
        "Get NCBI API key",
        "https://www.ncbi.nlm.nih.gov/books/NBK25497/",
    ),
    SourceDefinition(
        "wos",
        "Web of Science Starter",
        "required",
        "Web of Science Starter API Key",
        "Register Web of Science Starter application",
        "https://developer.clarivate.com/apis/wos-starter",
    ),
    SourceDefinition(
        "googlescholar",
        "Google Scholar (Serper)",
        "required",
        "Serper API Key",
        "Get Serper API key",
        "https://serper.dev/",
    ),
    SourceDefinition(
        "paperhub",
        "Computer science top conferences",
        "not_required",
    ),
    SourceDefinition(
        "wanfang",
        "Wanfang Data",
        "required",
        "Wanfang AppKey",
        "Get Wanfang AppKey",
        "https://ai.wfdata.com/skills/api",
    ),
    SourceDefinition("nssd", "National Social Sciences Database (NSSD)", "not_required"),
    SourceDefinition("yiigle", "Chinese Medical Journals (Yiigle)", "not_required"),
)

SOURCE_BY_ID = {source.id: source for source in SOURCES}


def list_source_descriptors() -> list[dict[str, object]]:
    return [source.descriptor() for source in SOURCES]
