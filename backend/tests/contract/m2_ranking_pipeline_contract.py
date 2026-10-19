from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import unittest

from paperseek_core.models import PaperRecord
from paperseek_core.ports import LLMPort
from paperseek_core.ranking_output import RankingOutputCodec
from paperseek_core.ranking_scoring import RankingScorer
from paperseek_core.errors import EmptyRankingBatchError
from paperseek_core.prompts import SYSTEM_RESULT_RANKING
from paperseek_core.models import RankingRequest
from paperseek_core.ranking import (
    RankingBatchScheduler,
    RankingPipeline,
)


ROOT = Path(__file__).resolve().parents[3]
M0_FIXTURES = ROOT / "tests" / "fixtures"
QUESTION = "How do transparent AI decision systems affect accountability in public agencies?"
RANKING_PROMPT_SHA256 = "BBD312897894056E8A3BF7C4BA3BA76E7333F169CDB615E67EF4C52CEC03C7FA"


def read_fixture(path: str) -> str:
    return (M0_FIXTURES / path).read_text(encoding="utf-8")


def documents() -> list[PaperRecord]:
    return [
        PaperRecord(
            uid="fixture:accountability",
            title="Accountable Automated Decisions in Public Agencies",
            abstract="A controlled study of accountability mechanisms for automated public decisions.",
            provider="arxiv",
        ),
        PaperRecord(
            uid="fixture:audit",
            title="Auditing Public-Sector Algorithms",
            abstract="A governance framework for auditing algorithms used by public institutions.",
            provider="arxiv",
        ),
        PaperRecord(
            uid="fixture:transparency",
            title="Transparency Practices for Decision Support",
            abstract="A general survey of transparency practices for decision-support systems.",
            provider="arxiv",
        ),
    ]


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


class RecordingObserver:
    def __init__(self) -> None:
        self.events = []
        self.checkpoints = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_checkpoint(self, checkpoint) -> None:
        self.checkpoints.append(checkpoint)


class GeneratedBatchLLM:
    def __init__(self) -> None:
        self.score_calls = 0
        self.reason_calls = 0

    async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
        prompt = messages[-1]["content"]
        if "integer-indexed papers" in messages[0]["content"]:
            self.score_calls += 1
            entries = []
            for block in prompt.split("\n\n"):
                index_match = re.search(r"^(\d+)\.$", block, re.MULTILINE)
                title_match = re.search(r"^\s*Title: Batch Paper (\d+)$", block, re.MULTILINE)
                if index_match and title_match:
                    entries.append([int(index_match.group(1)), int(title_match.group(1))])
            await asyncio.sleep(0)
            return json.dumps({"scores": entries})
        self.reason_calls += 1
        indexes = [int(value) for value in re.findall(r"^(\d+)\. Score:", prompt, re.MULTILINE)]
        return json.dumps({"reasons": [[index, f"reason-{index}"] for index in indexes]})


class FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
        del messages, temperature, max_tokens
        self.calls += 1
        raise RuntimeError("LLM API error (429): too many requests")


