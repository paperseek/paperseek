"""Contracts for JSON final-content extraction and ranking score validity.

Covers the 2026-09-21 production failure where 256 candidates were sent for
LLM scoring, zero valid scores were produced, and every result surfaced as a
fake zero score. These tests pin four semantics:

1. JSON calls consume only the final message content; a reasoning trace is
   never accepted as the JSON answer, even after the response_format retry.
2. Truncated and empty final content are classified distinctly.
3. Papers whose batch failed keep score=None (unscored) while a legitimate
   zero from a successful batch stays 0.
4. CSV export renders unscored as an empty cell and a legal zero as "0".
"""

from __future__ import annotations

import json
import unittest

import httpx

from paperseek_core.errors import LLMRequestError
from paperseek_core.execution import NEVER_CANCELLED
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.models import FinalPaperResult, PaperRecord
from paperseek_core.ranking import RankingPipeline, RankingRequest


def _adapter(handler) -> HttpLLMAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpLLMAdapter(
        client=client,
        cancellation=NEVER_CANCELLED,
        api_type="openai_chat",
        base_url="http://llm.fixture",
        model="fixture-model",
        api_key="fixture-key",
    )


def _payload(content: str | None, *, finish_reason: str = "stop", reasoning: str | None = None) -> dict:
    message: dict[str, object] = {"content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


class TestJsonFinalContentExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_final_content_is_returned(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload('{"scores": [[1, 8]]}'))

        result = await _adapter(handler).chat_json([{"role": "user", "content": "score"}])
        self.assertEqual(result, '{"scores": [[1, 8]]}')

    async def test_reasoning_content_is_not_accepted_as_json_answer(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload("", reasoning='{"scores": [[1, 8]]}'))

        with self.assertRaises(LLMRequestError) as raised:
            await _adapter(handler).chat_json([{"role": "user", "content": "score"}])
        self.assertEqual(raised.exception.code, "provider_empty_content")

    async def test_truncated_final_content_is_classified(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload("", finish_reason="length"))

        with self.assertRaises(LLMRequestError) as raised:
            await _adapter(handler).chat_json([{"role": "user", "content": "score"}])
        self.assertEqual(raised.exception.code, "provider_output_truncated")
        self.assertIn("truncat", str(raised.exception).casefold())

    async def test_response_format_retry_still_forbids_reasoning_fallback(self) -> None:
        requests: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(400, json={"error": {"message": "response_format unsupported"}})
            return httpx.Response(200, json=_payload(None, reasoning="chain of thought"))

        with self.assertRaises(LLMRequestError) as raised:
            await _adapter(handler).chat_json([{"role": "user", "content": "score"}])
        self.assertEqual(raised.exception.code, "provider_empty_content")
        self.assertEqual(len(requests), 2)
        self.assertIn("response_format", requests[0])
        self.assertNotIn("response_format", requests[1])

    async def test_json_calls_suppress_or_minimize_thinking(self) -> None:
        bodies: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json=_payload('{"scores": [[1, 8]]}'))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def call(base_url: str, model: str) -> None:
            adapter = HttpLLMAdapter(
                client=client,
                cancellation=NEVER_CANCELLED,
                api_type="openai_chat",
                base_url=base_url,
                model=model,
                api_key="fixture-key",
            )
            await adapter.chat_json([{"role": "user", "content": "score"}])

        await call("https://api.deepseek.com", "deepseek-v4-flash")
        self.assertEqual(bodies[0].get("thinking"), {"type": "disabled"})

        await call("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-max")
        self.assertEqual(bodies[1].get("enable_thinking"), False)

        await call("https://api.openai.com/v1", "gpt-5-mini")
        self.assertEqual(bodies[2].get("reasoning_effort"), "minimal")

        await call("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3-flash")
        self.assertEqual(bodies[3].get("reasoning_effort"), "low")

        await call("http://llm.fixture", "plain-model")
        self.assertNotIn("thinking", bodies[4])
        self.assertNotIn("enable_thinking", bodies[4])
        self.assertNotIn("reasoning_effort", bodies[4])

        await call("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3-super-120b-a12b")
        self.assertEqual(bodies[5].get("chat_template_kwargs"), {"enable_thinking": False})

    async def test_rejected_thinking_control_is_dropped_on_retry(self) -> None:
        bodies: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            if len(bodies) == 1:
                return httpx.Response(400, json={"error": {"message": "unknown parameter"}})
            return httpx.Response(200, json=_payload('{"scores": [[1, 8]]}'))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = HttpLLMAdapter(
            client=client,
            cancellation=NEVER_CANCELLED,
            api_type="openai_chat",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="fixture-key",
        )
        result = await adapter.chat_json([{"role": "user", "content": "score"}])
        self.assertEqual(result, '{"scores": [[1, 8]]}')
        self.assertIn("thinking", bodies[0])
        self.assertNotIn("thinking", bodies[1])
        self.assertNotIn("response_format", bodies[1])


