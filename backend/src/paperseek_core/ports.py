from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from paperseek_core.models import (
    CheckpointDraft,
    CitationSourceRequest,
    CitationSourceResult,
    CoreProgressEvent,
    DecisionBatchResult,
    PaperRecord,
    RetrievalEmbeddingResult,
    RetrievalRerankResult,
)
from paperseek_core.sources.contracts import SourceCapabilities, SourceSearchRequest, SourceSearchResult


@runtime_checkable
class SourcePort(Protocol):
    name: str
    capabilities: SourceCapabilities

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult: ...


@runtime_checkable
class LLMPort(Protocol):
    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str: ...




@runtime_checkable
class CitationPort(Protocol):
    name: str
    capabilities: SourceCapabilities

    async def traverse_citations(self, request: CitationSourceRequest) -> CitationSourceResult: ...


@runtime_checkable
class EmbeddingPort(Protocol):
    async def embedding_scores(
        self,
        question: str,
        documents: list[PaperRecord],
        *,
        model: str | None = None,
    ) -> RetrievalEmbeddingResult: ...


@runtime_checkable
class RerankPort(Protocol):
    async def rerank(
        self,
        question: str,
        documents: list[PaperRecord],
    ) -> RetrievalRerankResult: ...


@runtime_checkable
class DecisionPort(Protocol):
    provider: str
    model: str
    base_url: str

    async def score(self, state: dict, questions: dict) -> DecisionBatchResult: ...


@runtime_checkable
class EnrichmentPort(Protocol):
    """Adds journal partitions and conference acceptance to final results; never fails a run."""

    async def annotate(self, documents: list[PaperRecord]) -> dict[str, dict[str, Any]]: ...


@runtime_checkable
class CancellationToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...

    async def wait(self) -> None: ...


class WorkflowObserver(Protocol):
    def on_event(self, event: CoreProgressEvent) -> None: ...

    # None disables recovery snapshots, including their construction/serialization.
    on_checkpoint: Callable[[CheckpointDraft], Awaitable[None] | None] | None
