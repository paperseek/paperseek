from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from paperseek_core import prompts
from paperseek_core.errors import QueryGenerationError, QuerySourceError
from paperseek_core.models import (
    ConceptGroup,
    PaperRecord,
    QueryLoopBudget,
    QueryLoopRequest,
    QueryLoopResumeState,
    QueryLoopSnapshot,
    QueryView,
    SearchPlan,
)
from paperseek_core.ports import LLMPort, SourcePort
from paperseek_core.query_compiler import compile_query_view, validate_source_query
from paperseek_core.query_parsing import parse_search_plan
from paperseek_core.query_loop import QueryLoopPipeline
from paperseek_core.query_planning import QueryLoopController, QueryPlanner
from paperseek_core.sources.contracts import (
    SourceCapabilities,
    SourceError,
    SourceFailureKind,
    SourceSearchMetadata,
    SourceSearchRequest,
    SourceSearchResult,
)
from paperseek_core.sources.openalex import OpenAlexSource
from paperseek_core.sources.http import AsyncHttpTransport


ROOT = Path(__file__).resolve().parents[3]
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


class M2QueryLoopContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_source_filter_context_reaches_user_messages_without_changing_system_prompts(self) -> None:
        context = (
            "\nResearch field/context hint: distributed systems.\n"
            "Use this hint only to choose better source terms."
        )
        llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("llm/query_generated.json"),
        )
        planner = QueryPlanner(llm)
        plan = await planner.plan(
            QUESTION,
            portfolio_required=True,
            source_context=context,
        )
        await planner.propose(
            "arxiv",
            QUESTION,
            plan,
            operation="generate",
            source_context=context,
        )

        self.assertEqual(llm.calls[0]["messages"][0]["content"], prompts.SYSTEM_SEARCH_INTENT_ANALYSIS)
        self.assertEqual(llm.calls[1]["messages"][0]["content"], prompts.SYSTEM_ARXIV_QUERY_GENERATION)
        self.assertIn(context, llm.calls[0]["messages"][1]["content"])
        self.assertIn(context, llm.calls[1]["messages"][1]["content"])

    async def test_m0_prompt_hashes_search_plan_and_source_queries_are_exact(self) -> None:
        frozen = json.loads(read_text("baseline/frozen_search_semantics.json"))
        for name, expected in frozen["prompt_sha256"].items():
            with self.subTest(prompt=name):
                actual = getattr(prompts, name)
                self.assertEqual(
                    hashlib.sha256(actual.encode("utf-8")).hexdigest().upper(),
                    expected,
                )

        llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("llm/query_generated.json"),
            read_text("llm/query_broadened.json"),
            read_text("llm/query_narrowed.json"),
        )
        planner = QueryPlanner(llm)
        plan = await planner.plan(QUESTION, portfolio_required=True)
        generated = await planner.propose("arxiv", QUESTION, plan, operation="generate")
        broadened = await planner.propose(
            "arxiv",
            QUESTION,
            plan,
            operation="broaden",
            current_query=generated.query,
        )
        narrowed = await planner.propose(
            "arxiv",
            QUESTION,
            plan,
            operation="narrow",
            current_query=broadened.query,
        )

        self.assertIsInstance(llm, LLMPort)
        self.assertEqual(plan.model_dump(mode="json"), m0_plan().model_dump(mode="json"))
        self.assertEqual(generated.query, 'all:"AI governance" AND all:accountability')
        self.assertEqual(
            broadened.query,
            'all:("AI governance" OR "algorithmic governance") AND all:accountability',
        )
        self.assertEqual(
            narrowed.query,
            'all:"AI governance" AND all:accountability AND all:"public agency"',
        )
        self.assertEqual([call["max_tokens"] for call in llm.calls], [4096, 2048, 2048, 2048])
        self.assertTrue(all(call["temperature"] == 0.0 for call in llm.calls))

    async def test_structured_output_repair_is_bounded_and_failure_semantics_are_explicit(self) -> None:
        invalid_plan = FixtureLLM(
            read_text("llm/intent_invalid.txt"),
            read_text("llm/intent_invalid.txt"),
        )
        fallback = await QueryPlanner(invalid_plan).plan(QUESTION, portfolio_required=True)
        self.assertEqual(fallback.intent, QUESTION)
        self.assertEqual(fallback.query_views, [])
        self.assertIn("Structured planning fallback", fallback.adjustment_strategy)
        self.assertEqual(len(invalid_plan.calls), 2)

        repaired_llm = FixtureLLM(
            read_text("llm/query_malformed.json"),
            read_text("llm/query_broadened.json"),
        )
        repaired = await QueryPlanner(repaired_llm).propose(
            "arxiv",
            QUESTION,
            m0_plan(),
            operation="generate",
        )
        self.assertEqual(
            repaired.query,
            'all:("AI governance" OR "algorithmic governance") AND all:accountability',
        )
        self.assertEqual(repaired.warnings, ["query_output_repaired"])
        self.assertEqual(len(repaired_llm.calls), 2)

        failed_generation = FixtureLLM(
            read_text("llm/query_malformed.json"),
            read_text("llm/query_invalid.txt"),
        )
        with self.assertRaisesRegex(QueryGenerationError, "valid JSON with a query field"):
            await QueryPlanner(failed_generation).propose(
                "arxiv",
                QUESTION,
                m0_plan(),
                operation="generate",
            )
        self.assertEqual(len(failed_generation.calls), 2)

        failed_revision = FixtureLLM(
            read_text("llm/query_malformed.json"),
            read_text("llm/query_invalid.txt"),
        )
        previous = 'all:"AI governance" AND all:accountability'
        retained = await QueryPlanner(failed_revision).propose(
            "arxiv",
            QUESTION,
            m0_plan(),
            operation="broaden",
            current_query=previous,
        )
        self.assertEqual(retained.query, previous)
        self.assertEqual(retained.rationale, "")
        self.assertEqual(retained.warnings, ["query_revision_invalid_previous_query_retained"])

    async def test_query_compilers_and_validation_cover_all_provider_requests(self) -> None:
        plan = m0_plan()
        expected_sources = {
            "arxiv",
            "crossref",
            "googlescholar",
            "openalex",
            "paperhub",
            "pubmed",
            "semanticscholar",
            "wos",
        }
        compiled = {
            source: compile_query_view(source, plan.query_views[0]).query
            for source in expected_sources
        }
        self.assertEqual(set(compiled), expected_sources)
        for source, query in compiled.items():
            with self.subTest(source=source):
                validation = validate_source_query(source, query)
                self.assertTrue(validation.ok, validation.issue)
                request = SourceSearchRequest(
                    query=query,
                    limit=50 if source == "wos" else 100,
                    page=1,
                    lane="relevance",
                )
                self.assertEqual(request.query, query)
                self.assertEqual(request.page, 1)
                self.assertEqual(request.lane, "relevance")

        malformed = validate_source_query("openalex", '"AI governance" AND (accountability OR')
        self.assertFalse(malformed.ok)
        self.assertIn("unbalanced", malformed.issue)

    async def test_controller_preserves_broaden_narrow_accept_and_stop_thresholds(self) -> None:
        controller = QueryLoopController()
        base = dict(
            configured_iterations=5,
            adaptive_max_iterations=15,
            target_min=5,
            target_max=50,
            retrieval_pool_min=5,
            retrieval_pool_max=3000,
            query="AI governance accountability",
        )
        cases = [
            (QueryLoopSnapshot(iteration=1, total=25, **base), "accept", True),
            (QueryLoopSnapshot(iteration=1, total=1200, **base), "accept_pool", True),
            (QueryLoopSnapshot(iteration=1, total=31561, **base), "narrow", False),
            (QueryLoopSnapshot(iteration=1, total=0, **base), "broaden", False),
            (
                QueryLoopSnapshot(
                    iteration=3,
                    total=0,
                    history=[{"total": 0, "action": "broaden"}, {"total": 0, "action": "broaden"}],
                    **base,
                ),
                "empty",
                True,
            ),
            (QueryLoopSnapshot(iteration=15, total=3, **base), "accept_low", True),
            (QueryLoopSnapshot(iteration=15, total=31561, **base), "accept_high", True),
        ]
        for snapshot, action, terminal in cases:
            with self.subTest(action=action):
                decision = controller.decide(snapshot)
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.terminal, terminal)

        drift = controller.decide(
            QueryLoopSnapshot(
                iteration=1,
                total=25,
                hit_count=3,
                top_titles=["Marine biology survey", "Ocean ecology", "Fish habitats"],
                intent_terms=["algorithmic", "accountability", "public", "agencies", "transparent", "decision"],
                **base,
            )
        )
        self.assertEqual(drift.action, "narrow")
        self.assertIn("off-intent", drift.message)

    async def test_golden_portfolio_provider_requests_and_retrieval_handoff_are_exact(self) -> None:
        records = golden_records()
        source = SequenceSource(*(source_result(records, total=5) for _ in range(4)))
        llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("golden_search/llm_query.json"),
        )
        observer = RecordingObserver()

        result = await QueryLoopPipeline(llm, source, observer).run(
            QueryLoopRequest(question=QUESTION)
        )

        golden = json.loads(read_text("golden_search/final_result.json"))
        self.assertIsInstance(source, SourcePort)
        self.assertEqual(result.search_plan.model_dump(mode="json"), golden["search_plan"])
        self.assertEqual(result.final_query, golden["final_query"])
        self.assertEqual(result.terminal_status, "accept")
        self.assertEqual(
            [item.model_dump(mode="json") for item in result.query_view_selection],
            golden["query_view_selection"],
        )
        self.assertEqual(
            [item.model_dump(mode="json") for item in result.query_portfolio],
            golden["query_portfolio"],
        )
        self.assertEqual(
            [request.query for request in source.requests],
            [golden["final_query"], *[item["query"] for item in golden["query_portfolio"]]],
        )
        self.assertEqual([request.limit for request in source.requests], [100, 100, 100, 100])
        self.assertEqual(
            set(result.retrieval_request.supplemental_lanes),
            {
                "query_view:direct_governance",
                "query_view:audit_mechanisms",
                "query_view:evidence_reviews",
            },
        )
        self.assertEqual(result.retrieval_request.primary_hits, records)
        self.assertEqual(result.retrieval_request.primary_page.raw_count, len(records))
        self.assertEqual(result.retrieval_request.primary_page.limit, 100)
        self.assertEqual(result.retrieval_request.pool_max, 3000)
        self.assertEqual(result.retrieval_request.ranking_candidate_limit, 256)
        self.assertEqual(observer.checkpoints[-1].step_key, "query.complete")
        self.assertEqual(observer.checkpoints[-1].workflow_version, "m2.query-loop.v2")
        planned = next(event for event in observer.events if event.event_type == "query.planned")
        self.assertEqual(planned.payload["intent"], golden["search_plan"]["intent"])
        self.assertEqual(planned.payload["question_type"], golden["search_plan"]["question_type"])
        self.assertEqual(planned.payload["current_query"], golden["final_query"])
        self.assertEqual(planned.payload["query_views"], golden["search_plan"]["query_views"])
        request_started = next(event for event in observer.events if event.event_type == "query.request_started")
        self.assertEqual(request_started.payload["query"], golden["final_query"])
        decision = next(event for event in observer.events if event.event_type == "query.progress")
        self.assertEqual(
            {
                "query": decision.payload["query"],
                "action": decision.payload["decision"],
                "message": decision.payload["message"],
                "reason": decision.payload["reason"],
            },
            {
                "query": golden["final_query"],
                "action": golden["history"][0]["action"],
                "message": golden["history"][0]["message"],
                "reason": golden["history"][0]["controller_reason"],
            },
        )
        portfolio_events = [
            event.payload
            for event in observer.events
            if event.event_type == "query.portfolio_recorded"
        ]
        self.assertEqual(portfolio_events, golden["query_portfolio"])

    async def test_broadened_query_and_iteration_memory_feed_retrieval_without_repeating_final_query(self) -> None:
        recovered = PaperRecord(uid="recovered", title="Recovered evidence", provider="openalex")
        source = SequenceSource(
            source_result([], total=0),
            source_result([recovered], total=5),
        )
        llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("llm/query_generated.json"),
            read_text("llm/query_broadened.json"),
        )
        budget = QueryLoopBudget(query_portfolio_enabled=False)
        llm.responses.append(json.dumps({"core_concepts_preserved": True, "required_relation_preserved": True,
                                         "constraints_preserved": True}))

        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(
            QueryLoopRequest(question=QUESTION, budget=budget)
        )

        self.assertEqual([item.total for item in result.query_iteration_memory], [5])
        self.assertEqual([item.query for item in result.query_iteration_memory], [result.final_query])
        self.assertEqual(result.retrieval_request.supplemental_lanes, {})
        self.assertEqual(result.retrieval_request.primary_hits, [recovered])
        self.assertEqual(result.history[0].action, "empty_recovery")
        self.assertIn("query_empty_recovered", result.warnings)

    async def test_checkpoint_resume_skips_completed_primary_query_and_resumes_portfolio(self) -> None:
        records = golden_records()
        first_source = SequenceSource(source_result(records, total=5))
        first_observer = RecordingObserver(stop_phase="query_primary_complete")
        first_llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("golden_search/llm_query.json"),
        )

        with self.assertRaises(StopAfterCheckpoint):
            await QueryLoopPipeline(first_llm, first_source, first_observer).run(
                QueryLoopRequest(question=QUESTION)
            )
        checkpoint = first_observer.checkpoints[-1]
        resume = QueryLoopResumeState.model_validate(checkpoint.state)

        resumed_source = SequenceSource(*(source_result(records, total=5) for _ in range(3)))
        resumed_llm = FixtureLLM()
        result = await QueryLoopPipeline(resumed_llm, resumed_source, RecordingObserver()).run(
            QueryLoopRequest(question=QUESTION, resume=resume)
        )

        self.assertEqual(len(first_source.requests), 1)
        self.assertEqual(len(resumed_source.requests), 3)
        self.assertEqual(resumed_llm.calls, [])
        self.assertEqual(result.final_query, resume.current_query)
        self.assertEqual(result.primary_hits, records)
        self.assertEqual(result.retrieval_request.primary_page, resume.primary_page)
        self.assertEqual(len(result.query_portfolio), 3)

    async def test_invalid_resumed_query_raises_classified_error_before_source_request(self) -> None:
        source = SequenceSource()
        resume = QueryLoopResumeState(
            question=QUESTION,
            source=source.name,
            search_plan=SearchPlan(intent=QUESTION),
            current_query="",
        )
        with self.assertRaises(QueryGenerationError):
            await QueryLoopPipeline(FixtureLLM(), source, RecordingObserver()).run(
                QueryLoopRequest(question=QUESTION, resume=resume)
            )
        self.assertEqual(source.requests, [])

    async def test_source_failure_keeps_safe_status_and_timeout_category(self) -> None:
        for error, code in (
            (SourceError("openalex", "private response", status=429, body="api_key=secret"), "source_rate_limited"),
            (SourceError("openalex", "private URL", failure_kind=SourceFailureKind.FIRST_BYTE_TIMEOUT), "source_timeout"),
        ):
            with self.subTest(code=code):
                observer = RecordingObserver()
                resume = QueryLoopResumeState(question=QUESTION, source="openalex", search_plan=SearchPlan(intent=QUESTION), current_query="governance")
                with self.assertRaises(QuerySourceError) as caught:
                    await QueryLoopPipeline(FixtureLLM(), SequenceSource(error), observer).run(QueryLoopRequest(question=QUESTION, resume=resume))
                self.assertEqual(caught.exception.code, code)
                payload = observer.events[-1].payload
                self.assertEqual(payload["code"], code)
                if error.status:
                    self.assertEqual(payload["http_status"], error.status)
                else:
                    self.assertEqual(payload["failure_kind"], "first_byte_timeout")
                self.assertNotIn("private", str(observer.events))
                self.assertNotIn("secret", str(observer.events))

    async def test_primary_failure_is_stable_and_optional_portfolio_failure_is_visible(self) -> None:
        secret = "sensitive upstream response"
        source = SequenceSource(
            SourceError("openalex", "forbidden", status=403, body=secret),
        )
        observer = RecordingObserver()
        llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("golden_search/llm_query.json"),
        )
        with self.assertRaisesRegex(QuerySourceError, "primary query failed"):
            await QueryLoopPipeline(llm, source, observer).run(QueryLoopRequest(question=QUESTION))
        self.assertNotIn(secret, str(observer.events))
        self.assertEqual(observer.events[-1].event_type, "query.failed")
        self.assertEqual(observer.events[-1].payload["code"], "source_authentication_failed")
        self.assertEqual(observer.events[-1].payload["http_status"], 403)

        records = golden_records()
        portfolio_source = SequenceSource(
            source_result(records, total=5),
            RuntimeError(secret),
        )
        portfolio_observer = RecordingObserver()
        portfolio_llm = FixtureLLM(
            read_text("llm/intent_search_plan.json"),
            read_text("golden_search/llm_query.json"),
        )
        result = await QueryLoopPipeline(portfolio_llm, portfolio_source, portfolio_observer).run(
            QueryLoopRequest(
                question=QUESTION,
                budget=QueryLoopBudget(query_portfolio_max=1),
            )
        )
        self.assertEqual(result.primary_hits, records)
        self.assertEqual(result.query_portfolio[0].status, "failed")
        self.assertIn("query_portfolio_failed:direct_governance", result.warnings)
        self.assertNotIn(secret, str(portfolio_observer.events))


if __name__ == "__main__":
    unittest.main()
