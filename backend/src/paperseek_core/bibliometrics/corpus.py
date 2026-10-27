"""Normalize already-returned source metadata without fetching missing fields."""

from collections.abc import Iterable
import re
import unicodedata

from paperseek_core.bibliometrics.models import MetricCorpus, MetricPaper
from paperseek_core.document_identity import document_alias_keys, document_key
from paperseek_core.journal_metrics import JournalMetricsTable, normalize_issns
from paperseek_core.models import PaperRecord
from paperseek_core.retrieval_fusion import dedupe_documents
from paperseek_core.sources.normalization import normalize_doi


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def unique(values: Iterable[str], *, casefold: bool = False) -> list[str]:
    cleaned = (clean(value) for value in values if value)
    return sorted(
        {value.casefold() if casefold else value for value in cleaned if value}
    )


def citation_count(record: PaperRecord) -> int | None:
    """Read native presence before adapters' legacy zero/default conversions."""
    raw = record.raw
    value = next(
        (
            raw[key]
            for key in ("cited_by_count", "citationCount", "is-referenced-by-count")
            if raw.get(key) is not None
        ),
        None,
    )
    if record.provider == "wos":
        value = next(
            (
                entry.get("count")
                for entry in raw.get("citations") or []
                if isinstance(entry, dict) and entry.get("count") is not None
            ),
            None,
        )
    elif record.provider == "googlescholar":
        value = raw.get("citedBy")
        if isinstance(value, dict):
            value = next(
                (
                    value[key]
                    for key in ("total", "cites", "count", "value")
                    if value.get(key) is not None
                ),
                None,
            )
    elif value is None and record.citations:
        value = record.citations[0].count
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def compact_paper(record: PaperRecord) -> MetricPaper:
    raw = record.raw
    institutions, countries = [], []
    for authorship in raw.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            institutions.append(institution.get("display_name") or "")
            countries.append(institution.get("country_code") or "")
    for author in raw.get("author") or []:
        for affiliation in author.get("affiliation") or []:
            if isinstance(affiliation, dict):
                institutions.append(affiliation.get("name") or "")
    references = []
    for ref in raw.get("referenced_works") or []:
        identity = str(ref or "").rstrip("/").rsplit("/", 1)[-1].upper()
        if re.fullmatch(r"W\d+", identity):
            references.append("openalex:" + identity)
    for ref in raw.get("reference") or raw.get("references") or []:
        if not isinstance(ref, dict):
            continue
        doi = normalize_doi(
            ref.get("DOI")
            or ref.get("doi")
            or (ref.get("externalIds") or {}).get("DOI")
        )
        if doi:
            references.append("doi:" + doi.casefold())
        elif ref.get("paperId"):
            references.append("semanticscholar:" + str(ref["paperId"]).casefold())
    keywords = unique(
        record.keywords.author_keywords if record.keywords else [], casefold=True
    )
    keyword_source = record.provider + ":native" if keywords else ""
    if record.provider == "openalex" and keywords:
        keyword_source = (
            "openalex:keywords" if raw.get("keywords") else "openalex:concepts"
        )
    elif record.provider == "semanticscholar" and keywords:
        keyword_source = "semanticscholar:fields_of_study"
    # Zero is available data; absent citation counts must remain unknown.
    citation_value = citation_count(record)
    year = record.source.publish_year if record.source else None
    return MetricPaper(
        id=document_key(record),
        title=record.title,
        doi=normalize_doi(record.identifiers.doi).casefold()
        if record.identifiers
        else "",
        year=year if year and 1000 <= year <= 9999 else None,
        venue=clean(record.source.source_title) if record.source else "",
        provider=record.provider,
        document_types=unique(record.types),
        authors=unique(a.display_name or a.wos_standard for a in record.names.authors)
        if record.names
        else [],
        institutions=unique(institutions),
        countries=unique(countries),
        keywords=keywords,
        keyword_source=keyword_source,
        references=unique(references),
        references_available=any(
            key in raw and isinstance(raw[key], list)
            for key in ("referenced_works", "reference", "references")
        ),
        citations=max(0, int(citation_value)) if citation_value is not None else None,
        citation_source=record.provider if citation_value is not None else "",
        issns=normalize_issns(record.identifiers.issn, record.identifiers.eissn) if record.identifiers else [],
    )


def build_corpus(records: Iterable[PaperRecord], **metadata) -> MetricCorpus:
    return MetricCorpus(
        papers=[compact_paper(paper) for paper in dedupe_documents(list(records))],
        **metadata,
    )


def merge_corpus(
    corpus: MetricCorpus | None,
    records: Iterable[PaperRecord],
    *,
    scored_ids: set[str],
    **metadata,
) -> MetricCorpus:
    papers = {paper.id: paper for paper in corpus.papers} if corpus else {}
    # Match the retrieval engine's primary/title aliases when citation expansion
    # supplies a different provider ID for a paper already in the full corpus.
    aliases = {
        alias: paper.id
        for paper in papers.values()
        for alias in (
            paper.id,
            "title:" + " ".join(paper.title.strip().lower().split()),
        )
        if alias != "title:"
    }
    scored = {identity for identity in papers if identity in scored_ids}
    for record in records:
        paper = compact_paper(record)
        keys = document_alias_keys(record)
        identity = next((aliases[key] for key in keys if key in aliases), paper.id)
        papers.setdefault(identity, paper)
        for key in keys:
            aliases.setdefault(key, identity)
        if paper.id in scored_ids:
            scored.add(identity)
    return MetricCorpus(
        papers=[
            paper.model_copy(update={"scored": paper.id in scored})
            for paper in papers.values()
        ],
        **metadata,
    )


def rank_corpus(corpus: MetricCorpus, table: JournalMetricsTable | None) -> MetricCorpus:
    """Label each paper's venue with its CAS tier, JCR quartile and Impact Factor, and CCF class."""
    if not table:
        return corpus
    papers = []
    for paper in corpus.papers:
        labels = table.lookup(paper.issns, paper.venue)
        cas, jcr, ccf = labels.get("cas") or {}, labels.get("jcr") or {}, labels.get("ccf") or {}
        papers.append(paper.model_copy(update={
            "cas_tier": cas.get("tier"), "cas_top": bool(cas.get("top")), "jcr_quartile": jcr.get("quartile") or "",
            "impact_factor": jcr.get("impact_factor"), "ccf_rank": ccf.get("rank") or "",
        }))
    return corpus.model_copy(update={"papers": papers})