def _documents(count: int = 4) -> list[PaperRecord]:
    return [
        PaperRecord(
            uid=f"fixture:{index}",
            title=f"Fixture paper {index}",
            abstract=f"Abstract of fixture paper {index}.",
            provider="arxiv",
        )
        for index in range(1, count + 1)
    ]


class _ScriptedLLM:
    def __init__(self, *actions: object) -> None:
        self.actions = list(actions)

    async def chat_json(self, messages, *, temperature: float = 0.0, max_tokens: int = 4096) -> str:
        if not self.actions:
            raise AssertionError("scripted LLM actions were exhausted")
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return str(action)


class _RecordingObserver:
    def __init__(self) -> None:
        self.events = []
        self.checkpoints = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_checkpoint(self, draft) -> None:
        self.checkpoints.append(draft)


_PROVIDER_FAILURE = LLMRequestError(
    "The configured LLM credential is unavailable.", code="provider_authentication_failed"
)


class TestRankingScoreValidity(unittest.IsolatedAsyncioTestCase):
    async def test_all_batches_failed_keeps_every_paper_unscored(self) -> None:
        documents = _documents(4)
        observer = _RecordingObserver()
        pipeline = RankingPipeline(_ScriptedLLM(_PROVIDER_FAILURE), observer)
        result = await pipeline.run(
            RankingRequest(
                question="fixture question",
                documents=documents,
                batch_size=4,
                concurrency=1,
            )
        )
        self.assertEqual(len(result.items), 4)
        self.assertTrue(all(item.score is None for item in result.items))
        self.assertIn("ranking_failed_local_order_retained", result.warnings)
        degraded = [event for event in observer.events if event.event_type == "ranking.degraded"]
        self.assertTrue(degraded)
        self.assertEqual(degraded[-1].payload["code"], "ranking_failed_local_order_retained")
        self.assertEqual(degraded[-1].payload["error_code"], "provider_authentication_failed")

    async def test_partial_failure_distinguishes_scored_zero_and_unscored(self) -> None:
        documents = _documents(4)
        scores = json.dumps({"scores": [[1, 8], [2, 0]]})
        reasons = json.dumps({"reasons": [[1, "r1"], [2, "r2"], [3, "r3"], [4, "r4"]]})
        observer = _RecordingObserver()
        pipeline = RankingPipeline(_ScriptedLLM(scores, _PROVIDER_FAILURE, reasons), observer)
        result = await pipeline.run(
            RankingRequest(
                question="fixture question",
                documents=documents,
                batch_size=2,
                concurrency=2,
            )
        )
        self.assertEqual(len(result.items), 4)
        ordered_scores = [item.score for item in result.items]
        self.assertEqual(ordered_scores[0], 8)
        self.assertEqual(ordered_scores[1], 0)
        self.assertIsNone(ordered_scores[2])
        self.assertIsNone(ordered_scores[3])
        self.assertIn("ranking_partial_failure_local_order_retained", result.warnings)
        completed = [event for event in observer.events if event.event_type == "ranking.completed"]
        self.assertTrue(completed)
        self.assertEqual(completed[-1].payload["scored_count"], 2)
        self.assertEqual(completed[-1].payload["candidate_count"], 4)


class TestScoreExportRendering(unittest.TestCase):
    def test_csv_renders_unscored_empty_and_legal_zero_as_zero(self) -> None:
        csv_text = FinalResultAssembler._csv(
            [
                FinalPaperResult(rank=1, title="unscored", score=None),
                FinalPaperResult(rank=2, title="legal zero", score=0),
            ]
        )
        rows = csv_text.splitlines()
        self.assertEqual(rows[1].split(",")[1], '""')
        self.assertEqual(rows[2].split(",")[1], '"0"')

    def test_number_helper(self) -> None:
        self.assertEqual(FinalResultAssembler._number(None), "")
        self.assertEqual(FinalResultAssembler._number(0), "0")
        self.assertEqual(FinalResultAssembler._number(8.5), "8.5")


if __name__ == "__main__":
    unittest.main()
