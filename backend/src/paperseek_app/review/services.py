from __future__ import annotations

import json

from paperseek_app.models import RunStatus
from paperseek_app.ports import RunUnitOfWork
from paperseek_app.errors import ArtifactNotFound
from paperseek_app.services.run_queries import RunQueryService
from paperseek_app.execution import CancellationScope
from paperseek_app.review.models import (
    PaperSuggestion,
    PaperSuggestionRequest,
    PaperSuggestions,
    ReviewContent,
    ReviewCreate,
    ReviewDetail,
    ReviewError,
)
from paperseek_app.review.ports import ReviewStore
from paperseek_core.ports import LLMPort
from paperseek_core.review.evidence import fingerprint, normalize_abstract, snapshot
from paperseek_core.review.selection import Candidate, suggest_papers
from paperseek_core.review.structured import ReviewOutputError


class ReviewService:
    def __init__(self, store: ReviewStore, runs: RunUnitOfWork, queries: RunQueryService | None = None) -> None:
        self.store = store
        self.runs = runs
        self.queries = queries

    def create(self, request: ReviewCreate, idempotency_key: str) -> ReviewDetail:
        source_id = str(request.source_run_id)
        run = self.runs.get_run(source_id)
        if run.kind != "research" or run.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS}:
            raise ReviewError("review_source_not_completed", status=422)
        records = self.runs.get_results(source_id, uids=request.paper_uids)
        if {item.record.uid for item in records} != set(request.paper_uids):
            raise ReviewError("review_invalid_selection", status=422)
        papers = [snapshot(item.record, source_id, item.rank) for item in records]
        if sum(paper.has_abstract for paper in papers) < 2:
            raise ReviewError("review_insufficient_abstracts", status=422)
        if any(len(paper.abstract) > 60000 for paper in papers):
            raise ReviewError("review_abstract_too_large", status=422)
        citation_map = None
        if self.queries:
            try:
                citation_map = json.loads(self.queries.get_citation_map(source_id).content)
            except ArtifactNotFound:
                pass  # Imported legacy searches may have no citation artifact.
        content = ReviewContent(
            source_question=run.question, citation_map=citation_map,
            topic=request.topic, language=request.language, length=request.length,
            section_count=request.section_count or {"short": 1, "standard": 3, "detailed": 6}[request.length],
            paragraph_count=request.paragraph_count, paragraph_length=request.paragraph_length,
            corpus_hash=fingerprint([paper.model_dump() for paper in papers]), papers=papers,
            model_route=dict(run.config_snapshot.get("llm") or {}),
        )
        digest = fingerprint({**request.model_dump(mode="json"), "paper_uids": sorted(request.paper_uids)})
        return self.store.create_review(source_id, content, idempotency_key, digest)

    def suggestion_candidates(self, source_run_id: str) -> list[Candidate]:
        run = self.runs.get_run(source_run_id)
        if run.kind != "research" or run.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS}:
            raise ReviewError("review_source_not_completed", status=422)
        records = sorted(self.runs.get_results(source_run_id), key=lambda item: item.rank)
        return [Candidate(item.record.uid, item.record.title, normalize_abstract(item.record.abstract),
                          item.record.year or item.record.publish_year) for item in records]


async def suggest_review_papers(llm: LLMPort, request: PaperSuggestionRequest,
                                candidates: list[Candidate]) -> PaperSuggestions:
    try:
        picks = await suggest_papers(llm, CancellationScope(), topic=request.topic, language=request.language,
                                     candidates=candidates, count=request.count)
    except ReviewOutputError as exc:
        raise ReviewError("review_selection_failed", status=502) from exc
    return PaperSuggestions(items=[PaperSuggestion(uid=item.uid, theme=item.theme, reason=item.reason) for item in picks])
