from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReviewPaper(ReviewModel):
    id: str
    source_run_id: str
    source_uid: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    doi: str = ""
    url: str = ""
    provider: str = ""
    source_rank: int = 0
    original_abstract: str = ""
    abstract: str = ""
    abstract_hash: str
    has_abstract: bool


class EvidenceDraft(ReviewModel):
    category: Literal["question", "method", "finding", "scope"]
    text: str = Field(min_length=1, max_length=600)
    quote: str = Field(min_length=1, max_length=2000)


class _DraftModel(ReviewModel):
    # Model output: unknown keys are ignored rather than discarding the whole batch.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class EvidenceItemDraft(_DraftModel):
    category: Literal["question", "method", "finding", "scope"]
    text: str = Field(min_length=1, max_length=600)
    # An over-long quote is trimmed to its best sentences against the abstract, not rejected.
    quote: str = Field(min_length=1, max_length=60000)


class ReviewEvidence(EvidenceDraft):
    id: str
    paper_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    abstract_hash: str
    evidence_type: Literal["abstract"] = "abstract"


# Observations kept per paper; extra ones are trimmed, keeping every category the paper has.
EVIDENCE_PER_PAPER = 8


class EvidenceCard(ReviewModel):
    paper_id: str
    signature: str
    evidence: list[ReviewEvidence] = Field(max_length=EVIDENCE_PER_PAPER)


class EvidenceCardDraft(_DraftModel):
    paper_id: str
    evidence: list[EvidenceItemDraft] = Field(max_length=40)


class EvidenceBatch(_DraftModel):
    cards: list[EvidenceCardDraft] = Field(min_length=1, max_length=4)


class ParagraphPlan(ReviewModel):
    goal: str = Field(min_length=1, max_length=1200)
    paper_ids: list[str] = Field(min_length=1, max_length=100)


# Most papers a generated paragraph plan may carry; a user's own outline is not limited by it.
PLANNED_PAPERS_PER_PARAGRAPH = 10


class PaperExclusion(ReviewModel):
    """A selected paper the outline or a section deliberately does not cite, and why."""
    paper_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=400)
    # Set by the evidence check when the reason looks doubtful for this paper's evidence.
    questioned: bool = False
    check_reason: str = Field(default="", max_length=600)


class OutlineSection(ReviewModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=250)
    parent_id: str = ""
    kind: Literal["theme", "overview", "conclusion"] = "theme"
    goal: str = Field(min_length=1, max_length=1500)
    comparisons: list[str] = Field(default_factory=list, max_length=8)
    paper_ids: list[str] = Field(min_length=1, max_length=100)
    paragraphs: list[ParagraphPlan] = Field(default_factory=list, max_length=12)

    @field_validator("id")
    @classmethod
    def non_reserved_id(cls, value: str) -> str:
        if value == "outline":
            raise ValueError("Use a section ID such as s1; outline is reserved for the outline history.")
        return value


class ReviewOutline(ReviewModel):
    focus: str = Field(default="", max_length=2000)
    sections: list[OutlineSection] = Field(min_length=1, max_length=14)
    # Selected papers the outline leaves out on purpose, each with its reason.
    excluded: list[PaperExclusion] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def hierarchy(self) -> ReviewOutline:
        sections = {section.id: section for section in self.sections}
        if len(sections) != len(self.sections):
            raise ValueError("Section IDs must be unique.")
        for section in self.sections:
            if section.parent_id and (
                section.parent_id not in sections or section.parent_id == section.id
                or sections[section.parent_id].parent_id
            ):
                raise ValueError("The outline supports at most two levels.")
            if len(set(section.paper_ids)) != len(section.paper_ids):
                raise ValueError("Assigned papers must be unique.")
            for index, paragraph in enumerate(section.paragraphs):
                if len(set(paragraph.paper_ids)) != len(paragraph.paper_ids):
                    raise ValueError(f"Section {section.id}, paragraph {index + 1}: paper IDs must be unique.")
                if outside := set(paragraph.paper_ids) - set(section.paper_ids):
                    raise ValueError(f"Section {section.id}, paragraph {index + 1} uses unassigned papers {sorted(outside)}. "
                                     "Make paragraph sources and the section's paper_ids consistent.")
            covered = {paper for paragraph in section.paragraphs for paper in paragraph.paper_ids}
            if section.paragraphs and (missing := set(section.paper_ids) - covered):
                raise ValueError(f"Section {section.id}: assign these papers to relevant paragraph plans: {sorted(missing)}.")
        return self


class ReviewParagraph(ReviewModel):
    text: str = Field(min_length=1, max_length=6000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class SectionBody(ReviewModel):
    paragraphs: list[ReviewParagraph] = Field(min_length=1, max_length=40)
    # Planned papers this section does not cite, each with the writer's reason.
    omitted: list[PaperExclusion] = Field(default_factory=list, max_length=100)


class SupportFinding(ReviewModel):
    paragraph: int = Field(ge=0)
    status: Literal["supported", "needs_revision", "unsupported"]
    reason: str = Field(default="", max_length=1500)


class OmissionCheck(ReviewModel):
    paper_id: str
    status: Literal["accepted", "questioned"]
    reason: str = Field(default="", max_length=600)


class SupportCheck(ReviewModel):
    findings: list[SupportFinding] = Field(min_length=1, max_length=40)
    omissions: list[OmissionCheck] = Field(default_factory=list, max_length=100)


class ConsistencyFinding(ReviewModel):
    section_ids: list[str] = Field(min_length=1, max_length=2)
    reason: str = Field(min_length=1, max_length=1000)


class ConsistencyCheck(ReviewModel):
    has_material_issue: bool
    findings: list[ConsistencyFinding] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def issues_match_verdict(self) -> ConsistencyCheck:
        if self.has_material_issue != bool(self.findings):
            raise ValueError("No material issue requires empty findings; an issue requires a concrete finding.")
        return self


class WrittenSection(ReviewModel):
    section_id: str
    revision: int = Field(ge=1)
    body: SectionBody
    author: Literal["model", "user"]
    signature: str
    validation: Literal["supported", "needs_revision", "user_edited"]
    findings: list[SupportFinding] = Field(default_factory=list)
    model: str = ""
    job_id: str = ""
    stale: bool = False


class ReviewCitation(ReviewModel):
    number: int
    paper_id: str
    evidence_ids: list[str]
    label: str = ""


class CitationSpan(ReviewModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    text: str = ""
    citation: ReviewCitation | None = None


class CitedParagraph(ReviewModel):
    text: str
    citations: list[ReviewCitation]
    spans: list[CitationSpan] = Field(default_factory=list)
    citation_warnings: list[str] = Field(default_factory=list)
    # Citations the text does not place beside a claim, listed apart from the prose.
    unplaced: list[ReviewCitation] = Field(default_factory=list)


class RenderedSection(ReviewModel):
    id: str
    title: str
    level: int
    revision: int
    validation: str
    stale: bool
    paragraphs: list[CitedParagraph]
    omitted: list[PaperExclusion] = Field(default_factory=list)


class ReviewReference(ReviewModel):
    number: int
    paper_id: str
    text: str
    doi: str
    url: str


class RenderedReview(ReviewModel):
    id: str
    version: int
    title: str
    language: str
    citation_style: Literal["numeric", "harvard"] = "numeric"
    sections: list[RenderedSection] = Field(default_factory=list)
    references: list[ReviewReference] = Field(default_factory=list)
    draft: bool = True
