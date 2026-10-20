from __future__ import annotations

import json
from pathlib import Path

from paperseek_core.models import (
    PaperRecord,
    SearchPlan,
)
from paperseek_core.query_parsing import parse_search_plan
from paperseek_core.sources.contracts import (
    SourceCapabilities,
    SourceSearchMetadata,
    SourceSearchRequest,
    SourceSearchResult,
)
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openalex import OpenAlexSource

ROOT = Path(__file__).resolve().parents[4]


M0 = ROOT / "tests" / "fixtures"


QUESTION = "How do transparent AI decision systems affect accountability in public agencies?"


def read_text(path: str) -> str:
    return (M0 / path).read_text(encoding="utf-8")


class FixtureLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
        if not self.responses:
            raise AssertionError("fixture response sequence was exhausted")
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self.responses.pop(0)


class SequenceSource:
    name = "openalex"
    capabilities = SourceCapabilities(
        source="openalex",
        lanes=("relevance", "impact", "recent"),
        max_page_size=200,
        max_lane_limit=1000,
        lane_concurrency=2,
    )

    def __init__(self, *responses: SourceSearchResult | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[SourceSearchRequest] = []

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("fixture source response sequence was exhausted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RecordingObserver:
    def __init__(self, stop_phase: str = "") -> None:
        self.events = []
        self.checkpoints = []
        self.stop_phase = stop_phase

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_checkpoint(self, checkpoint) -> None:
        self.checkpoints.append(checkpoint)
        if self.stop_phase and checkpoint.state.get("phase") == self.stop_phase:
            raise StopAfterCheckpoint(self.stop_phase)


class StopAfterCheckpoint(RuntimeError):
    pass


def source_result(records: list[PaperRecord], *, total: int) -> SourceSearchResult:
    return SourceSearchResult(
        metadata=SourceSearchMetadata(total=total, page=1, limit=100),
        hits=records,
    )


def golden_records() -> list[PaperRecord]:
    payload = json.loads(read_text("golden_search/source_responses.json"))
    mapper = OpenAlexSource(transport=AsyncHttpTransport())
    return [mapper._to_record(work) for work in payload["relevance"]]


def m0_plan() -> SearchPlan:
    plan = parse_search_plan(read_text("llm/intent_search_plan.json"), QUESTION)
    assert plan is not None
    return plan
