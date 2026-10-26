from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypedDict
from paperseek_core.bibliometrics.models import MetricCorpus


class CoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PaperAuthor(CoreModel):
    display_name: str = ""
    wos_standard: str = ""
    researcher_id: str = ""


class PaperNames(CoreModel):
    authors: list[PaperAuthor] = Field(default_factory=list)


class PaperSource(CoreModel):
    source_title: str = ""
    publish_year: int | None = None
    publish_month: str = ""
    volume: str = ""
    issue: str = ""
    pages: Any = None


class PaperLinks(CoreModel):
    record: str = ""
    citing_articles: str = ""
    references: str = ""
    related: str = ""
    landing_page: str = ""
    pdf: str = ""


class PaperCitation(CoreModel):
    db: str = ""
    count: int = 0


class PaperIdentifiers(CoreModel):
    doi: str = ""
    issn: str = ""
    eissn: str = ""
    isbn: str = ""
    eisbn: str = ""
    pmid: str = ""
    openalex: str = ""
    arxiv: str = ""
    semanticscholar: str = ""


class PaperKeywords(CoreModel):
    author_keywords: list[str] = Field(default_factory=list)


class PaperRecord(CoreModel):
    uid: str
    title: str = ""
    types: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    source: PaperSource | None = None
    names: PaperNames | None = None
    links: PaperLinks | None = None
    citations: list[PaperCitation] = Field(default_factory=list)
    identifiers: PaperIdentifiers | None = None
    keywords: PaperKeywords | None = None
    abstract: str = ""
    provider: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class RankingScore(CoreModel):
    uid: str = Field(min_length=1)
    score: float
    reasoning: str = ""


class RankedPaper(CoreModel):
    document: PaperRecord
    score: float | None = None
    reasoning: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class RankingRequest(CoreModel):
    question: str
    documents: list[PaperRecord] = Field(default_factory=list)
    plan_context: str = "{}"
    provenance_by_identity: dict[str, dict[str, Any]] = Field(default_factory=dict)
    step_id: str = "llm_reranking"
    # Scoring batches of this size run side by side, as do the groups that write reasons.
    batch_size: int = 64
    concurrency: int = 8
    resume_scores: list[Any] = Field(default_factory=list)
    completed_batch_ids: list[str] = Field(default_factory=list)
    checkpoint_resume_stage: str = "pre_rank"


class RankingResult(CoreModel):
    items: list[RankedPaper] = Field(default_factory=list)
    scores: list[RankingScore] = Field(default_factory=list)
    completed_batch_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SourceSearchMetadata(CoreModel):
    total: int = 0
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=10, ge=1)
    raw_count: int | None = Field(default=None, ge=0)


class RetrievalRequest(CoreModel):
    question: str
    query: str
    primary_hits: list[PaperRecord] = Field(default_factory=list)
    primary_page: SourceSearchMetadata | None = None
    supplemental_lanes: dict[str, list[PaperRecord]] = Field(default_factory=dict)
    lane_limit: int = Field(default=500, ge=1)
    ranking_candidate_limit: int = Field(default=256, ge=1)
    pool_max: int = Field(default=3000, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    field_ids: tuple[str, ...] = ()
    resume_lane_results: dict[str, list[PaperRecord]] = Field(default_factory=dict)
    completed_lanes: list[str] = Field(default_factory=list)
    checkpoint_resume_stage: str = "retrieve"


class RetrievalRerankItem(CoreModel):
    document: PaperRecord
    score: float


class RetrievalRerankResult(CoreModel):
    items: list[RetrievalRerankItem] = Field(default_factory=list)
    provider: str = ""
    model: str = ""


class RetrievalEmbeddingResult(CoreModel):
    scores: list[float]
    provider: str
    model: str


class RetrievalEmbeddingState(CoreModel):
    question: str
    provider: str
    model: str
    scores_by_identity: dict[str, float]
    text_hashes: dict[str, str]


class DecisionScore(CoreModel):
    score: float = Field(ge=0, le=3, allow_inf_nan=False, strict=True)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)


class DecisionBatchResult(CoreModel):
    scores: dict[str, DecisionScore]
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class DecisionBatchCheckpoint(CoreModel):
    input_fingerprint: str
    batch_id: str
    result: DecisionBatchResult


