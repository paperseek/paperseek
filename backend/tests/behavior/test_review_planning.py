from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from fixtures.review_support import ReviewCase

from paperseek_app.execution import CancellationScope
from paperseek_app.review.models import ReviewContent, ReviewCreate
from paperseek_core.errors import LLMRequestError
from paperseek_core.models import FinalPaperResult
from paperseek_core.review.evidence import snapshot, validate_cards
from paperseek_core.review.models import EvidenceBatch, ReviewOutline
from paperseek_core.review.planning import (
    prepare_outline,
)
from paperseek_core.review.structured import (
    ReviewOutputError,
)


class ReviewPlanningTests(ReviewCase):
    async def test_empty_source_paragraph_is_replanned_without_regenerating_valid_paragraphs(self):
        original = self.llm.chat_json
        calls = []

        async def missing_source(messages, **kwargs):
            task = messages[0]['content'].split('TASK: ', 1)[1].split('\n', 1)[0]
            calls.append(task)
            if task == 'outline_sources':
                return json.dumps({'theme:2': {'goal': 'A revised argument grounded in the first study.',
                                               'paper_ids': ['p_001']}})
            result = json.loads(await original(messages, **kwargs))
            if task == 'outline':
                result['sections'][0]['paragraphs'][1] = {'goal': 'An unsupported planned argument.', 'paper_ids': ['p_093']}
            return json.dumps(result)

        self.llm.chat_json = missing_source
        result = await prepare_outline(self.llm, CancellationScope(), topic='Study settings', language='en',
                                       length='short', section_count=1, paragraph_count=2, cards=self.cards)
        self.assertEqual(calls, ['outline', 'outline_sources'])
        self.assertEqual(result.sections[0].paper_ids, ['p_001', 'p_002'])
        self.assertEqual(len(result.sections[0].paragraphs), 2)
        self.assertEqual(result.sections[0].paragraphs[0].goal, 'Compare findings across study settings.')
        self.assertEqual(result.sections[0].paragraphs[1].goal, 'A revised argument grounded in the first study.')

    async def test_empty_source_repair_cannot_reintroduce_unavailable_papers(self):
        original = self.llm.chat_json

        async def unsupported(messages, **kwargs):
            if 'TASK: outline_sources\n' in messages[0]['content']:
                return json.dumps({'theme:1': {'goal': 'Unsupported', 'paper_ids': ['p_093']}})
            result = json.loads(await original(messages, **kwargs))
            result['sections'][0]['paragraphs'][0]['paper_ids'] = ['p_093']
            return json.dumps(result)

        self.llm.chat_json = unsupported
        with self.assertRaises(ReviewOutputError) as caught:
            await self.outline(1, 'short')
        self.assertEqual(caught.exception.task, 'outline_sources')
        self.assertIn('supplied paper IDs', caught.exception.reason)

    async def test_outline_leaves_out_papers_without_verified_evidence_instead_of_failing(self):
        original = self.llm.chat_json

        async def stray(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if "TASK: outline\n" in messages[0]["content"]:
                result["sections"][0]["paragraphs"][0]["paper_ids"].append("p_093")
            return json.dumps(result)

        self.llm.chat_json = stray
        outline = await self.outline(1)
        self.assertEqual(self.llm.calls["outline"], 1)
        self.assertEqual(outline.sections[0].paper_ids, ["p_001", "p_002"])

    async def test_explicit_counts_and_presets_produce_two_level_plans(self):
        for count in (1, 2, 3, 4, 6, 14):
            with self.subTest(count=count):
                outline = await self.outline(count)
                self.assertEqual(len(outline.sections), count)
                self.assertTrue(outline.focus)
                for section in outline.sections:
                    self.assertFalse(section.parent_id)
                    self.assertTrue(section.paragraphs)
                    self.assertEqual(set(section.paper_ids), {p for plan in section.paragraphs for p in plan.paper_ids})
                    if count <= 4:
                        self.assertEqual(section.kind, "theme")
        for length, expected in (("short", 1), ("standard", 3), ("detailed", 6)):
            self.assertEqual(len((await self.outline(length=length)).sections), expected)

    async def test_wrong_count_or_short_review_wrapper_sections_are_repaired(self):
        original = self.llm.chat_json
        for defect in ("count", "wrapper", "missing_plan"):
            attempts = 0

            async def first_invalid(messages, **kwargs):
                nonlocal attempts
                attempts += 1
                result = json.loads(await original(messages, **kwargs))
                if attempts == 1:
                    if defect == "count":
                        result["sections"].pop()
                    elif defect == "wrapper":
                        result["sections"][0]["kind"] = "overview"
                    else:
                        result["sections"][0]["paragraphs"] = []
                return json.dumps(result)

            self.llm.chat_json = first_invalid
            outline = await self.outline(3)
            self.assertEqual(attempts, 2)
            self.assertEqual(len(outline.sections), 3)
            self.assertTrue(all(s.kind == "theme" and s.paragraphs for s in outline.sections))

    async def test_paragraph_plans_reject_foreign_or_uncovered_papers(self):
        data = (await self.outline(1)).model_dump()
        for assigned in (["foreign"], ["p_001"], ["p_001", "p_001", "p_002"]):
            data["sections"][0]["paragraphs"][0]["paper_ids"] = assigned
            with self.subTest(assigned=assigned), self.assertRaises(ValueError):
                ReviewOutline.model_validate(data)

    async def test_old_saved_outlines_remain_readable_and_section_counts_are_validated(self):
        outline = (await self.outline(1)).model_dump()
        del outline["focus"]
        del outline["sections"][0]["paragraphs"]
        content = ReviewContent(topic="Existing review", language="en", length="short", corpus_hash="test",
                                papers=self.papers, outline=outline)
        self.assertIsNone(content.section_count)
        self.assertEqual(content.outline.sections[0].paragraphs, [])
        for count in (0, 15, True, 2.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                ReviewCreate(source_run_id=uuid4(), paper_uids=["1", "2"], topic="Test", section_count=count)

    async def test_unused_papers_are_placed_within_capacity_or_excluded_with_a_reason(self):
        papers, cards = self.corpus(20)
        original = self.llm.chat_json
        missing = {"p_005", "p_010", "p_015", "p_020"}
        initial = None

        async def leave_four(messages, **kwargs):
            nonlocal initial
            if "TASK: outline_assign\n" in messages[0]["content"]:
                self.llm.calls["outline_assign"] += 1
                data = json.loads(messages[1]["content"])
                self.assertEqual({p["id"] for p in data["papers"]}, missing)
                self.assertEqual([t["available_slots"] for t in data["targets"]], [1, 3])
                return json.dumps({"p_005": {"target": "theme:1", "reason": ""},
                                   "p_010": {"target": "theme_2:1", "reason": ""},
                                   "p_015": {"target": "exclude", "reason": "Duplicates a larger cohort study."},
                                   "p_020": {"target": "exclude", "reason": "Background only."}})
            result = json.loads(await original(messages, **kwargs))
            retained = [p.id for p in papers if p.id not in missing]
            result["excluded"] = []
            for section, ids in zip(result["sections"], (retained[:9], retained[9:]), strict=True):
                section["paper_ids"] = ids
                section["paragraphs"][0]["paper_ids"] = ids
            initial = result
            return json.dumps(result)

        self.llm.chat_json = leave_four
        outline = await prepare_outline(self.llm, CancellationScope(), topic="Themes", language="en",
                                        length="standard", section_count=2, cards=cards, papers=papers)
        self.assertEqual(self.llm.calls["outline"], 1)
        self.assertEqual(self.llm.calls["outline_assign"], 1)
        self.assertEqual(outline.focus, initial["focus"])
        self.assertIn("p_005", outline.sections[0].paper_ids)
        self.assertIn("p_010", outline.sections[1].paper_ids)
        self.assertEqual({item.paper_id: item.reason for item in outline.excluded},
                         {"p_015": "Duplicates a larger cohort study.", "p_020": "Background only."})
        self.assertTrue(all(len(p.paper_ids) <= 10 for s in outline.sections for p in s.paragraphs))
        planned = {p for s in outline.sections for p in s.paper_ids}
        self.assertEqual(planned | {item.paper_id for item in outline.excluded}, {p.id for p in papers})

    async def test_placement_beyond_ten_papers_or_without_a_reason_is_repaired(self):
        papers, cards = self.corpus(12)
        original = self.llm.chat_json
        attempts = []

        async def crowded(messages, **kwargs):
            if "TASK: outline_assign\n" in messages[0]["content"]:
                attempts.append(messages[-1]["content"])
                if len(attempts) == 1:
                    return json.dumps({"p_011": {"target": "theme:1", "reason": ""},
                                       "p_012": {"target": "exclude", "reason": ""}})
                return json.dumps({"p_011": {"target": "exclude", "reason": "Outside the planned comparison."},
                                   "p_012": {"target": "exclude", "reason": "Background only."}})
            result = json.loads(await original(messages, **kwargs))
            result["excluded"] = []
            return json.dumps(result)

        self.llm.chat_json = crowded
        outline = await self.outline_for(papers, cards, 1)
        self.assertEqual(len(attempts), 2)
        self.assertIn("exceed 10 papers", attempts[1])
        self.assertEqual(len(outline.sections[0].paper_ids), 10)
        self.assertEqual({item.paper_id for item in outline.excluded}, {"p_011", "p_012"})

    async def test_a_placement_that_still_overfills_a_paragraph_leaves_the_extra_papers_out(self):
        papers, cards = self.corpus(12)
        original = self.llm.chat_json
        attempts = []

        async def crowded(messages, **kwargs):
            if "TASK: outline_assign\n" in messages[0]["content"]:
                attempts.append(1)
                self.assertIn("Leave reason empty for a placement", messages[0]["content"])
                return json.dumps({"p_011": {"target": "theme:1", "reason": ""},
                                   "p_012": {"target": "exclude", "reason": "Background only."}})
            result = json.loads(await original(messages, **kwargs))
            result["excluded"] = []
            return json.dumps(result)

        self.llm.chat_json = crowded
        outline = await self.outline_for(papers, cards, 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(outline.sections[0].paragraphs[0].paper_ids), 10)
        self.assertEqual({item.paper_id: item.reason for item in outline.excluded},
                         {"p_011": "No fitting paragraph had room (at most 10 papers each).", "p_012": "Background only."})

    async def test_exclusions_listed_inside_a_section_join_the_outline_list(self):
        papers, cards = self.corpus(12)
        original = self.llm.chat_json
        prompts = []

        async def nested(messages, **kwargs):
            if "TASK: outline\n" in messages[0]["content"]:
                prompts.append(messages[0]["content"])
                result = json.loads(await original(messages, **kwargs))
                result["sections"][0]["excluded"] = result.pop("excluded")
                return json.dumps(result)
            return await original(messages, **kwargs)

        self.llm.chat_json = nested
        outline = await self.outline_for(papers, cards, 1)
        self.assertEqual(len(prompts), 1)
        self.assertNotIn('"excluded"', json.dumps(outline.sections[0].model_dump()))
        self.assertEqual({item.paper_id for item in outline.excluded}, {"p_011", "p_012"})
        self.assertEqual(self.llm.calls["outline_assign"], 0)

    async def test_an_overloaded_paragraph_plan_is_asked_for_again(self):
        papers, cards = self.corpus(12)
        original = self.llm.chat_json
        prompts = []

        async def overloaded(messages, **kwargs):
            if "TASK: outline\n" in messages[0]["content"]:
                prompts.append(messages[-1]["content"])
                result = json.loads(await original(messages, **kwargs))
                ids = [p.id for p in papers]
                if len(prompts) == 1:
                    result["excluded"] = []
                    result["sections"][0]["paragraphs"][0]["paper_ids"] = ids
                return json.dumps(result)
            return await original(messages, **kwargs)

        self.llm.chat_json = overloaded
        outline = await self.outline_for(papers, cards, 1)
        self.assertEqual(len(prompts), 2)
        self.assertIn("at most 10", prompts[1])
        self.assertEqual(len(outline.sections[0].paragraphs[0].paper_ids), 10)
        self.assertEqual({item.paper_id for item in outline.excluded}, {"p_011", "p_012"})

    async def test_complete_outline_needs_no_placement_and_placement_failures_surface(self):
        papers, cards = self.corpus(8)
        original = self.llm.chat_json
        for incomplete in (False, True):
            for failure in ("json", "unknown_target", "http", "cancel"):
                with self.subTest(incomplete=incomplete, failure=failure):
                    scope = CancellationScope()
                    placements = []

                    async def broken_assignment(messages, **kwargs):
                        if "TASK: outline_assign\n" in messages[0]["content"]:
                            placements.append(1)
                            if failure == "cancel":
                                scope.cancel()
                                scope.raise_if_cancelled()
                            if failure == "http":
                                raise LLMRequestError("Rate limited", code="model_rate_limited")
                            return "not json" if failure == "json" else json.dumps(
                                {p.id: {"target": "unknown:1", "reason": ""} for p in papers})
                        result = json.loads(await original(messages, **kwargs))
                        for section in result["sections"]:
                            section["paragraphs"][0]["paper_ids"] = [p.id for p in (papers[:-1] if incomplete else papers)]
                        return json.dumps(result)

                    self.llm.chat_json = broken_assignment
                    call = prepare_outline(self.llm, scope, topic="Themes", language="en", length="standard",
                                           section_count=3, cards=cards, papers=papers)
                    if not incomplete:
                        outline = await call
                        self.assertEqual(placements, [])
                        self.assertTrue(all(s.paper_ids == [p.id for p in papers] for s in outline.sections))
                    elif failure == "cancel":
                        with self.assertRaises(asyncio.CancelledError):
                            await call
                    else:
                        with self.assertRaises(LLMRequestError if failure == "http" else ReviewOutputError):
                            await call

    async def test_empty_assignment_targets_reuse_their_original_thematic_sources(self):
        papers = [snapshot(FinalPaperResult(rank=i, uid=str(i), title=f"Study {i}", abstract=f"Finding {i}."),
                           "source", i) for i in range(1, 6)]
        cards = [validate_cards(EvidenceBatch(cards=[{"paper_id": p.id, "evidence": [{
            "category": "finding", "text": p.abstract, "quote": p.abstract}]}]), [p], "test")[0] for p in papers]
        original = self.llm.chat_json
        async def uneven(messages, **kwargs):
            if "TASK: outline_assign\n" in messages[0]["content"]:
                data = json.loads(messages[1]["content"])
                return json.dumps({p["id"]: data["targets"][0]["id"] for p in data["papers"]})
            return await original(messages, **kwargs)

        self.llm.chat_json = uneven
        outline = await prepare_outline(self.llm, CancellationScope(), topic="Themes", language="en", length="standard",
                                        section_count=2, cards=cards, papers=papers)
        self.assertEqual({p for s in outline.sections for p in s.paper_ids}, {p.id for p in papers})
        self.assertTrue(all(p.paper_ids for s in outline.sections for p in s.paragraphs))
        self.assertEqual(outline.sections[1].paragraphs[0].paper_ids, [p.id for p in papers])

    async def test_assignment_patch_rejects_missing_papers_and_invalid_targets(self):
        original = self.llm.chat_json
        attempts = []

        async def invalid_assignment(messages, **kwargs):
            if "TASK: outline_assign\n" in messages[0]["content"]:
                attempts.append(messages)
                return '{}' if len(attempts) == 1 else '{"p_002":{"target":"invented:1","reason":""}}'
            result = json.loads(await original(messages, **kwargs))
            for section in result["sections"]:
                section["paragraphs"][0]["paper_ids"] = ["p_001"]
            return json.dumps(result)

        self.llm.chat_json = invalid_assignment
        with self.assertRaises(ReviewOutputError) as caught:
            await self.outline(3)
        self.assertEqual(len(attempts), 2)
        self.assertIn("p_002", attempts[-1][-1]["content"])
        self.assertEqual(caught.exception.task, "outline_assign")
        self.assertIn("theme:1", caught.exception.reason)

    async def test_custom_paragraph_count_is_planned_exactly_in_every_section(self):
        prompts = []
        original = self.llm.chat_json

        async def record(messages, **kwargs):
            prompts.append(messages[0]["content"])
            return await original(messages, **kwargs)

        self.llm.chat_json = record
        outline = await prepare_outline(self.llm, CancellationScope(), topic="Study settings", language="en",
                                        length="standard", section_count=2, cards=self.cards, papers=self.papers,
                                        paragraph_count=4)
        self.assertTrue(all(len(section.paragraphs) == 4 for section in outline.sections))
        self.assertIn("exactly 4 paragraphs per section", prompts[0])

    def test_paragraph_length_bounds_follow_the_output_language(self):
        base = {"source_run_id": str(uuid4()), "paper_uids": ["a", "b"], "topic": "T"}
        self.assertEqual(ReviewCreate(**base, language="zh", paragraph_count=4, paragraph_length=1200).paragraph_length, 1200)
        with self.assertRaises(ValueError):
            ReviewCreate(**base, language="en", paragraph_length=1200)
        with self.assertRaises(ValueError):
            ReviewCreate(**base, language="zh", paragraph_length=60)
        with self.assertRaises(ValueError):
            ReviewCreate(**base, paragraph_count=11)

    async def test_source_repair_can_reinclude_an_excluded_paper(self):
        original = self.llm.chat_json

        async def repair(messages, **kwargs):
            if "TASK: outline_sources\n" in messages[0]["content"]:
                return json.dumps({"theme:2": {"goal": "Compare setting two.", "paper_ids": ["p_002"]}})
            result = json.loads(await original(messages, **kwargs))
            result["sections"][0]["paragraphs"] = [
                {"goal": "Compare setting one.", "paper_ids": ["p_001"]},
                {"goal": "Unsupported setting.", "paper_ids": ["p_999"]}]
            result["excluded"] = [{"paper_id": "p_002", "reason": "Outside the original argument."}]
            return json.dumps(result)

        self.llm.chat_json = repair
        outline = await prepare_outline(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", section_count=1, paragraph_count=2, cards=self.cards, papers=self.papers)
        self.assertEqual(outline.sections[0].paper_ids, ["p_001", "p_002"])
        self.assertEqual(outline.excluded, [])