class RateLimitOnceLLM(GeneratedBatchLLM):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
        prompt = messages[-1]["content"]
        if "integer-indexed papers" in messages[0]["content"] and "Title: Batch Paper 1" in prompt:
            self.score_calls += 1
            if not self.failed:
                self.failed = True
                raise RuntimeError("LLM API error (429): too many requests")
        else:
            return await super().chat_json(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        entries = []
        for block in prompt.split("\n\n"):
            index_match = re.search(r"^(\d+)\.$", block, re.MULTILINE)
            title_match = re.search(r"^\s*Title: Batch Paper (\d+)$", block, re.MULTILINE)
            if index_match and title_match:
                entries.append([int(index_match.group(1)), int(title_match.group(1))])
        return json.dumps({"scores": entries})


class M2RankingPipelineContractTest(unittest.TestCase):
    def test_reason_output_repair_does_not_repeat_exhausted_transport_requests(self):
        from paperseek_core.errors import LLMRequestError

        class ExhaustedLLM:
            calls = 0

            async def chat_json(self, messages, **kwargs):
                self.calls += 1
                error = LLMRequestError('HTTP 429 after request retries')
                error.retry_details = {'http_status': 429, 'attempts': 5, 'retryable': True}
                raise error

        llm = ExhaustedLLM()
        records = [PaperRecord(uid=f'paper-{i}', title=f'Study {i}') for i in range(32)]
        scores = [{'uid': record.uid, 'score': 9} for record in records]
        scorer = RankingScorer(llm)
        result = asyncio.run(scorer.attach_reasons(QUESTION, records, scores, '{}'))
        self.assertEqual(llm.calls, 1)
        self.assertEqual(scorer.missing_reason_count, 32)
        self.assertEqual(scorer.reason_failures, [{'http_status': 429, 'attempts': 5, 'retryable': True}])
        self.assertTrue(all(not score['reasoning'] and score['score'] == 9 for score in result))

    def test_m0_prompt_codec_scores_and_reasons_are_exact(self) -> None:
        self.assertEqual(
            hashlib.sha256(SYSTEM_RESULT_RANKING.encode("utf-8")).hexdigest().upper(),
            RANKING_PROMPT_SHA256,
        )
        codec = RankingOutputCodec()
        records = documents()
        scores = codec.parse_scores(read_fixture("llm/ranking_scores.json"), records)
        reasons = codec.parse_reasons(
            read_fixture("llm/ranking_reasons.json"),
            [record.uid for record in records],
        )

        self.assertEqual(
            scores,
            [
                {"uid": "fixture:accountability", "score": 9.0, "reasoning": ""},
                {"uid": "fixture:audit", "score": 6.0, "reasoning": ""},
                {"uid": "fixture:transparency", "score": 3.0, "reasoning": ""},
            ],
        )
        self.assertEqual(
            reasons,
            {
                "fixture:accountability": "Directly addresses accountable AI use in public agencies.",
                "fixture:audit": "Provides relevant governance mechanisms but less direct outcome evidence.",
                "fixture:transparency": "Offers adjacent transparency context with weaker accountability focus.",
            },
        )

    def test_scorer_preserves_structured_calls_and_limited_repair(self) -> None:
        llm = FixtureLLM(
            read_fixture("llm/ranking_scores.json"),
            read_fixture("llm/ranking_reasons.json"),
        )
        scorer = RankingScorer(llm)

        scores = asyncio.run(scorer.score_batch(QUESTION, documents(), "{}"))
        scores = asyncio.run(scorer.attach_reasons(QUESTION, documents(), scores, "{}"))

        self.assertIsInstance(llm, LLMPort)
        self.assertEqual([entry["score"] for entry in scores], [9.0, 6.0, 3.0])
        self.assertEqual(
            [entry["reasoning"] for entry in scores],
            [
                "Directly addresses accountable AI use in public agencies.",
                "Provides relevant governance mechanisms but less direct outcome evidence.",
                "Offers adjacent transparency context with weaker accountability focus.",
            ],
        )
        self.assertEqual([call["max_tokens"] for call in llm.calls], [4096, 2048])
        self.assertTrue(all(call["temperature"] == 0.0 for call in llm.calls))

        invalid = read_fixture("llm/query_invalid.txt")
        failing = FixtureLLM(invalid, invalid)
        with self.assertRaisesRegex(EmptyRankingBatchError, "omitted 3/3 candidate"):
            asyncio.run(RankingScorer(failing).score_batch(QUESTION, documents(), "{}"))
        self.assertEqual(len(failing.calls), 2)

    def test_score_repair_only_requests_missing_candidates(self) -> None:
        records = documents()
        llm = FixtureLLM('{"scores":[[1,9],[3,3]]}', '{"scores":[[1,6]]}')
        scores = asyncio.run(RankingScorer(llm).score_batch(QUESTION, records, "{}"))
        self.assertEqual({item["uid"]: item["score"] for item in scores},
                         {records[0].uid: 9, records[1].uid: 6, records[2].uid: 3})
        prompt = str(llm.calls[1]["messages"])
        self.assertIn(records[1].title, prompt)
        self.assertNotIn(records[0].title, prompt)
        self.assertNotIn(records[2].title, prompt)

    def test_partial_scores_survive_repair_with_an_explicit_warning(self) -> None:
        llm = FixtureLLM('{"scores":[[1,9]]}', '{"scores":[[1,6]]}',
                         '{"reasons":[[1,"Direct evidence"],[2,"Adjacent evidence"]]}')
        observer = RecordingObserver()
        result = asyncio.run(RankingPipeline(llm, observer).run(
            RankingRequest(question=QUESTION, documents=documents())))
        self.assertEqual([item.score for item in result.items], [9, 6, None])
        self.assertEqual(result.items[-1].reasoning, "")
        self.assertEqual(result.warnings, ["ranking_partial_failure_local_order_retained"])
        degraded = [event for event in observer.events if event.event_type == "ranking.degraded"]
        self.assertEqual(degraded[-1].payload["failed_candidates"], 1)
        self.assertNotIn(documents()[2].title, str(llm.calls[-1]["messages"]))

    def test_later_score_chunk_failure_keeps_valid_earlier_scores(self) -> None:
        class InterruptedLLM:
            calls = 0

            async def chat_json(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return '{"scores":[[1,8]]}'
                raise RuntimeError("upstream unavailable")

        llm = InterruptedLLM()
        scores = asyncio.run(RankingScorer(llm).request_scores_in_chunks(
            QUESTION, documents(), "{}", chunk_size=1))
        self.assertEqual([(item["uid"], item["score"]) for item in scores], [(documents()[0].uid, 8)])
        self.assertEqual(llm.calls, 2)

    def test_invalid_reason_output_keeps_scores_without_inventing_reasons(self) -> None:
        llm = FixtureLLM(read_fixture("llm/query_invalid.txt"))
        scores = [
            {"uid": record.uid, "score": score, "reasoning": "stale"}
            for record, score in zip(documents(), (9.0, 6.0, 3.0))
        ]

        output = asyncio.run(RankingScorer(llm).attach_reasons(QUESTION, documents(), scores, "{}"))

        self.assertTrue(all(item["reasoning"] == "" for item in output))
        self.assertEqual(len(llm.calls), 1)

    def test_reasons_cut_off_mid_reply_are_kept_and_the_rest_requested_again(self) -> None:
        # The first reply stops mid-list, as a long reply does when it reaches its token budget.
        llm = FixtureLLM(
            '{"reasons":[[1,"First paper fits the question."],[2,"Second paper is rel',
            '{"reasons":[[1,"Second paper fits too."],[2,"Third paper is adjacent."]]}',
        )
        scores = [{"uid": record.uid, "score": score} for record, score in zip(documents(), (9.0, 6.0, 3.0))]
        scorer = RankingScorer(llm)

        output = asyncio.run(scorer.attach_reasons(QUESTION, documents(), scores, "{}"))

        self.assertEqual([item["reasoning"] for item in output],
                         ["First paper fits the question.", "Second paper fits too.", "Third paper is adjacent."])
        self.assertEqual(scorer.missing_reason_count, 0)
        self.assertEqual(len(llm.calls), 2)

    def test_pipeline_makes_missing_reasons_visible_without_inventing_text(self) -> None:
        llm = FixtureLLM(
            read_fixture("llm/ranking_scores.json"),
            read_fixture("llm/query_invalid.txt"),
        )
        observer = RecordingObserver()

        result = asyncio.run(
            RankingPipeline(llm, observer).run(
                RankingRequest(question=QUESTION, documents=documents())
            )
        )

        self.assertEqual([item.score for item in result.items], [9.0, 6.0, 3.0])
        self.assertTrue(all(item.reasoning == "" for item in result.items))
        self.assertEqual(result.warnings, ["ranking_reasons_missing"])
        degraded = [event for event in observer.events if event.event_type == "ranking.degraded"]
        self.assertEqual(degraded[-1].payload["code"], "ranking_reasons_missing")
        self.assertEqual(degraded[-1].payload["missing_reason_count"], 3)

    def test_pipeline_sorts_and_preserves_provenance_with_batch_checkpoints(self) -> None:
        records = [
            PaperRecord(uid=f"W{index}", title=f"Batch Paper {index}", provider="fixture")
            for index in range(1, 5)
        ]
        llm = GeneratedBatchLLM()
        observer = RecordingObserver()
        request = RankingRequest(
            question="batch ranking",
            documents=records,
            batch_size=2,
            concurrency=4,
            provenance_by_identity={
                record.uid: {"origins": ["retrieval"], "retrieval_score": index / 10}
                for index, record in enumerate(records, 1)
            },
        )

        result = asyncio.run(RankingPipeline(llm, observer).run(request))

        self.assertEqual([item.document.uid for item in result.items], ["W4", "W3", "W2", "W1"])
        self.assertEqual([item.score for item in result.items], [4.0, 3.0, 2.0, 1.0])
        self.assertEqual(result.items[0].provenance["retrieval_score"], 0.4)
        self.assertEqual(llm.score_calls, 2)
        self.assertEqual(llm.reason_calls, 1)
        # One checkpoint per scoring batch, then one with the written reasons.
        self.assertEqual(len(observer.checkpoints), 3)
        self.assertEqual(len(result.completed_batch_ids), 2)
        self.assertEqual(observer.events[0].event_type, "ranking.started")
        self.assertEqual(observer.events[-1].event_type, "ranking.completed")
        self.assertEqual(observer.events[0].payload["llm_gate_candidate_count"], 4)
        completed = [event.payload for event in observer.events if event.event_type == "ranking.batch_completed"]
        self.assertEqual(completed[-1]["completed_batches"], 2)
        self.assertEqual(completed[-1]["batch_count"], 2)
        self.assertEqual(completed[-1]["scored_count"], 4)
        self.assertEqual(completed[-1]["concurrency"], 4)
        self.assertEqual(result.warnings, [])

    def test_scoring_batches_and_reason_groups_run_side_by_side_with_progress(self) -> None:
        class ParallelLLM:
            def __init__(self) -> None:
                self.active = self.peak = 0
                self.kinds = []
                self.reasons_active = self.reasons_peak = 0

            async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(0.01)
                    prompt = messages[-1]["content"]
                    if "integer-indexed papers" in messages[0]["content"]:
                        self.kinds.append("score")
                        indexes = re.findall(r"^(\d+)\.$", prompt, re.MULTILINE)
                        return json.dumps({"scores": [[int(index), 9] for index in indexes]})
                    self.kinds.append("reasons")
                    self.reasons_active += 1
                    self.reasons_peak = max(self.reasons_peak, self.reasons_active)
                    await asyncio.sleep(0.01)
                    self.reasons_active -= 1
                    indexes = re.findall(r"^(\d+)\. Score:", prompt, re.MULTILINE)
                    return json.dumps({"reasons": [[int(index), f"reason-{index}"] for index in indexes]})
                finally:
                    self.active -= 1

        records = [PaperRecord(uid=f"W{index}", title=f"Paper {index}", provider="fixture") for index in range(1, 201)]
        llm = ParallelLLM()
        observer = RecordingObserver()
        result = asyncio.run(RankingPipeline(llm, observer).run(
            RankingRequest(question="parallel ranking", documents=records, concurrency=2)))

        self.assertEqual(llm.kinds.count("score"), 4)  # 200 papers in batches of 64.
        self.assertEqual(llm.kinds.count("reasons"), 7)  # Every paper scored 9 gets a reason, 32 per group.
        self.assertEqual(llm.peak, 2)
        self.assertEqual(llm.reasons_peak, 2)  # Reasons keep the level scoring finished at.
        self.assertTrue(all(item.reasoning for item in result.items))
        batches = [event.payload for event in observer.events if event.event_type == "ranking.batch_completed"]
        self.assertEqual([payload["completed_batches"] for payload in batches], [1, 2, 3, 4])
        reasons = [event.payload for event in observer.events if event.event_type == "ranking.reasons_progress"]
        self.assertEqual([(payload["completed_groups"], payload["group_count"]) for payload in reasons],
                         [(index, 7) for index in range(8)])
        self.assertTrue(all(payload["phase"] == "reasons" for payload in reasons))

    def test_cancelling_reasons_stops_every_group_in_flight(self) -> None:
        class BlockingLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.stopped = 0

            async def chat_json(self, messages, *, temperature=0.0, max_tokens=4096) -> str:
                self.calls += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.stopped += 1
                    raise
                return "{}"

        records = [PaperRecord(uid=f"W{index}", title=f"Paper {index}", provider="fixture") for index in range(1, 201)]
        scores = [{"uid": record.uid, "score": 9.0} for record in records]
        llm = BlockingLLM()

        async def run():
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(RankingScorer(llm).attach_reasons(QUESTION, records, scores, "{}", concurrency=4), 0.1)

        asyncio.run(run())
        self.assertEqual(llm.calls, 4)  # Seven groups wait for four slots.
        self.assertEqual(llm.stopped, 4)

    def test_rate_limit_retries_failed_batch_at_lower_concurrency(self) -> None:
        records = [
            PaperRecord(uid=f"W{index}", title=f"Batch Paper {index}", provider="fixture")
            for index in range(1, 5)
        ]
        llm = RateLimitOnceLLM()
        observer = RecordingObserver()

        result = asyncio.run(
            RankingPipeline(llm, observer).run(
                RankingRequest(
                    question="batch ranking",
                    documents=records,
                    batch_size=2,
                    concurrency=8,
                )
            )
        )

        self.assertTrue(llm.failed)
        self.assertEqual(llm.score_calls, 3)
        self.assertEqual([item.document.uid for item in result.items], ["W4", "W3", "W2", "W1"])
        self.assertEqual(result.warnings, [])
        failed = [event for event in observer.events if event.event_type == "ranking.batch_failed"]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].payload["rate_limited"])
        self.assertEqual(failed[0].payload["concurrency"], 8)
        self.assertEqual(len(observer.checkpoints), 3)

    def test_completed_batch_resumes_without_rescoring_and_checkpoints_only_new_work(self) -> None:
        records = [
            PaperRecord(uid=f"W{index}", title=f"Batch Paper {index}", provider="fixture")
            for index in range(1, 5)
        ]
        first_batch_id = RankingBatchScheduler.batch_id("llm_reranking", records[:2])
        llm = GeneratedBatchLLM()
        observer = RecordingObserver()

        result = asyncio.run(
            RankingPipeline(llm, observer).run(
                RankingRequest(
                    question="batch ranking",
                    documents=records,
                    batch_size=2,
                    concurrency=4,
                    resume_scores=[
                        {"uid": "W1", "score": 1, "reasoning": ""},
                        {"uid": "W2", "score": 2, "reasoning": ""},
                    ],
                    completed_batch_ids=[first_batch_id],
                )
            )
        )

        self.assertEqual(llm.score_calls, 1)
        self.assertEqual(llm.reason_calls, 1)
        self.assertEqual([item.document.uid for item in result.items], ["W4", "W3", "W2", "W1"])
        self.assertEqual(len(result.completed_batch_ids), 2)
        self.assertEqual(len(observer.checkpoints), 2)
        self.assertNotIn(first_batch_id, [checkpoint.idempotency_key for checkpoint in observer.checkpoints])

    def test_resume_after_reasons_were_written_makes_no_model_calls(self) -> None:
        records = [PaperRecord(uid=f"W{index}", title=f"Batch Paper {index}", provider="fixture") for index in range(1, 5)]
        observer = RecordingObserver()
        request = RankingRequest(question="batch ranking", documents=records, batch_size=2)
        asyncio.run(RankingPipeline(GeneratedBatchLLM(), observer).run(request))
        final = observer.checkpoints[-1].state
        llm = GeneratedBatchLLM()
        resumed = asyncio.run(RankingPipeline(llm, RecordingObserver()).run(request.model_copy(update={
            "resume_scores": final["ranking_scores"], "completed_batch_ids": final["completed_batch_ids"]})))
        self.assertEqual((llm.score_calls, llm.reason_calls), (0, 0))
        self.assertTrue(all(item.reasoning for item in resumed.items))

    def test_pipeline_matches_golden_search_ranking_and_candidate_provenance(self) -> None:
        golden = json.loads(read_fixture("golden_search/final_result.json"))
        expected = golden["ranked"]
        records = [
            PaperRecord(
                uid=item["uid"],
                title=item["title"],
                abstract=item["abstract"],
                provider=item["provider"],
            )
            for item in expected
        ]
        candidate_rows = [
            json.loads(line)
            for line in read_fixture("golden_search/candidate_pool.jsonl").splitlines()
            if line and json.loads(line).get("record_type") == "candidate"
        ]
        provenance = {
            row["openalex"]: {
                "candidate_id": row["candidate_id"],
                "origins": row["origins"],
                "retrieval_score": row["retrieval_score"],
                "lifecycle": row["lifecycle"],
            }
            for row in candidate_rows
        }
        llm = FixtureLLM(
            read_fixture("golden_search/llm_ranking_scores.json"),
            read_fixture("golden_search/llm_ranking_reasons.json"),
        )
        observer = RecordingObserver()

        result = asyncio.run(
            RankingPipeline(llm, observer).run(
                RankingRequest(
                    question=QUESTION,
                    documents=records,
                    provenance_by_identity=provenance,
                )
            )
        )

        self.assertEqual([item.document.uid for item in result.items], [item["uid"] for item in expected])
        self.assertEqual([item.score for item in result.items], [item["score"] for item in expected])
        self.assertEqual([item.reasoning for item in result.items], [item["reasoning"] for item in expected])
        self.assertEqual(
            [item.provenance["candidate_id"] for item in result.items],
            [
                "doi:10.5555/fixture.1001",
                "doi:10.5555/fixture.1003",
                "doi:10.5555/fixture.1002",
                "doi:10.5555/fixture.1004",
                "doi:10.5555/fixture.1005",
            ],
        )
        self.assertTrue(all(item.provenance["lifecycle"] for item in result.items))
        self.assertEqual(len(observer.checkpoints), 1)

    def test_failure_is_visible_and_falls_back_to_local_order(self) -> None:
        llm = FailingLLM()
        observer = RecordingObserver()

        result = asyncio.run(
            RankingPipeline(llm, observer).run(
                RankingRequest(question=QUESTION, documents=documents(), batch_size=256, concurrency=4)
            )
        )

        self.assertEqual([item.document.uid for item in result.items], [record.uid for record in documents()])
        self.assertTrue(all(item.score is None for item in result.items))
        self.assertEqual(result.warnings, ["ranking_failed_local_order_retained"])
        self.assertIn("ranking.degraded", [event.event_type for event in observer.events])
        self.assertEqual(observer.checkpoints, [])
        self.assertEqual(llm.calls, 1)

    def test_empty_input_is_deterministic_and_does_not_call_llm(self) -> None:
        llm = FailingLLM()
        observer = RecordingObserver()

        result = asyncio.run(
            RankingPipeline(llm, observer).run(RankingRequest(question=QUESTION, documents=[]))
        )

        self.assertEqual(result.items, [])
        self.assertEqual(result.scores, [])
        self.assertEqual(result.completed_batch_ids, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(llm.calls, 0)
        self.assertEqual(observer.events, [])

    def test_scheduler_and_codec_keep_existing_defaults_and_matching(self) -> None:
        scheduler = RankingBatchScheduler(batch_size_value="invalid", concurrency_value="invalid")
        self.assertEqual(scheduler.batch_size(300), 64)
        self.assertEqual(scheduler.concurrency_schedule(), [8, 4, 2, 1])
        self.assertEqual(RankingBatchScheduler(concurrency_value=1).concurrency_schedule(), [1])
        self.assertEqual(RankingBatchScheduler(concurrency_value=12).concurrency_schedule(), [12, 8, 4, 2, 1])

        codec = RankingOutputCodec()
        records = documents()[:2]
        parsed = codec.parse_scores(
            '```json\n{"scores":[[1,9],[1,3],[2,"NaN"],[3,8],[2,6]]}\n```',
            records,
        )
        matched, missing = codec.match_scores(
            records,
            [
                {"uid": "FIXTURE:ACCOUNTABILITY", "score": 9, "reasoning": "exact"},
                {"uid": "audit", "score": 7, "reasoning": "partial"},
            ],
        )
        self.assertEqual([entry["score"] for entry in parsed], [9.0, 6.0])
        self.assertEqual([entry["uid"] for entry in matched], [record.uid for record in records])
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
