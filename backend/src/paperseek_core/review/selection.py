"""Topic-coverage paper selection before a review is created; the user still confirms every choice."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from paperseek_core.ports import CancellationToken, LLMPort
from paperseek_core.review.models import MAX_REVIEW_PAPERS, _DraftModel
from paperseek_core.review.structured import structured_call

# Enough of each abstract to tell themes and designs apart within one bounded candidate pool.
_ABSTRACT_PREVIEW = 300
MAX_CANDIDATES = MAX_REVIEW_PAPERS


@dataclass(frozen=True, slots=True)
class Candidate:
    uid: str
    title: str
    abstract: str
    year: int | None = None


class _Pick(_DraftModel):
    id: str = Field(max_length=16)
    theme: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=300)


class _Selection(_DraftModel):
    papers: list[_Pick] = Field(default_factory=list, max_length=MAX_CANDIDATES)


@dataclass(frozen=True, slots=True)
class Suggestion:
    uid: str
    theme: str
    reason: str


async def suggest_papers(llm: LLMPort, cancellation: CancellationToken, *, topic: str, language: str,
                         candidates: list[Candidate], count: int) -> list[Suggestion]:
    pool = [item for item in candidates if item.abstract.strip()][:MAX_CANDIDATES]
    if len(pool) <= count:
        return [Suggestion(item.uid, "", "") for item in pool]
    ids = {f"c{index}": item for index, item in enumerate(pool, 1)}

    def validate(result: _Selection) -> None:
        chosen = [item.id for item in result.papers]
        unknown = [item for item in chosen if item not in ids]
        if unknown:
            raise ValueError("Only use candidate IDs from the input: " + ", ".join(unknown[:10]))
        if len(set(chosen)) != len(chosen):
            raise ValueError("Pick each candidate at most once.")
        if len(chosen) != count:
            raise ValueError(f"Pick exactly {count} candidates; you picked {len(chosen)}.")

    zh = language == "zh"
    result = await structured_call(
        llm, cancellation, "paper_selection",
        f"Pick exactly {count} candidates for a literature review on the topic. Aim for coverage: "
        "include every distinct theme, method, population or setting the candidates offer for this topic, "
        "and prefer the most informative study for each before adding similar ones. Leave out candidates "
        "that are off-topic or near-duplicates of a picked one. Candidates are in search-rank order; rank "
        "is only a tie-breaker. For each pick, give a short theme label and a one-sentence reason. "
        + ("Write theme and reason in Chinese." if zh else "Write theme and reason in English."),
        {"topic": topic, "count": count, "candidates": [
            {"id": key, "title": item.title, "year": item.year, "abstract": item.abstract[:_ABSTRACT_PREVIEW]}
            for key, item in ids.items()]},
        _Selection, max_tokens=min(24000, 400 + count * 90), validate=validate, language=language,
    )
    return [Suggestion(ids[item.id].uid, item.theme, item.reason) for item in result.papers]
