"""Portable, compact inputs and explicit coverage for bibliometric analysis."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MetricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricPaper(MetricModel):
    id: str
    title: str
    doi: str = ""
    year: int | None = None
    venue: str = ""
    provider: str = ""
    document_types: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    institutions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    keyword_source: str = ""
    references: list[str] = Field(default_factory=list)
    references_available: bool = False
    citations: int | None = None
    citation_source: str = ""
    scored: bool = False
    issns: list[str] = Field(default_factory=list)
    # Venue rankings from the ShowJCR tables, added when an analysis is created.
    cas_tier: int | None = None
    cas_top: bool = False
    jcr_quartile: str = ""
    impact_factor: float | None = None
    ccf_rank: str = ""


class MetricCorpus(MetricModel):
    schema_version: Literal[1] = 1
    scope: Literal["retrieved_candidates", "legacy_ranked_only"] = (
        "retrieved_candidates"
    )
    papers: list[MetricPaper] = Field(default_factory=list)
    source_reported_total: int | None = None
    retrieval_pool_limit: int | None = None
    scoring_limit: int | None = None
    warnings: list[str] = Field(default_factory=list)


class MetricBucket(MetricModel):
    label: str
    count: int


class RankedVenue(MetricModel):
    label: str
    count: int
    cas_tier: int | None = None
    cas_top: bool = False
    jcr_quartile: str = ""
    impact_factor: float | None = None
    ccf_rank: str = ""


class Coverage(MetricModel):
    field: str
    available: int
    total: int


class MetricOverview(MetricModel):
    paper_count: int
    scored_count: int
    cited_paper_count: int
    citation_sum: int
    years: list[MetricBucket]
    venues: list[MetricBucket]
    providers: list[MetricBucket]
    document_types: list[MetricBucket]
    authors: list[MetricBucket]
    institutions: list[MetricBucket]
    countries: list[MetricBucket]
    keywords: list[MetricBucket]
    keyword_sources: list[MetricBucket]
    citation_sources: list[MetricBucket]
    coverage: list[Coverage]
    category_totals: dict[str, int] = Field(default_factory=dict)
    category_limit: int = 100
    ranked_paper_count: int = 0
    cas_top_count: int = 0
    cas_tiers: list[MetricBucket] = Field(default_factory=list)
    jcr_quartiles: list[MetricBucket] = Field(default_factory=list)
    ccf_ranks: list[MetricBucket] = Field(default_factory=list)
    ranked_venues: list[RankedVenue] = Field(default_factory=list)


class NetworkRequest(MetricModel):
    kind: Literal["keywords", "coupling"]
    min_occurrences: int = Field(default=2, ge=1, le=10000)
    min_weight: int = Field(default=1, ge=1, le=10000)
    max_nodes: int = Field(default=100, ge=10, le=300)
    max_edges: int = Field(default=500, ge=10, le=2000)
    # Optional and disclosed: exclude ubiquitous references before pair counting.
    max_reference_frequency: int | None = Field(default=None, ge=2, le=10000)


class NetworkNode(MetricModel):
    id: str
    label: str
    count: int
    group: int = 0
    year: int | None = None


class NetworkEdge(MetricModel):
    source: str
    target: str
    weight: int
    similarity: float


class MetricNetwork(MetricModel):
    parameters: NetworkRequest
    corpus_count: int
    eligible_paper_count: int
    eligible_node_count: int
    total_edge_count: int
    excluded_reference_count: int = 0
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]
    warnings: list[str] = Field(default_factory=list)


class NetworkTooDense(ValueError):
    """The complete requested network exceeds the bounded pair-work budget."""
