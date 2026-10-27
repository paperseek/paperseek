"""Paper-level full counting, with missing data kept distinct from zero."""

from collections import Counter

from paperseek_core.bibliometrics.models import (
    Coverage,
    MetricBucket,
    MetricCorpus,
    MetricOverview,
    RankedVenue,
)


def overview(corpus: MetricCorpus) -> MetricOverview:
    papers = corpus.papers
    category_totals = {}

    def buckets(field: str, *, many: bool = False, chronological: bool = False):
        counts = Counter()
        for paper in papers:
            value = getattr(paper, field)
            counts.update(
                set(value)
                if many
                else [str(value)]
                if value is not None and value != ""
                else []
            )
        ordered = sorted(
            counts.items(),
            key=lambda item: item[0] if chronological else (-item[1], item[0]),
        )
        category_totals[field] = len(ordered)
        return [
            MetricBucket(label=label, count=count)
            for label, count in (ordered if chronological else ordered[:100])
        ]

    fields = (
        "year",
        "venue",
        "authors",
        "institutions",
        "countries",
        "keywords",
        "references",
        "citations",
        "doi",
    )
    coverage = [
        Coverage(
            field=field,
            total=len(papers),
            available=sum(
                getattr(paper, field) is not None
                if field == "citations"
                else bool(getattr(paper, field))
                for paper in papers
            ),
        )
        for field in fields
    ]
    coverage.append(
        Coverage(
            field="references_available",
            total=len(papers),
            available=sum(paper.references_available for paper in papers),
        )
    )
    ranked = [paper for paper in papers if paper.cas_tier or paper.jcr_quartile or paper.ccf_rank]
    coverage.append(Coverage(field="venue_rank", total=len(papers), available=len(ranked)))

    def ranks(values) -> list[MetricBucket]:
        # In rank order (1 before 2, Q1 before Q2, A before B), not by count.
        return [MetricBucket(label=label, count=count) for label, count in sorted(Counter(v for v in values if v).items())]

    venues: dict[str, list] = {}
    for paper in ranked:
        if paper.venue:
            venues.setdefault(paper.venue, []).append(paper)
    ranked_venues = [
        RankedVenue(label=venue, count=len(group), cas_tier=group[0].cas_tier, cas_top=group[0].cas_top,
                    jcr_quartile=group[0].jcr_quartile, impact_factor=group[0].impact_factor, ccf_rank=group[0].ccf_rank)
        for venue, group in sorted(venues.items(), key=lambda item: (-len(item[1]), item[0]))[:100]
    ]
    category_totals["ranked_venues"] = len(venues)
    return MetricOverview(
        paper_count=len(papers),
        scored_count=sum(paper.scored for paper in papers),
        cited_paper_count=sum((paper.citations or 0) > 0 for paper in papers),
        citation_sum=sum(paper.citations or 0 for paper in papers),
        years=buckets("year", chronological=True),
        venues=buckets("venue"),
        providers=buckets("provider"),
        document_types=buckets("document_types", many=True),
        authors=buckets("authors", many=True),
        institutions=buckets("institutions", many=True),
        countries=buckets("countries", many=True),
        keywords=buckets("keywords", many=True),
        keyword_sources=buckets("keyword_source"),
        citation_sources=buckets("citation_source"),
        coverage=coverage,
        category_totals=category_totals,
        ranked_paper_count=len(ranked),
        cas_top_count=sum(paper.cas_top for paper in papers),
        cas_tiers=ranks(str(paper.cas_tier) if paper.cas_tier else "" for paper in papers),
        jcr_quartiles=ranks(paper.jcr_quartile for paper in papers),
        ccf_ranks=ranks(paper.ccf_rank for paper in papers),
        ranked_venues=ranked_venues,
    )
