from __future__ import annotations

from paperseek_core.models import (
    PaperIdentifiers,
    PaperRecord,
)


def record(uid: str, title: str | None = None, *, doi: str = "") -> PaperRecord:
    return PaperRecord(
        uid=uid,
        title=title or uid,
        identifiers=PaperIdentifiers(doi=doi),
        provider="fixture",
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.events = []
        self.checkpoints = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_checkpoint(self, checkpoint) -> None:
        self.checkpoints.append(checkpoint)

import asyncio

from paperseek_core.retrieval_fusion import RetrievalLane
from paperseek_core.sources.contracts import (
    SourceCapabilities,
    SourceSearchMetadata,
    SourceSearchRequest,
    SourceSearchResult,
)


class FixtureSource:
    name = "fixture"

    def __init__(
        self,
        outcomes: dict[tuple[str, int], object],
        *,
        capabilities: SourceCapabilities | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.capabilities = capabilities or SourceCapabilities(
            source=self.name,
            lanes=(RetrievalLane.RELEVANCE, RetrievalLane.IMPACT, RetrievalLane.RECENT),
            max_page_size=100,
            max_lane_limit=1000,
            lane_concurrency=2,
        )
        self.outcomes = dict(outcomes)
        self.delays = dict(delays or {})
        self.calls: list[SourceSearchRequest] = []
        self.active = 0
        self.max_active = 0

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        self.calls.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(request.lane, 0))
            outcome = self.outcomes.get((request.lane, request.page))
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                return SourceSearchResult(
                    metadata=SourceSearchMetadata(page=request.page, limit=request.limit),
                )
            return outcome
        finally:
            self.active -= 1

def source_result(records: list[PaperRecord], *, page: int = 1, limit: int = 100, total: int | None = None) -> SourceSearchResult:
    return SourceSearchResult(
        metadata=SourceSearchMetadata(
            total=len(records) if total is None else total,
            page=page,
            limit=limit,
        ),
        hits=records,
    )
