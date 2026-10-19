from __future__ import annotations

import json
import asyncio
import unittest

from paperseek_core.models import (
    PaperRecord,
    QueryHistoryEntry,
    QueryLoopBudget,
    QueryLoopRequest,
    QueryLoopResumeState,
    QueryLoopSnapshot,
    SearchPlan,
)
from paperseek_core.query_loop import QueryLoopPipeline
from paperseek_core.query_planning import QueryLoopController
from paperseek_core.query_history import (
    query_fingerprint,
    refinement_context,
    repeated_attempt,
)
from paperseek_core.sources.contracts import SourceError
from m2_query_loop_contract import (
    FixtureLLM,
    RecordingObserver,
    SequenceSource,
    StopAfterCheckpoint,
    source_result,
)


class QueryRefinementContract(unittest.IsolatedAsyncioTestCase):
    async def run_queries(self, queries, totals, *, titles=None, budget=None, plan=None):
        records = [
            PaperRecord(uid=f"paper-{i}", title=titles[i] if titles else f"Evidence {i}", provider="openalex")
            for i in range(len(totals))
        ]
        source = SequenceSource(
            *(
                source_result([record] if total else [], total=total)
                for record, total in zip(records, totals)
            )
        )
        responses = []
        for index, query in enumerate(queries[1:], 1):
            # A repeated proposal is asked for once more; the fixture model repeats it again.
            responses += [json.dumps({"query": query})] * (2 if query in queries[:index] else 1)
        llm = FixtureLLM(*responses)
        observer = RecordingObserver()
        result = await QueryLoopPipeline(llm, source, observer).run(
            QueryLoopRequest(
                question="ecosystem resilience",
                budget=budget or QueryLoopBudget(query_portfolio_enabled=False),
                resume=QueryLoopResumeState(
                    question="ecosystem resilience",
                    source="openalex",
                    search_plan=plan or SearchPlan(intent="ecosystem resilience"),
                    current_query=queries[0],
                ),
            )
        )
        return result, source, llm, observer

    async def test_repeat_after_widening_selects_best_earlier_query_and_its_page(self):
        queries = ['"open source" AND funding', '"open source" AND funding AND software',
                   '"open source" AND funding AND "venture capital"',
                   '"open source" AND funding AND "software development"']
        result, source, _, observer = await self.run_queries(
            [*queries, queries[2]], [714288, 536685, 6123, 48078],
        )
        self.assertEqual(len(source.requests), 4)
        self.assertEqual(result.final_query, queries[2])
        self.assertEqual(result.total, 6123)
        self.assertEqual(result.primary_hits[0].uid, "paper-2")
        self.assertEqual(result.retrieval_request.query, queries[2])
        self.assertEqual(result.retrieval_request.primary_page.total, 6123)
        self.assertEqual(result.iterations, 4)
        selection = next(e for e in observer.events if e.event_type == "query.selected")
        self.assertEqual(selection.payload["selected_iteration"], 3)
        self.assertEqual(selection.payload["previous_total"], 48078)
        stopped = next(e for e in observer.events if e.event_type == "query.refinement_stopped")
        self.assertEqual(stopped.payload["query"], queries[2])
        self.assertEqual(stopped.payload["total"], 6123)
        self.assertTrue(any(row.query == queries[3] and row.total == 48078 for row in result.history))

    async def test_stall_and_hard_limit_also_select_earlier_query(self):
        for totals in ([20000, 8000, 4000, 6000, 7000], [20000, 8000, 4000, 3001, 7000]):
            with self.subTest(totals=totals):
                result, _, _, _ = await self.run_queries(
                    [f"ecosystem AND term{i}" for i in range(5)], totals,
                    budget=QueryLoopBudget(query_portfolio_enabled=False, max_iterations=5, adaptive_max_iterations=5),
                )
                best = min(range(len(totals)), key=totals.__getitem__)
                self.assertEqual(result.total, totals[best])
                self.assertEqual(result.primary_hits[0].uid, f"paper-{best}")

    async def test_smaller_off_intent_query_does_not_replace_aligned_results(self):
        aligned = [PaperRecord(uid=f"aligned-{i}", title="Open source software funding", provider="openalex") for i in range(3)]
        drifted = [PaperRecord(uid=f"drifted-{i}", title="Cancer treatment clinical trial", provider="openalex") for i in range(3)]
        source = SequenceSource(source_result(aligned, total=48078), source_result(drifted, total=6123))
        observer = RecordingObserver(stop_phase="query_primary_complete")
        request = QueryLoopRequest(question="open source software funding",
            budget=QueryLoopBudget(query_portfolio_enabled=False),
            resume=QueryLoopResumeState(question="open source software funding", source="openalex",
                search_plan=SearchPlan(intent="open source software funding", core_concepts=["open source", "software", "funding"]),
                current_query='"open source" AND funding'))
        with self.assertRaises(StopAfterCheckpoint):
            await QueryLoopPipeline(FixtureLLM(*['{"query":"cancer"}'] * 3), source, observer).run(request)
        resumed = request.model_copy(update={"resume":QueryLoopResumeState.model_validate(observer.checkpoints[-1].state)})
        result = await QueryLoopPipeline(FixtureLLM(), SequenceSource(), RecordingObserver()).run(resumed)
        self.assertEqual(result.total, 48078)
        self.assertEqual(result.primary_hits, aligned)
        self.assertEqual(result.retrieval_request.primary_page.total, 48078)

    async def test_cycles_stop_before_repeating_source_request_and_preserve_candidates(
        self,
    ):
        for queries, totals in (
            (["ecosystem", "ecosystem"], [13151]),
            (["ecosystem", "ecosystem AND resilience", "ecosystem"], [13151, 4381]),
            (
                [
                    "ecosystem",
                    "ecosystem AND resilience",
                    "ecosystem AND robustness",
                    "ecosystem",
                ],
                [13151, 4381, 4200],
            ),
        ):
            with self.subTest(queries=queries):
                result, source, llm, observer = await self.run_queries(queries, totals)
                self.assertEqual(len(source.requests), len(totals))
                self.assertEqual(len(llm.calls), len(totals) + 1)
                self.assertIn("already executed", str(llm.calls[-1]["messages"]))
                self.assertEqual(result.final_query, queries[-2])
                self.assertIn("query_repeated", result.warnings)
                self.assertEqual(result.terminal_status, "accept_high")
                self.assertEqual(
                    len(result.retrieval_request.supplemental_lanes), len(totals) - 1
                )
                stopped = next(
                    e
                    for e in observer.events
                    if e.event_type == "query.refinement_stopped"
                )
                self.assertEqual(stopped.payload["code"], "query_repeated")
                self.assertEqual(stopped.payload["query"], result.final_query)

    async def test_zero_result_history_reaches_model_and_nonempty_fallback_survives_cycle(
        self,
    ):
        result, source, llm, _ = await self.run_queries(
            ["ecosystem", "ecosystem AND absent", "ecosystem"], [4381, 0]
        )
        prompt = llm.calls[-1]["messages"][1]["content"]
        self.assertIn('"total": 0', prompt)
        self.assertIn('"total": 4381', prompt)
        self.assertIn('"action": "broaden"', prompt)
        self.assertIn("Evidence 0", llm.calls[0]["messages"][1]["content"])
        self.assertEqual(result.final_query, "ecosystem")
        self.assertEqual(result.total, 4381)
        self.assertEqual(len(result.primary_hits), 1)
        self.assertEqual(len(source.requests), 2)
        self.assertTrue(
            any(
                row.total == 0 and row.query == "ecosystem AND absent"
                for row in result.history
            )
        )

    async def test_a_repeated_proposal_is_asked_for_once_more(self):
        source = SequenceSource(source_result([PaperRecord(uid="a", title="Evidence", provider="openalex")], total=13151),
                                source_result([PaperRecord(uid="b", title="Evidence", provider="openalex")], total=25))
        llm = FixtureLLM('{"query": "ecosystem"}', '{"query": "ecosystem AND resilience"}')
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(QueryLoopRequest(
            question="ecosystem resilience", budget=QueryLoopBudget(query_portfolio_enabled=False),
            resume=QueryLoopResumeState(question="ecosystem resilience", source="openalex",
                                        search_plan=SearchPlan(intent="ecosystem resilience"), current_query="ecosystem")))
        self.assertEqual([request.query for request in source.requests], ["ecosystem", "ecosystem AND resilience"])
        self.assertEqual(result.total, 25)
        self.assertNotIn("query_repeated", result.warnings)

    async def test_different_queries_with_same_count_can_still_improve(self):
        result, source, _, _ = await self.run_queries(
            ["ecosystem", "ecosystem AND resilience", "ecosystem AND robustness"],
            [4381, 4381, 25],
        )
        self.assertEqual(len(source.requests), 3)
        self.assertEqual(result.total, 25)
        self.assertNotIn("query_repeated", result.warnings)

    def test_history_is_bounded_for_prompt_but_complete_for_repeat_detection(self):
        history = [
            QueryHistoryEntry(
                iteration=i + 1,
                query=f"query-{i}",
                total=i,
                action="broaden",
                next_query=f"query-{i + 1}",
            )
            for i in range(9)
        ]
        context = refinement_context(history, ["sample title"] * 8)
        self.assertNotIn('"query-0"', context)
        self.assertIn('"query-8"', context)
        self.assertEqual(context.count('"iteration"'), 6)
        self.assertEqual(context.count('"sample title"'), 3)
        self.assertEqual(repeated_attempt("query-0", history).total, 0)
        self.assertIsNone(repeated_attempt("query-9", history))
        self.assertEqual(
            query_fingerprint(' A  AND "two  spaces" '), 'A AND "two  spaces"'
        )
        self.assertNotEqual(
            query_fingerprint('A AND "two  spaces"'),
            query_fingerprint('A AND "two spaces"'),
        )
        self.assertNotEqual(query_fingerprint("A AND B"), query_fingerprint("A OR B"))
        self.assertNotEqual(query_fingerprint("GeneA"), query_fingerprint("genea"))

    async def test_syntax_repair_cannot_reissue_a_completed_query(self):
        record = PaperRecord(uid="retained", title="Evidence", provider="openalex")
        source = SequenceSource(
            source_result([record], total=4381),
            SourceError("openalex", "syntax", status=400),
        )
        llm = FixtureLLM(
            '{"query": "ecosystem AND unsupported"}', '{"query": "ecosystem"}'
        )
        resume = QueryLoopResumeState(
            question="ecosystem",
            source="openalex",
            search_plan=SearchPlan(intent="ecosystem"),
            current_query="ecosystem",
        )
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(
            QueryLoopRequest(
                question="ecosystem",
                resume=resume,
                budget=QueryLoopBudget(query_portfolio_enabled=False),
            )
        )
        self.assertEqual(len(source.requests), 2)
        self.assertEqual(result.final_query, "ecosystem")
        self.assertEqual(result.primary_hits, [record])
        self.assertIn(
            "Executed query attempts", llm.calls[-1]["messages"][1]["content"]
        )

    async def test_resuming_after_stop_does_not_restart_refinement(self):
        observer = RecordingObserver(stop_phase="query_primary_complete")
        record = PaperRecord(uid="retained", title="Evidence", provider="openalex")
        request = QueryLoopRequest(
            question="ecosystem",
            budget=QueryLoopBudget(query_portfolio_enabled=False),
            resume=QueryLoopResumeState(
                question="ecosystem",
                source="openalex",
                search_plan=SearchPlan(intent="ecosystem"),
                current_query="ecosystem",
            ),
        )
        with self.assertRaises(StopAfterCheckpoint):
            await QueryLoopPipeline(
                FixtureLLM('{"query": "ecosystem"}', '{"query": "ecosystem"}'),
                SequenceSource(source_result([record], total=4381)),
                observer,
            ).run(request)
        source, llm = SequenceSource(), FixtureLLM()
        resumed = request.model_copy(
            update={
                "resume": QueryLoopResumeState.model_validate(
                    observer.checkpoints[-1].state
                )
            }
        )
        result = await QueryLoopPipeline(llm, source, RecordingObserver()).run(resumed)
        self.assertEqual(result.primary_hits, [record])
        self.assertIn("query_repeated", result.warnings)
        self.assertEqual((len(source.requests), len(llm.calls)), (0, 0))

    async def test_cancellation_after_proposal_prevents_further_source_requests(self):
        class Token:
            cancelled = False

            def raise_if_cancelled(self):
                if self.cancelled:
                    raise asyncio.CancelledError

        token = Token()

        class CancellingLLM(FixtureLLM):
            async def chat_json(self, *args, **kwargs):
                token.cancelled = True
                return '{"query": "ecosystem"}'

        source = SequenceSource(source_result([], total=0))
        with self.assertRaises(asyncio.CancelledError):
            await QueryLoopPipeline(
                CancellingLLM(), source, RecordingObserver(), cancellation=token
            ).run(
                QueryLoopRequest(
                    question="ecosystem",
                    budget=QueryLoopBudget(query_portfolio_enabled=False),
                    resume=QueryLoopResumeState(
                        question="ecosystem",
                        source="openalex",
                        search_plan=SearchPlan(intent="ecosystem"),
                        current_query="ecosystem",
                    ),
                )
            )
        self.assertEqual(len(source.requests), 1)

    async def test_pending_query_is_not_executed_history_but_old_checkpoint_cycle_is(
        self,
    ):
        for pending, expected_calls in (
            ("ecosystem AND resilience", 1),
            ("ecosystem", 0),
        ):
            with self.subTest(pending=pending):
                record = PaperRecord(
                    uid="retained", title="Evidence", provider="openalex"
                )
                source = SequenceSource(source_result([record], total=25))
                resume = QueryLoopResumeState(
                    phase="next_query",
                    question="ecosystem resilience",
                    source="openalex",
                    search_plan=SearchPlan(intent="ecosystem resilience"),
                    current_query=pending,
                    iteration=1,
                    total=13151,
                    primary_hits=[record],
                    history=[
                        QueryHistoryEntry(
                            iteration=1,
                            query="ecosystem",
                            total=13151,
                            action="narrow",
                            next_query=pending,
                        )
                    ],
                )
                result = await QueryLoopPipeline(
                    FixtureLLM(), source, RecordingObserver()
                ).run(
                    QueryLoopRequest(
                        question=resume.question,
                        resume=resume,
                        budget=QueryLoopBudget(query_portfolio_enabled=False),
                    )
                )
                self.assertEqual(len(source.requests), expected_calls)
                self.assertEqual(result.primary_hits, [record])

    def test_oscillating_counts_are_not_new_progress_but_real_improvement_continues(
        self,
    ):
        base = dict(
            iteration=5,
            configured_iterations=5,
            adaptive_max_iterations=15,
            target_min=5,
            target_max=50,
            retrieval_pool_min=5,
            retrieval_pool_max=3000,
        )
        for totals, expected in (
            ([20953, 13151, 4381, 13151, 4381], "accept_high"),
            ([20000, 15000, 10000, 7000, 4000], "narrow"),
            ([20000, 15000, 10000, 9500, 8900], "narrow"),
        ):
            with self.subTest(totals=totals):
                snapshot = QueryLoopSnapshot(
                    **base,
                    total=totals[-1],
                    history=[
                        {"query": f"distinct-{i}", "total": n, "action": "narrow"}
                        for i, n in enumerate(totals[:-1])
                    ],
                )
                self.assertEqual(
                    QueryLoopController().decide(snapshot).action, expected
                )


if __name__ == "__main__":
    unittest.main()
