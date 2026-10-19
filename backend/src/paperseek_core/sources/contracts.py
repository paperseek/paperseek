from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field

from paperseek_core.models import CoreModel, PaperRecord, SourceSearchMetadata


class SourceSearchRequest(CoreModel):
    query: str
    limit: int = 50
    page: int = 1
    lane: str = "relevance"
    field_ids: tuple[str, ...] = ()


class SourceSearchResult(CoreModel):
    metadata: SourceSearchMetadata
    hits: list[PaperRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    query_feedback: list[str] = Field(default_factory=list)


class SourceCapabilities(CoreModel):
    source: str
    lanes: tuple[str, ...] = ("relevance",)
    metadata_fields: tuple[str, ...] = ()
    supports_abstracts: bool = False
    supports_citations: bool = False
    supports_citation_expansion: bool = False
    supports_pdf_links: bool = False
    supports_hard_field_filter: bool = False
    api_key: Literal["required", "optional", "recommended", "not_required"] = "not_required"
    max_page_size: int = 1000
    max_lane_limit: int = 1000
    page_size_by_lane: dict[str, int] = Field(default_factory=dict)
    single_page_lanes: tuple[str, ...] = ()
    lane_concurrency: int = 1
    backoff_policy: str = ""

    def supports(self, lane: str) -> bool:
        return lane in self.lanes

    def page_size(self, lane: str) -> int:
        return max(1, int(self.page_size_by_lane.get(lane, self.max_page_size) or 1))

    def is_single_page(self, lane: str) -> bool:
        return lane in self.single_page_lanes


class SourceFailureKind(str, Enum):
    CONNECT_TIMEOUT = "connect_timeout"
    SEND_TIMEOUT = "send_timeout"
    FIRST_BYTE_TIMEOUT = "first_byte_timeout"
    READ_IDLE_TIMEOUT = "read_idle_timeout"
    STEP_DEADLINE = "step_deadline"
    CONNECT_ERROR = "connect_error"
    TRANSPORT_ERROR = "transport_error"


class SourceError(Exception):
    def __init__(
        self,
        source: str,
        message: str,
        status: int | None = None,
        body: str = "",
        query: str = "",
        failure_kind: SourceFailureKind | None = None,
        retry_details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.status = status
        self.body = body
        self.query = query
        self.failure_kind = failure_kind
        self.retry_details = dict(retry_details or {})
        if status is not None:
            self.retry_details["http_status"] = status
        if failure_kind is not None:
            self.retry_details["failure_kind"] = failure_kind.value

    @property
    def code(self) -> str:
        if self.status == 429:
            return "source_rate_limited"
        if self.status in {401, 403}:
            return "source_authentication_failed"
        if self.status in {400, 422}:
            return "source_query_rejected"
        if self.status is not None and self.status >= 500:
            return "source_unavailable"
        if self.failure_kind in {
            SourceFailureKind.CONNECT_TIMEOUT, SourceFailureKind.SEND_TIMEOUT,
            SourceFailureKind.FIRST_BYTE_TIMEOUT, SourceFailureKind.READ_IDLE_TIMEOUT,
            SourceFailureKind.STEP_DEADLINE,
        }:
            return "source_timeout"
        if self.failure_kind is not None:
            return "source_connection_failed"
        return "query_primary_source_failed"