class CandidateSelectionResult(CoreModel):
    documents: list[PaperRecord]
    provenance_by_identity: dict[str, dict[str, Any]]
    selected_count: int = Field(ge=0)
    input_fingerprint: str
    warnings: list[str] = Field(default_factory=list)
    requested_mode: str = "local_fusion"
    actual_mode: str = "local_fusion"
    component_statuses: dict[Literal["embedding", "reranker", "decision"], Literal["disabled", "skipped", "succeeded", "failed"]] = Field(default_factory=dict)


class RetrievalResult(CoreModel):
    bibliometric_corpus: MetricCorpus | None = None
    documents: list[PaperRecord] = Field(default_factory=list)
    provenance_by_identity: dict[str, dict[str, Any]] = Field(default_factory=dict)
    lane_counts: dict[str, int] = Field(default_factory=dict)
    total_candidates: int = 0
    completed_lanes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    embedding_state: RetrievalEmbeddingState | None = None


class CitationSeedPlan(CoreModel):
    record: PaperRecord
    role: str = "relevance"
    directions: tuple[Literal["backward", "forward"], ...] = ("backward", "forward")
    depth: int = Field(default=1, ge=1, le=3)


class CitationEdge(CoreModel):
    seed_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    direction: Literal["backward", "forward"]
    depth: int = Field(ge=1, le=3)

    def to_citation_map(self) -> dict[str, str]:
        if self.direction == "backward":
            source, target, relation = self.parent_id, self.record_id, "references"
        else:
            source, target, relation = self.record_id, self.parent_id, "cites"
        return {
            "source": source,
            "target": target,
            "type": relation,
            "seed": self.seed_id,
            "layer": str(self.depth),
        }

    @classmethod
    def from_citation_map(cls, value: dict[str, Any]) -> "CitationEdge":
        relation = str(value.get("type") or "")
        source = str(value.get("source") or "")
        target = str(value.get("target") or "")
        if relation == "references":
            direction, parent_id, record_id = "backward", source, target
        elif relation == "cites":
            direction, parent_id, record_id = "forward", target, source
        else:
            raise ValueError(f"Unsupported citation relation: {relation or '<empty>'}")
        return cls(
            seed_id=str(value.get("seed") or ""),
            parent_id=parent_id,
            record_id=record_id,
            direction=direction,
            depth=max(1, min(int(value.get("layer") or 1), 3)),
        )


class CitationSourceRequest(CoreModel):
    seed_plans: list[CitationSeedPlan] = Field(default_factory=list)
    per_seed: int = 4
    max_records: int = 40
    field_ids: tuple[str, ...] = ()
    depth: int = 1
    request_attempts: int = 1
    request_timeout: int = 10


class CitationSourceResult(CoreModel):
    records: list[PaperRecord] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[CitationEdge] = Field(default_factory=list)
    frontier: dict[str, Any] = Field(default_factory=dict)


class CitationExpansionRequest(CoreModel):
    candidates: list[PaperRecord] = Field(default_factory=list)
    seed_candidates: list[PaperRecord] = Field(default_factory=list)
    enabled: bool = True
    source_rate_limited: bool = False
    seed_count: int = 30
    per_seed: int = 4
    max_records: int = 160
    depth: int = 2
    field_ids: tuple[str, ...] = ()


