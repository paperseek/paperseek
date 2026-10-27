"""Bounded reads for stores without native Unicode casefold queries.

Community implements these queries in SQLite. Hosted uses its scoped read port
in batches, preserving Python casefold semantics on PostgreSQL 16.
"""
from __future__ import annotations

from heapq import nsmallest

from paperseek_app.models import HistoryItem, HistoryPage, ResultPage
from paperseek_app.ports import RunUnitOfWork
from paperseek_core.acceptance import matches_filter as acceptance_matches
from paperseek_core.journal_metrics import matches_filter as journal_matches


def query_results(
    store: RunUnitOfWork, run_id: str, *, offset: int, limit: int,
    query: str | None, min_score: float | None, metadata: str | None, sort: str,
    journal: str | None = None, acceptance: str | None = None,
) -> ResultPage:
    needle = (query or "").strip().casefold()
    summary = dict(total_count=0, matched_count=0, scored_count=0, high_score_count=0, max_score=None)

    def matches(record):
        if min_score is not None and (record.score is None or record.score < min_score):
            return False
        field = {"abstract": "abstract", "doi": "doi", "pdf": "pdf_url"}.get(metadata)
        if field and not getattr(record, field).strip():
            return False
        if journal and not journal_matches(record.journal_metrics, journal):
            return False
        if acceptance and not acceptance_matches(record.acceptance, acceptance):
            return False
        return not needle or needle in " ".join([
            record.title, record.authors_text, record.abstract, record.doi,
            record.venue, record.source, record.keywords_text,
        ]).casefold()

    def sort_key(item):
        record = item.record
        if sort == "score":
            return record.score is None, -(record.score or 0), item.rank
        if sort == "citations":
            return -record.citation_count, item.rank
        if sort == "year":
            return -(record.publish_year or record.year or 0), item.rank
        if sort == "impact":
            return (-((record.journal_metrics.get("jcr") or {}).get("impact_factor") or -1),
                    -((record.journal_metrics.get("openalex") or {}).get("two_year_mean_citedness") or -1), item.rank)
        return (item.rank,)

    def matching_records():
        after_rank = 0
        while True:
            batch = store.get_results(run_id, after_rank=after_rank, limit=200)
            for item in batch:
                summary["total_count"] += 1
                score = item.record.score
                if score is not None:
                    summary["scored_count"] += 1
                    summary["high_score_count"] += int(score >= 5)
                    previous = summary["max_score"]
                    summary["max_score"] = score if previous is None else max(previous, score)
                if matches(item.record):
                    summary["matched_count"] += 1
                    yield item
            if len(batch) < 200:
                return
            after_rank = batch[-1].rank

    visible = nsmallest(offset + limit, matching_records(), key=sort_key)[offset:]
    return ResultPage(results=visible, **summary)


def search_history(store: RunUnitOfWork, *, query: str, offset: int, limit: int) -> HistoryPage:
    needle = query.strip().casefold()
    before_created_at = before_id = None
    count = 0
    visible = []
    while True:
        batch = store.list_history(before_created_at=before_created_at, before_id=before_id, limit=200)
        for item in batch:
            if needle not in " ".join([item.display_title, item.question, item.source, item.status, item.error_code]).casefold():
                continue
            if offset <= count < offset + limit:
                visible.append(HistoryItem(
                    id=item.id, display_title=item.display_title, question=item.question, source=item.source, status=item.status,
                    result_count=item.result_count, error_code=item.error_code, warnings=item.warnings,
                    created_at=item.created_at, finished_at=item.finished_at,
                ))
            count += 1
        if len(batch) < 200:
            return HistoryPage(items=visible, matched_count=count)
        before_created_at, before_id = batch[-1].created_at, batch[-1].id
