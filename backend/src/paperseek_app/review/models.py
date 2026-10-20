from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from paperseek_app.configuration import ModelRouteSnapshot
from paperseek_app.errors import RunError
from paperseek_app.models import RunDetail
from paperseek_app.submission import CredentialInput, LLMRouteInput, ServiceSelectionInput
from paperseek_core.review.models import (
    ConsistencyFinding, EvidenceCard, RenderedReview, ReviewModel, ReviewOutline, ReviewPaper, SectionBody, WrittenSection,
)


class ReviewError(RunError):
    def __init__(self, code: str, *, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


PARAGRAPH_LENGTH_RANGE = {"zh": (100, 1500), "en": (50, 800)}


class ReviewCreate(ReviewModel):
    source_run_id: UUID
    paper_uids: list[str] = Field(min_length=2, max_length=100)
    topic: str = Field(min_length=1, max_length=2000)
    language: Literal["zh", "en"] = "zh"
    length: Literal["short", "standard", "detailed"] = "standard"
    section_count: int | None = Field(default=None, ge=1, le=14, strict=True)
    # None keeps the length preset's planning range; set values are exact user choices.
    paragraph_count: int | None = Field(default=None, ge=1, le=10, strict=True)
    # Characters for Chinese output, words for English output.
    paragraph_length: int | None = Field(default=None, ge=50, le=1500, strict=True)

    @model_validator(mode="after")
    def unique_papers(self) -> ReviewCreate:
        if len(set(self.paper_uids)) != len(self.paper_uids):
            raise ValueError("Paper UIDs must be unique.")
        low, high = PARAGRAPH_LENGTH_RANGE[self.language]
        if self.paragraph_length is not None and not low <= self.paragraph_length <= high:
            raise ValueError(f"Paragraph length must be between {low} and {high}.")
        return self


class ReviewContent(ReviewModel):
    source_question: str = ""
    citation_map: dict[str, Any] | None = None
    topic: str
    language: Literal["zh", "en"]
    length: Literal["short", "standard", "detailed"]
    section_count: int | None = Field(default=None, ge=1, le=14, strict=True)
    paragraph_count: int | None = Field(default=None, ge=1, le=10, strict=True)
    paragraph_length: int | None = Field(default=None, ge=50, le=1500, strict=True)
    corpus_hash: str
    papers: list[ReviewPaper]
    model_route: ModelRouteSnapshot = Field(default_factory=dict)
    cards: list[EvidenceCard] = Field(default_factory=list)
    evidence_errors: dict[str, str] = Field(default_factory=dict)
    outline: ReviewOutline | None = None
    outline_revision: int = 0
    approved_outline_revision: int = 0
    sections: list[WrittenSection] = Field(default_factory=list)
    consistency_findings: list[ConsistencyFinding] = Field(default_factory=list)
    consistency_signature: str = ""


class ReviewGenerationFailure(ReviewModel):
    task: str = Field(max_length=64)
    reason: str = Field(max_length=2000)


class ReviewJobSummary(ReviewModel):
    """What the latest job asked for, so a failed task can be retried as-is."""
    action: Literal["prepare_outline", "write", "rewrite_section"]
    section_id: str = ""
    instruction: str = ""


class ReviewDetail(ReviewModel):
    display_title: str = ""
    id: str
    source_run_id: str
    version: int
    content: ReviewContent
    document: RenderedReview | None = None
    active_job: RunDetail | None = None
    latest_job: RunDetail | None = None
    generation_failure: ReviewGenerationFailure | None = None
    latest_request: ReviewJobSummary | None = None
    created_at: datetime
    updated_at: datetime


class ReviewSummary(ReviewModel):
    display_title: str = ""
    id: str
    topic: str
    paper_count: int
    abstract_count: int
    completed_sections: int
    pending_sections: int = 0
    section_count: int
    phase: Literal["draft", "outline", "writing", "complete", "needs_attention"]
    active: bool
    updated_at: datetime


class ReviewPage(ReviewModel):
    items: list[ReviewSummary]
    total_count: int
    next_offset: int | None = None


class OutlineSave(ReviewModel):
    expected_version: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    outline: ReviewOutline


class SectionSave(ReviewModel):
    expected_version: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    body: SectionBody


class ReviewJobRequest(ReviewModel):
    service: ServiceSelectionInput | None = None
    action: Literal["prepare_outline", "write", "rewrite_section"]
    expected_version: int = Field(ge=1)
    outline_revision: int = Field(default=0, ge=0)
    section_id: str = Field(default="", max_length=64)
    section_revision: int = Field(default=0, ge=0)
    instruction: str = Field(default="", max_length=2000)
    llm: LLMRouteInput = Field(default_factory=LLMRouteInput)
    credential: CredentialInput = Field(default_factory=CredentialInput, repr=False)
    connection_id: str = Field(default="", max_length=64)


class ReviewJobContext(ReviewModel):
    review_id: str
    run_id: str
    action: Literal["prepare_outline", "write", "rewrite_section"]
    outline_revision: int = 0
    section_id: str = ""
    section_revision: int = 0
    instruction: str = ""


class ReviewRevision(ReviewModel):
    part: str
    revision: int
    author: Literal["model", "user"]
    created_at: datetime
    outline: ReviewOutline | None = None
    section: WrittenSection | None = None


class ReviewVersions(ReviewModel):
    items: list[ReviewRevision]


class PaperSuggestionRequest(ReviewModel):
    service: ServiceSelectionInput | None = None
    source_run_id: UUID
    topic: str = Field(min_length=1, max_length=2000)
    language: Literal["zh", "en"] = "zh"
    count: int = Field(ge=2, le=100, strict=True)
    llm: LLMRouteInput = Field(default_factory=LLMRouteInput)
    credential: CredentialInput = Field(default_factory=CredentialInput, repr=False)
    connection_id: str = Field(default="", max_length=64)


class PaperSuggestion(ReviewModel):
    uid: str
    theme: str = ""
    reason: str = ""


class PaperSuggestions(ReviewModel):
    items: list[PaperSuggestion]