class CitationExpansionResult(CoreModel):
    candidates: list[PaperRecord] = Field(default_factory=list)
    citation_map: dict[str, Any] = Field(default_factory=dict)
    provenance_by_identity: dict[str, dict[str, Any]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


QuestionType = Literal[
    "broad_landscape",
    "specific_relation",
    "method_application",
    "mechanism",
    "comparison",
    "seminal_work",
    "recent_progress",
    "systematic_search",
]
FieldHint = Literal["all", "title", "abstract"]
ViewType = Literal[
    "direct_terminology",
    "successor_paradigm",
    "mechanism_method",
    "application_benchmark",
    "evidence_synthesis",
    "relation_variant",
    "domain_variant",
    "other",
]


def _clean_string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    output: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,;")
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


class ConceptGroup(CoreModel):
    label: str = ""
    terms: list[str] = Field(default_factory=list)
    required: bool = True
    field_hint: FieldHint = "all"

    @model_validator(mode="before")
    @classmethod
    def normalize_group(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        label = re.sub(r"\s+", " ", str(payload.get("label") or "")).strip()
        terms = _clean_string_list(payload.get("terms"))
        if not terms and label:
            terms = [label]
        if not label and terms:
            label = terms[0]
        payload["label"] = label
        payload["terms"] = terms
        return payload

    @model_validator(mode="after")
    def require_terms(self) -> "ConceptGroup":
        if not self.terms:
            raise ValueError("concept group must contain at least one term")
        return self


class QueryView(CoreModel):
    view_id: str = Field(min_length=1)
    view_type: ViewType = "other"
    purpose: str = ""
    concept_groups: list[ConceptGroup] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_view(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        raw_id = payload.get("view_id") or payload.get("id") or payload.get("name") or "query_view"
        payload["view_id"] = re.sub(
            r"[^a-z0-9_-]+",
            "_",
            str(raw_id).strip().lower(),
        ).strip("_") or "query_view"
        payload.pop("id", None)
        payload.pop("name", None)
        payload["purpose"] = re.sub(r"\s+", " ", str(payload.get("purpose") or "")).strip()
        payload["concept_groups"] = payload.get("concept_groups") or payload.pop("groups", [])
        payload["exclusions"] = _clean_string_list(payload.get("exclusions"))
        return payload

    @model_validator(mode="after")
    def infer_type_and_require_groups(self) -> "QueryView":
        if not self.concept_groups:
            raise ValueError("query view must contain at least one concept group")
        if self.view_type != "other":
            return self
        haystack = " ".join(
            [
                self.view_id,
                self.purpose,
                *[group.label for group in self.concept_groups],
                *[term for group in self.concept_groups for term in group.terms],
            ]
        ).casefold()
        inferred: ViewType = "other"
        if any(marker in haystack for marker in ("successor", "terminology evolution", "cross-modal", "cross modal", "image-text", "vision-language", "modern paradigm")):
            inferred = "successor_paradigm"
        elif any(marker in haystack for marker in ("direct", "terminology", "lexical", "field name")):
            inferred = "direct_terminology"
        elif any(marker in haystack for marker in ("survey", "review", "overview", "synthesis")):
            inferred = "evidence_synthesis"
        elif any(marker in haystack for marker in ("application", "benchmark", "dataset", "use case")):
            inferred = "application_benchmark"
        elif any(marker in haystack for marker in ("method", "mechanism", "feature", "technique", "model")):
            inferred = "mechanism_method"
        if inferred != "other":
            object.__setattr__(self, "view_type", inferred)
        return self


class SearchPlan(CoreModel):
    intent: str = Field(min_length=1)
    question_type: QuestionType = "specific_relation"
    core_concepts: list[str] = Field(default_factory=list)
    must_have_facets: list[str] = Field(default_factory=list)
    optional_facets: list[str] = Field(default_factory=list)
    required_relation: str = ""
    likely_synonyms: list[str] = Field(default_factory=list)
    adjacent_concepts: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    adjustment_strategy: str = ""
    ambiguities: list[str] = Field(default_factory=list)
    query_views: list[QueryView] = Field(default_factory=list)

    @field_validator("intent", "required_relation", "adjustment_strategy")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @field_validator(
        "core_concepts",
        "must_have_facets",
        "optional_facets",
        "likely_synonyms",
        "adjacent_concepts",
        "boundaries",
        "ambiguities",
        mode="before",
    )
    @classmethod
    def clean_terms(cls, value: Any) -> list[str]:
        return _clean_string_list(value)

    @model_validator(mode="after")
    def normalize_plan(self) -> "SearchPlan":
        core_concepts = list(self.core_concepts or [self.intent])
        must_have = list(self.must_have_facets or core_concepts)
        required_keys = {value.casefold() for value in must_have}
        optional = [value for value in self.optional_facets if value.casefold() not in required_keys]
        seen: set[str] = set()
        views: list[QueryView] = []
        for view in self.query_views:
            if view.view_id in seen:
                continue
            seen.add(view.view_id)
            if self.question_type == "broad_landscape":
                view = view.model_copy(
                    update={
                        "concept_groups": [
                            group.model_copy(update={"field_hint": "all"})
                            for group in view.concept_groups
                        ]
                    }
                )
            views.append(view)
        object.__setattr__(self, "core_concepts", core_concepts)
        object.__setattr__(self, "must_have_facets", must_have)
        object.__setattr__(self, "optional_facets", optional)
        object.__setattr__(self, "query_views", views[:6])
        return self

    def audit_summary(self) -> str:
        parts = [f"intent: {self.intent}", f"question_type: {self.question_type}"]
        for label, values in (
            ("core_concepts", self.core_concepts),
            ("must_have_facets", self.must_have_facets),
            ("optional_facets", self.optional_facets),
            ("likely_synonyms", self.likely_synonyms),
            ("adjacent_concepts", self.adjacent_concepts),
            ("boundaries", self.boundaries),
            ("ambiguities", self.ambiguities),
        ):
            if values:
                parts.append(f"{label}: {'; '.join(values)}")
        if self.required_relation:
            parts.append(f"required_relation: {self.required_relation}")
        if self.adjustment_strategy:
            parts.append(f"adjustment_strategy: {self.adjustment_strategy}")
        if self.query_views:
            parts.append(
                "query_views: "
                + "; ".join(
                    f"{view.view_id} [{view.view_type}] ({view.purpose})"
                    for view in self.query_views
                )
            )
        return "\n".join(parts)

    def coverage_gaps(self) -> list[str]:
        gaps = []
        if len(self.query_views) < 3:
            gaps.append(
                "at least three valid complementary query views; the current plan would silently fall back to a single-query search"
            )
        if self.question_type == "broad_landscape" and "successor_paradigm" not in {
            view.view_type for view in self.query_views
        }:
            gaps.append(
                "a successor_paradigm query view covering established newer terminology or methods that solve the task without repeating the exact field name"
            )
        return gaps


class SourceQueryProposal(CoreModel):
    query: str = Field(min_length=1)
    rationale: str = ""
    warnings: list[str] = Field(default_factory=list)


class QueryValidation(CoreModel):
    source: str
    query: str = ""
    ok: bool
    issue: str = ""


class QueryLoopDecision(CoreModel):
    action: str
    message: str
    adjustment: str = ""
    terminal: bool = False
    reason: str = ""
    stop_reason: str = ""


class QueryLoopSnapshot(CoreModel):
    iteration: int
    configured_iterations: int
    adaptive_max_iterations: int
    total: int
    target_min: int
    target_max: int
    retrieval_pool_min: int
    retrieval_pool_max: int
    history: list[dict[str, Any]] = Field(default_factory=list)
    query: str = ""
    hit_count: int = 0
    top_titles: list[str] = Field(default_factory=list)
    intent_terms: list[str] = Field(default_factory=list)

    @property
    def consecutive_zero_results(self) -> int:
        if self.total != 0:
            return 0
        count = 1
        for row in reversed(self.history):
            if row.get("total") == 0:
                count += 1
                continue
            if row.get("action") in {"sanitize", "repair"}:
                continue
            break
        return count


class QueryLoopBudget(CoreModel):
    target_min: int = Field(default=5, ge=0)
    target_max: int = Field(default=50, ge=1)
    retrieval_pool_min: int = Field(default=5, ge=0)
    retrieval_pool_max: int = Field(default=3000, ge=1)
    max_iterations: int = Field(default=5, ge=1)
    adaptive_max_iterations: int | None = Field(default=None, ge=1)
    query_portfolio_enabled: bool = True
    query_portfolio_max: int = Field(default=3, ge=1, le=6)
    query_portfolio_limit: int = Field(default=100, ge=1, le=100)
    retrieval_lane_limit: int = Field(default=500, ge=1)
    ranking_candidate_limit: int = Field(default=256, ge=1)
    field_ids: tuple[str, ...] = ()


class QueryHistoryEntry(CoreModel):
    iteration: int
    query: str
    total: int | None = None
    action: str
    next_query: str | None = None
    message: str = ""
    rationale: str = ""
    controller_reason: str = ""
    source_feedback: list[str] = Field(default_factory=list)
    view_id: str = ""
    view_type: str = ""
    purpose: str = ""
    status: str = ""
    selector_score: float = 0.0
    selector_overlap: float = 0.0
    selector_reasons: list[str] = Field(default_factory=list)


class QueryIterationMemory(CoreModel):
    iteration: int
    query: str
    total: int = 0
    records: list[PaperRecord] = Field(default_factory=list)


class QueryViewSelection(CoreModel):
    source: str = ""
    view_id: str
    view_type: str = ""
    selected: bool = False
    score: float = 0.0
    lexical_overlap: float = 0.0
    max_pairwise_overlap: float = 0.0
    reason_codes: list[str] = Field(default_factory=list)
    compile_error: str = ""
    compiled_query: str = Field(default="", exclude=True)
    original_index: int = Field(default=0, exclude=True)


class QueryPortfolioExecution(CoreModel):
    view_id: str
    source: str = ""
    view_type: str = ""
    purpose: str = ""
    query: str
    total: int = 0
    hit_count: int = 0
    added_unique: int = 0
    status: str = "completed"
    error: str = ""
    selector_score: float = 0.0
    selector_overlap: float = 0.0
    selector_reasons: list[str] = Field(default_factory=list)


class QueryLoopResumeState(CoreModel):
    phase: str = "query_ready"
    question: str
    source: str
    search_plan: SearchPlan
    current_query: str
    iteration: int = 0
    total: int = 0
    history: list[QueryHistoryEntry] = Field(default_factory=list)
    query_iteration_memory: list[QueryIterationMemory] = Field(default_factory=list)
    primary_hits: list[PaperRecord] = Field(default_factory=list)
    primary_page: SourceSearchMetadata | None = None
    fallback_query: str = ""
    fallback_total: int = 0
    fallback_hits: list[PaperRecord] = Field(default_factory=list)
    fallback_page: SourceSearchMetadata | None = None
    query_view_selection: list[QueryViewSelection] = Field(default_factory=list)
    query_portfolio: list[QueryPortfolioExecution] = Field(default_factory=list)
    portfolio_lane_results: dict[str, list[PaperRecord]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    terminal_status: str = ""


class QueryLoopRequest(CoreModel):
    question: str = Field(min_length=1)
    source_context: str = ""
    budget: QueryLoopBudget = Field(default_factory=QueryLoopBudget)
    resume: QueryLoopResumeState | None = None


class QueryLoopResult(CoreModel):
    question: str
    source: str
    search_plan: SearchPlan
    final_query: str
    total: int = 0
    primary_hits: list[PaperRecord] = Field(default_factory=list)
    records: list[PaperRecord] = Field(default_factory=list)
    iterations: int = 0
    history: list[QueryHistoryEntry] = Field(default_factory=list)
    query_iteration_memory: list[QueryIterationMemory] = Field(default_factory=list)
    query_view_selection: list[QueryViewSelection] = Field(default_factory=list)
    query_portfolio: list[QueryPortfolioExecution] = Field(default_factory=list)
    terminal_status: str = ""
    warnings: list[str] = Field(default_factory=list)
    retrieval_request: RetrievalRequest












class ArtifactDraft(CoreModel):
    kind: str
    filename: str
    media_type: str
    content: str


class FinalPaperResult(CoreModel):
    rank: int
    source: str = ""
    id: str = ""
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    publication_type: str = ""
    doi: str = ""
    url: str = ""
    pdf_url: str = ""
    abstract: str = ""
    keywords: str = ""
    citation_count: int = 0
    relevance_score: float | None = None
    relevance_reason: str = ""
    source_rank: int = 0
    retrieval_score: float = 0
    retrieval_lanes: list[str] = Field(default_factory=list)
    source_raw_id: str = ""
    links: dict[str, Any] = Field(default_factory=dict)
    keywords_text: str = ""
    authors_text: str = ""
    score: float | None = None
    provider: str = ""
    uid: str = ""
    publish_year: int | None = None
    document_types: list[str] = Field(default_factory=list)
    citations: str = "0"
    reasoning: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    issn: str = ""
    eissn: str = ""
    arxiv_id: str = ""
    # Partitions and impact figures per platform; see paperseek_core.journal_metrics.
    journal_metrics: dict[str, Any] = Field(default_factory=dict)
    # Conference acceptance (venue, year, Oral/Spotlight/Poster); see paperseek_core.acceptance.
    acceptance: dict[str, Any] = Field(default_factory=dict)


class ResearchAssemblyRequest(CoreModel):
    bibliometric_corpus: MetricCorpus | None = None
    question: str = Field(min_length=1)
    source: str = Field(min_length=1)
    search_plan: SearchPlan
    final_query: str
    total: int = 0
    iterations: int = 0
    history: list[dict[str, Any] | QueryHistoryEntry] = Field(default_factory=list)
    query_view_selection: list[dict[str, Any] | QueryViewSelection] = Field(default_factory=list)
    query_portfolio: list[dict[str, Any] | QueryPortfolioExecution] = Field(default_factory=list)
    query_iteration_memory: list[dict[str, Any]] = Field(default_factory=list)
    citation_map: dict[str, Any] = Field(default_factory=dict)
    ranking_steps: list[dict[str, Any]] = Field(default_factory=list)
    search_state: dict[str, Any] = Field(default_factory=dict)
    items: list[RankedPaper] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Per document uid: {"journal_metrics": {...}, "acceptance": {...}} from the enrichment step.
    annotations_by_uid: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ResearchResult(CoreModel):
    bibliometric_corpus: MetricCorpus | None = None
    question: str
    search_intent: str
    search_plan: dict[str, Any]
    query_view_selection: list[dict[str, Any]] = Field(default_factory=list)
    query_portfolio: list[dict[str, Any]] = Field(default_factory=list)
    source: str
    final_query: str
    db: str
    field: str = ""
    total: int = 0
    iterations: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    search_state: dict[str, Any] = Field(default_factory=dict)
    citation_map: dict[str, Any] = Field(default_factory=dict)
    ranking_steps: list[dict[str, Any]] = Field(default_factory=list)
    ranked: list[FinalPaperResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provenance_by_identity: dict[str, dict[str, Any]] = Field(default_factory=dict)
    query_iteration_memory: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[ArtifactDraft] = Field(default_factory=list)

    def public_payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "search_intent": self.search_intent,
            "search_plan": self.search_plan,
            "query_view_selection": self.query_view_selection,
            "query_portfolio": self.query_portfolio,
            "source": self.source,
            "final_query": self.final_query,
            "db": self.db,
            "field": self.field,
            "total": self.total,
            "iterations": self.iterations,
            "history": self.history,
            "search_state": self.search_state,
            "citation_map": self.citation_map,
            "ranking_steps": self.ranking_steps,
            "ranked": [item.model_dump(mode="json") for item in self.ranked],
        }


class ResearchWorkflowResumeState(CoreModel):
    query: QueryLoopResumeState | None = None
    retrieval: RetrievalResult | None = None
    retrieval_lane_results: dict[str, list[PaperRecord]] = Field(default_factory=dict)
    retrieval_completed_lanes: list[str] = Field(default_factory=list)
    citation: CitationExpansionResult | None = None
    selection: CandidateSelectionResult | None = None
    decision_batches: list[DecisionBatchCheckpoint] = Field(default_factory=list)
    ranking_scores: list[Any] = Field(default_factory=list)
    ranking_completed_batch_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ranking_requires_selected_candidates(self) -> ResearchWorkflowResumeState:
        if (self.ranking_scores or self.ranking_completed_batch_ids) and self.selection is None:
            raise ValueError("Ranking recovery requires a completed candidate selection checkpoint.")
        return self


class ResearchWorkflowRequest(CoreModel):
    question: str = Field(min_length=1)
    source_context: str = ""
    query_budget: QueryLoopBudget = Field(default_factory=QueryLoopBudget)
    citation_enabled: bool = True
    citation_seed_count: int = 30
    citation_per_seed: int = 4
    citation_max_records: int = 160
    citation_depth: int = 2
    ranking_batch_size: int = 64
    ranking_concurrency: int = 8
    resume: ResearchWorkflowResumeState | None = None


class ProgressPayload(TypedDict, total=False):
    """Shared event metrics; stage-specific audit data remains extensible."""

    __pydantic_config__ = ConfigDict(extra="allow", strict=True)
    code: str
    source: str
    http_status: int
    failure_kind: str
    candidate_count: int
    result_count: int
    warning_count: int
    scored_count: int
    total_candidates: int
    completed_lanes: int
    total_lanes: int
    completed_batches: int
    batch_count: int
    lane_counts: dict[str, int]


class CoreProgressEvent(CoreModel):
    event_type: str
    stage: str
    payload: ProgressPayload = Field(default_factory=dict)
    schema_version: int = 1


class CheckpointDraft(CoreModel):
    step_key: str
    idempotency_key: str
    state: dict[str, Any] = Field(default_factory=dict)
    workflow_version: str = "m2.source-models.v1"
    schema_version: int = 1
