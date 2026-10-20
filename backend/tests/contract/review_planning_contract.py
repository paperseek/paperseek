from __future__ import annotations

import asyncio
import json
import unittest
from uuid import uuid4

from fixtures.review_support import ReviewLLM

from paperseek_app.execution import CancellationScope
from paperseek_app.review.models import ReviewContent, ReviewCreate
from paperseek_core.errors import LLMRequestError
from paperseek_core.models import FinalPaperResult
from paperseek_core.review.evidence import snapshot, validate_cards
from paperseek_core.review.generation import (
    write_section,
)
from paperseek_core.review.models import EvidenceBatch, EvidenceCard, ReviewEvidence, ReviewOutline, SectionBody
from paperseek_core.review.planning import (
    extract_evidence,
    prepare_outline,
)
from paperseek_core.review.structured import (
    ReviewOutputError,
    structured_call,
)


class ReviewPlanningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.papers = [snapshot(FinalPaperResult(rank=i, uid=str(i), title=f"Study {i}",
                                               abstract=f"Study {i} reports an association in setting {i}."), "source", i)
                       for i in (1, 2)]
        self.cards = validate_cards(EvidenceBatch(cards=[{
            "paper_id": paper.id, "evidence": [{"category": "finding", "text": paper.abstract,
                                                "quote": paper.abstract}]} for paper in self.papers]), self.papers, "test")
        self.llm = ReviewLLM()

    async def outline(self, count=None, length="standard"):
        return await prepare_outline(self.llm, CancellationScope(), topic="Study settings", language="en",
                                     length=length, section_count=count, cards=self.cards, papers=self.papers)

    def only_first_paper(self, result, *, reason="Duplicates the larger study p_001."):
        first = next(item for item in self.cards[0].evidence)
        for paragraph in result["paragraphs"]:
            paragraph["text"] = "One study reports a related finding (p_001)."
            paragraph["evidence_ids"] = [first.id]
        if reason is not None:
            result["omitted"] = [{"paper_id": "p_002", "reason": reason}]
        return result

    async def test_a_planned_paper_left_out_with_a_reason_is_kept_without_a_warning(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def leave_out(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]["content"] for task in ("section", "section_edit")):
                result = self.only_first_paper(result)
            if "TASK: support_check\n" in messages[0]["content"]:
                data = json.loads(messages[1]["content"])
                self.assertEqual([item["paper_id"] for item in data["omitted_papers"]], ["p_002"])
                self.assertEqual(data["omitted_papers"][0]["abstract"], self.papers[1].abstract)
            return json.dumps(result)

        self.llm.chat_json = leave_out
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual([(item.paper_id, item.reason, item.questioned) for item in body.omitted],
                         [("p_002", "Duplicates the larger study p_001.", False)])
        self.assertTrue(all(item.status == "supported" for item in findings))
        self.assertEqual(self.llm.calls["section_sources"], 0)

    async def test_a_doubtful_reason_is_questioned_and_marked_for_review(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def doubtful(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]["content"] for task in ("section", "section_edit")):
                result = self.only_first_paper(result, reason="Not related to the topic.")
            if "TASK: support_check\n" in messages[0]["content"]:
                result["omissions"] = [{"paper_id": "p_002", "status": "questioned",
                                        "reason": "Its abstract reports the association this paragraph compares."}]
            return json.dumps(result)

        self.llm.chat_json = doubtful
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertTrue(body.omitted[0].questioned)
        self.assertIn("association", body.omitted[0].check_reason)
        self.assertEqual(findings[0].status, "needs_revision")
        self.assertIn("p_002 may not hold", findings[0].reason)

    async def test_a_planned_paper_dropped_without_a_reason_gets_cited_or_explained(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def silent(messages, **kwargs):
            if "TASK: section_sources\n" in messages[0]["content"]:
                self.llm.calls["section_sources"] += 1
                data = json.loads(messages[1]["content"])
                key = next(iter(data["paragraphs"]))
                draft = data["paragraphs"][key]["draft"]
                return json.dumps({key: draft, "omitted": [{"paper_id": "p_002", "reason": "Different population."}]})
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]["content"] for task in ("section", "section_edit")):
                result = self.only_first_paper(result, reason=None)
            return json.dumps(result)

        self.llm.chat_json = silent
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls["section_sources"], 1)
        self.assertEqual([(item.paper_id, item.reason) for item in body.omitted], [("p_002", "Different population.")])
        self.assertTrue(all("没有说明原因" not in item.reason for item in findings))

    async def test_invented_citations_are_cleaned_and_the_section_is_kept(self):
        outline = await self.outline(1)
        original = self.llm.chat_json
        diagnostics = []

        async def invented(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if "TASK: section\n" in messages[0]["content"]:
                for paragraph in result["paragraphs"]:
                    paragraph["text"] += " Both agree (e_0123456789abcdef01234567)."
                    paragraph["evidence_ids"].append("e_p_010")
            return json.dumps(result)

        self.llm.chat_json = invented
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers,
            diagnostics=diagnostics)
        self.assertEqual(self.llm.calls["section"], 3)
        self.assertNotIn("e_p_010", body.paragraphs[0].evidence_ids)
        self.assertNotIn("e_0123", body.paragraphs[0].text)
        self.assertTrue(diagnostics and diagnostics[0].startswith("section:"))
        self.assertEqual(findings[0].status, "needs_revision")
        self.assertIn("do not exist were removed", findings[0].reason)

    async def test_support_check_falls_back_to_single_paragraphs_and_records_why(self):
        outline = await prepare_outline(self.llm, CancellationScope(), topic="Settings", language="en", length="standard",
                                        section_count=1, cards=self.cards, papers=self.papers, paragraph_count=2)
        original = self.llm.chat_json
        diagnostics = []
        prompts = []

        async def broken_section_check(messages, **kwargs):
            if "TASK: support_check\n" in messages[0]["content"]:
                prompts.append(messages[0]["content"])
                data = json.loads(messages[1]["content"])
                if len(data["paragraphs"]) > 1:
                    return json.dumps({"findings": [{"paragraph": 0, "status": "supported", "reason": "only one"}]})
            return await original(messages, **kwargs)

        self.llm.chat_json = broken_section_check
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
            length="standard", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers,
            diagnostics=diagnostics)
        self.assertEqual(len(body.paragraphs), 2)
        self.assertEqual([item.paragraph for item in findings], [0, 1])
        self.assertTrue(all(item.status == "supported" for item in findings))
        self.assertTrue(any("support_check section" in item for item in diagnostics))
        self.assertIn("Write every reason in Chinese.", prompts[0])

    async def test_bad_batch_quote_recovers_only_the_missing_paper(self):
        original = self.llm.chat_json
        batches = []
        async def recover(messages, **kwargs):
            data = json.loads(messages[1]['content'])
            batches.append([paper['id'] for paper in data['papers']])
            result = json.loads(await original(messages, **kwargs))
            if len(data['papers']) > 1:
                result['cards'][1]['evidence'][0]['quote'] = 'An invented quotation.'
            return json.dumps(result)
        self.llm.chat_json = recover
        cards = await extract_evidence(self.llm, CancellationScope(), self.papers, 'en', 'test')
        self.assertEqual([card.paper_id for card in cards], ['p_001', 'p_002'])
        self.assertEqual(batches, [['p_001', 'p_002'], ['p_001', 'p_002'], ['p_002']])
        self.assertEqual(cards[1].evidence[0].quote, self.papers[1].abstract)

    async def test_bad_card_schema_retries_only_that_paper(self):
        original = self.llm.chat_json
        batches = []

        async def oversized(messages, **kwargs):
            data = json.loads(messages[1]['content'])
            batches.append([paper['id'] for paper in data['papers']])
            result = json.loads(await original(messages, **kwargs))
            if len(data['papers']) > 1:
                result['cards'][1]['evidence'][0]['quote'] = 'x' * 2001
            return json.dumps(result)

        self.llm.chat_json = oversized
        diagnostics = []
        cards = await extract_evidence(self.llm, CancellationScope(), self.papers, 'en', 'test', diagnostics)
        self.assertEqual([card.paper_id for card in cards], ['p_001', 'p_002'])
        self.assertEqual(batches, [['p_001', 'p_002'], ['p_001', 'p_002'], ['p_002']])
        # A long quote is no longer a schema error; one that is not in the abstract is still rejected.
        self.assertIn('Non-verbatim', diagnostics[0])

    async def test_isolated_request_failure_preserves_verified_cards_but_cancellation_propagates(self):
        original = self.llm.chat_json
        failure = LLMRequestError('Request retries exhausted')

        async def unavailable(messages, **kwargs):
            data = json.loads(messages[1]['content'])
            if len(data['papers']) == 1:
                raise failure
            result = json.loads(await original(messages, **kwargs))
            result['cards'][1]['evidence'][0]['quote'] = 'An invented quotation.'
            return json.dumps(result)

        self.llm.chat_json = unavailable
        diagnostics = []
        cards = await extract_evidence(self.llm, CancellationScope(), self.papers, 'en', 'test', diagnostics)
        self.assertEqual([card.paper_id for card in cards], ['p_001'])
        self.assertIn('p_002: research_workflow_failed', diagnostics)
        failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await extract_evidence(self.llm, CancellationScope(), self.papers, 'en', 'test')

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

    async def test_invalid_edit_keeps_valid_draft_and_still_checks_evidence(self):
        outline = await self.outline(1)
        original = self.llm.chat_json
        async def invalid_edit(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if 'TASK: section_edit\n' in messages[0]['content']:
                result['paragraphs'][0]['evidence_ids'] = ['invented']
            return json.dumps(result)
        self.llm.chat_json = invalid_edit
        body, findings = await write_section(self.llm, CancellationScope(), topic='Settings', language='en',
            length='short', outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls['section_edit'], 2)
        self.assertGreater(self.llm.calls['support_check'], 0)
        self.assertNotIn('invented', body.paragraphs[0].evidence_ids)
        self.assertEqual(findings[0].status, 'needs_revision')
        self.assertIn('validated draft was retained', findings[0].reason)

    async def test_draft_missing_only_inline_placement_or_chinese_is_kept_and_marked(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def stubborn(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]["content"] for task in ("section", "section_edit", "section_repair")):
                for paragraph in result["paragraphs"]:
                    paragraph["text"] = "Both studies report an association across their two separate study settings."
            return json.dumps(result)

        self.llm.chat_json = stubborn
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls["section"], 3)
        self.assertEqual(len(body.paragraphs[0].evidence_ids), 2)
        self.assertEqual(findings[0].status, "needs_revision")
        # Unplaced citations are listed under the paragraph when shown, not reported as a note.
        self.assertNotIn("未在正文中标注", findings[0].reason)
        self.assertIn("英文", findings[0].reason)

    def test_ellipsis_quotes_split_unverifiable_extras_drop_and_a_seventh_item_is_ignored(self):
        paper = snapshot(FinalPaperResult(rank=1, uid="multi", title="Multi",
            abstract="Alpha beta gamma delta epsilon. Zeta eta theta iota kappa. Lambda mu nu xi omicron."), "source", 1)
        items = [{"category": "finding", "text": "Joined", "quote": "Alpha beta gamma delta epsilon... Lambda mu nu xi omicron"},
                 {"category": "finding", "text": "Invented", "quote": "A sentence that appears nowhere in this abstract at all."}]
        items += [{"category": "scope", "text": "Repeated observation", "quote": "Zeta eta theta iota kappa."} for _ in range(5)]
        batch = EvidenceBatch.model_validate({"cards": [{"paper_id": paper.id, "evidence": items}]})
        self.assertEqual(len(batch.cards[0].evidence), 7)
        dropped = []
        card = validate_cards(batch, [paper], "test", dropped)[0]
        self.assertEqual([item.quote for item in card.evidence],
                         ["Alpha beta gamma delta epsilon", "Lambda mu nu xi omicron", "Zeta eta theta iota kappa."])
        self.assertEqual(len(dropped), 1)
        self.assertIn("appears nowhere", dropped[0])

    def test_extra_observations_keep_every_category_and_long_quotes_keep_their_best_sentences(self):
        sentences = [f"Sentence {index} reports finding number {index} in adults across two cohorts." for index in range(1, 13)]
        abstract = " ".join(sentences)
        paper = snapshot(FinalPaperResult(rank=1, uid="long", title="Long", abstract=abstract), "source", 1)
        items = [{"category": "finding", "text": f"Finding {index}", "quote": sentences[index - 1]} for index in range(1, 10)]
        items.append({"category": "scope", "text": "Scope note", "quote": sentences[9]})
        items.append({"category": "method", "text": "Sentence 11 reports finding number 11", "quote": abstract,
                      "unexpected": "ignored"})
        batch = EvidenceBatch.model_validate({"cards": [{"paper_id": paper.id, "evidence": items, "note": "x"}]})
        card = validate_cards(batch, [paper], "test")[0]
        self.assertEqual(len(card.evidence), 8)
        self.assertEqual({item.category for item in card.evidence}, {"finding", "scope", "method"})
        method = next(item for item in card.evidence if item.category == "method")
        self.assertIn("Sentence 11 reports finding number 11", method.quote)
        self.assertLessEqual(len(method.quote), 2 * len(sentences[10]) + 1)
        self.assertEqual(abstract[method.start:method.end], method.quote)

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

    async def test_approved_plan_reaches_writer_and_edited_prose_is_fact_checked(self):
        outline = await self.outline(1)
        outline.sections[0].paragraphs.append(outline.sections[0].paragraphs[0].model_copy(deep=True))
        original = self.llm.chat_json
        inspected = []

        async def observe(messages, **kwargs):
            task = messages[0]["content"].split("TASK: ")[1].split("\n")[0]
            data = json.loads(messages[1]["content"])
            result = json.loads(await original(messages, **kwargs))
            if task == "section":
                self.assertEqual(data["review_focus"], outline.focus)
                self.assertEqual(len(data["section"]["paragraphs"]), 2)
                result["paragraphs"][0]["text"] = "Unfortunately, the studies examine different settings (p_001, p_002)."
            elif task == "section_edit":
                self.assertEqual(data["editing_focus"], [{"paragraph": 0, "expressions_to_reframe": ["unfortunately"]}])
                for paragraph in result["paragraphs"]:
                    paragraph["text"] = "The association was studied in two distinct settings (p_001, p_002)."
            elif task == "support_check":
                inspected.extend(data["paragraphs"])
                self.assertEqual({p['paper_id'] for p in data['paragraphs'][0]['cited_paper_abstracts']},
                                 {p.id for p in self.papers})
            return json.dumps(result)

        self.llm.chat_json = observe
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(len(body.paragraphs), 2)
        self.assertEqual(self.llm.calls["section_edit"], 1)
        self.assertTrue(all(p["text"] == body.paragraphs[0].text for p in inspected))
        self.assertEqual({q["paper_id"] for q in inspected[0]["abstract_quotes"]}, {"p_001", "p_002"})
        self.assertTrue(all(f.status == "supported" for f in findings))

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

    def test_quote_repair_reports_all_affected_papers(self):
        batch = EvidenceBatch(cards=[{"paper_id": p.id, "evidence": [{"category": "finding", "text": "Observation",
            "quote": "Nothing in this sentence comes from the supplied abstract whatsoever."}]} for p in self.papers])
        with self.assertRaises(ValueError) as caught:
            validate_cards(batch, self.papers, "test")
        for paper in self.papers:
            self.assertIn(paper.id, str(caught.exception))

    async def test_editor_can_complete_coverage_without_rewriting_the_entire_draft(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def omit_once(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if "TASK: section\n" in messages[0]["content"] and self.llm.calls["section"] == 1:
                result["paragraphs"][0].update(text="Setting one shows an association (p_001).",
                                               evidence_ids=[self.cards[0].evidence[0].id])
            return json.dumps(result)

        self.llm.chat_json = omit_once
        body, _ = await write_section(self.llm, CancellationScope(), topic="Settings", language="en", length="short",
                                      outline=outline, section=outline.sections[0], cards=self.cards)
        self.assertEqual(self.llm.calls["section"], 1)
        self.assertEqual(len(body.paragraphs[0].evidence_ids), 2)

    async def test_missing_source_patch_preserves_other_paragraphs_and_is_fact_checked(self):
        outline = await self.outline(1)
        outline.sections[0].paragraphs.append(outline.sections[0].paragraphs[0].model_copy(deep=True))
        outline.sections[0].paragraphs[0].paper_ids = ["p_001"]
        outline.sections[0].paragraphs[1].paper_ids = ["p_002"]
        original = self.llm.chat_json
        patch_calls = 0
        checked = []

        async def omit_then_patch(messages, **kwargs):
            nonlocal patch_calls
            task = messages[0]["content"].split("TASK: ")[1].split("\n")[0]
            data = json.loads(messages[1]["content"])
            if task == "section_sources":
                patch_calls += 1
                self.assertEqual(list(data["paragraphs"]), ["paragraph_2"])
                self.assertEqual(data["paragraphs"]["paragraph_2"]["missing_paper_ids"], ["p_002"])
                # Merely exchanging the old citation for the missing one must not lose coverage elsewhere.
                return json.dumps({"paragraph_2": {"text": "The association was studied in setting two (p_002).",
                                                  "evidence_ids": [self.cards[1].evidence[0].id]}})
            if task == "support_check":
                checked.extend(data["paragraphs"])
            result = json.loads(await original(messages, **kwargs))
            if task in {"section", "section_edit"}:
                for p in result["paragraphs"]:
                    p["text"] = "Existing supported comparison (p_001)."
                    p["evidence_ids"] = [self.cards[0].evidence[0].id]
            return json.dumps(result)

        self.llm.chat_json = omit_then_patch
        body, _ = await write_section(self.llm, CancellationScope(), topic="Settings", language="en", length="short",
                                      outline=outline, section=outline.sections[0], cards=self.cards)
        self.assertEqual(patch_calls, 1)
        self.assertEqual(body.paragraphs[0].text, "Existing supported comparison (p_001).")
        self.assertEqual(body.paragraphs[0].evidence_ids, [self.cards[0].evidence[0].id])
        self.assertEqual(checked[1]["text"], body.paragraphs[1].text)
        self.assertEqual(checked[1]["abstract_quotes"][0]["quote"], self.papers[1].abstract)

    async def test_source_patch_cannot_trade_one_missing_paper_for_another(self):
        outline = await self.outline(1)
        original = self.llm.chat_json
        patch_calls = 0

        async def replace_then_include(messages, **kwargs):
            nonlocal patch_calls
            task = messages[0]["content"].split("TASK: ")[1].split("\n")[0]
            if task == "section_sources":
                patch_calls += 1
                if patch_calls == 2:
                    self.assertIn("p_001", messages[-1]["content"])
                cards = self.cards[1:] if patch_calls == 1 else self.cards
                mentions = ", ".join(c.paper_id for c in cards)
                return json.dumps({"paragraph_1": {"text": f"The studies examine two settings ({mentions}).",
                                                   "evidence_ids": [c.evidence[0].id for c in cards]}})
            result = json.loads(await original(messages, **kwargs))
            if task in {"section", "section_edit"}:
                result["paragraphs"][0].update(text="Setting one shows an association (p_001).",
                                               evidence_ids=[self.cards[0].evidence[0].id])
            return json.dumps(result)

        self.llm.chat_json = replace_then_include
        body, _ = await write_section(self.llm, CancellationScope(), topic="Settings", language="en", length="short",
                                      outline=outline, section=outline.sections[0], cards=self.cards)
        self.assertEqual(patch_calls, 2)
        self.assertEqual(set(body.paragraphs[0].evidence_ids), {c.evidence[0].id for c in self.cards})

    async def test_unresolved_coverage_keeps_valid_draft_and_never_accepts_invalid_citations(self):
        outline = await self.outline(1)
        original = self.llm.chat_json
        for invalid_id in (False, True):
            patches = 0

            async def incomplete(messages, **kwargs):
                nonlocal patches
                if "TASK: section_sources\n" in messages[0]['content']:
                    patches += 1
                    return json.dumps({'paragraph_1': {'text': 'Existing valid finding (p_001).',
                        'evidence_ids': ['invented'] if invalid_id else [self.cards[0].evidence[0].id]}})
                result = json.loads(await original(messages, **kwargs))
                if any(f'TASK: {t}\n' in messages[0]['content'] for t in ['section', 'section_edit']):
                    result['paragraphs'][0].update(text='Setting one shows an association (p_001).',
                                                   evidence_ids=[self.cards[0].evidence[0].id])
                return json.dumps(result)

            self.llm.chat_json = incomplete
            with self.subTest(invalid_id=invalid_id):
                request = write_section(self.llm, CancellationScope(), topic='Settings', language='zh', length='short',
                                        outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
                # An invalid repair is never accepted; either way the validated draft is kept
                # and the omitted planned source is reported for review instead of failing the task.
                body, findings = await request
                self.assertEqual(body.paragraphs[0].evidence_ids, [self.cards[0].evidence[0].id])
                self.assertEqual(findings[0].status, 'needs_revision')
                self.assertIn('p_002', findings[0].reason)
                self.assertEqual(patches, 2)

    async def test_mistyped_citation_gets_specific_repair_feedback(self):
        outline = await self.outline(1)
        original = self.llm.chat_json
        evidence_id = self.cards[0].evidence[0].id

        async def mistype_once(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if "TASK: section\n" in messages[0]["content"]:
                if self.llm.calls["section"] == 1:
                    result["paragraphs"][0]["evidence_ids"][0] = evidence_id[:-1]
                else:
                    self.assertIn(evidence_id, messages[-1]["content"])
                    self.assertIn(evidence_id[:-1], messages[-1]["content"])
            return json.dumps(result)

        self.llm.chat_json = mistype_once
        body, _ = await write_section(self.llm, CancellationScope(), topic="Settings", language="en", length="short",
                                      outline=outline, section=outline.sections[0], cards=self.cards)
        self.assertEqual(self.llm.calls["section"], 2)
        self.assertIn(evidence_id, body.paragraphs[0].evidence_ids)

    async def test_schema_is_supplied_first_and_a_complete_json_fence_is_accepted(self):
        class Fenced:
            calls = 0

            async def chat_json(inner, messages, **kwargs):
                inner.calls += 1
                self.assertIn('"maxItems":40', messages[0]["content"])
                self.assertIn('"additionalProperties":false', messages[0]["content"])
                return '```json\n{"paragraphs":[{"text":"Finding","evidence_ids":["e_known"]}]}\n```'

        llm = Fenced()
        body = await structured_call(llm, CancellationScope(), "section", "Return paragraphs.", {}, SectionBody)
        self.assertEqual(body.paragraphs[0].text, "Finding")
        self.assertEqual(llm.calls, 1)

    async def test_invalid_json_is_not_salvaged_and_failure_retains_the_task_and_field(self):
        class Invalid:
            calls = 0

            async def chat_json(inner, messages, **kwargs):
                inner.calls += 1
                if inner.calls == 1:
                    return 'Explanation: {"paragraphs": []}'
                return '{"paragraphs":[{"text":["private-input-value"],"evidence_ids":["e_known"]}]}'

        llm = Invalid()
        with self.assertRaises(ReviewOutputError) as caught:
            await structured_call(llm, CancellationScope(), "section_edit", "Return paragraphs.", {}, SectionBody)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(caught.exception.task, "section_edit")
        self.assertIn("paragraphs.0.text", caught.exception.reason)
        self.assertNotIn("private-input-value", caught.exception.reason)

    def corpus(self, count):
        papers = [snapshot(FinalPaperResult(rank=i, uid=str(i), title=f"Study {i}", abstract=f"Finding {i}."),
                           "source", i) for i in range(1, count + 1)]
        cards = [validate_cards(EvidenceBatch(cards=[{"paper_id": p.id, "evidence": [{
            "category": "finding", "text": p.abstract, "quote": p.abstract}]}]), [p], "test")[0] for p in papers]
        return papers, cards

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

    async def outline_for(self, papers, cards, count):
        return await prepare_outline(self.llm, CancellationScope(), topic="Themes", language="en", length="standard",
                                     section_count=count, cards=cards, papers=papers)

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

    async def test_giant_generated_paragraph_is_condensed_without_failing_the_section(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def oversized(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]['content'] for task in ("section", "section_edit")):
                result['paragraphs'][0]['text'] = '综述' * 801 + '（p_001、p_002）'
            return json.dumps(result)

        self.llm.chat_json = oversized
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
                                             length="standard", outline=outline, section=outline.sections[0],
                                             cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls['section'], 1)
        self.assertEqual(self.llm.calls['section_condense'], 1)
        self.assertLessEqual(len(body.paragraphs[0].text), 900)

    async def test_condense_failure_keeps_the_paragraph_and_marks_it_for_review(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def stubborn(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            content = messages[0]['content']
            if "TASK: section_condense\n" in content:
                result['text'] = '综述' * 801 + '（p_001、p_002）'
            elif any(f"TASK: {task}\n" in content for task in ("section", "section_edit")):
                result['paragraphs'][0]['text'] = '综述' * 801 + '（p_001、p_002）'
            return json.dumps(result)

        self.llm.chat_json = stubborn
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
                                             length="standard", outline=outline, section=outline.sections[0],
                                             cards=self.cards, papers=self.papers)
        self.assertEqual(len(body.paragraphs[0].text), 1615)
        self.assertEqual(findings[0].status, 'needs_revision')
        self.assertIn('篇幅', findings[0].reason)

    async def test_broken_json_is_reformatted_without_regenerating_content(self):
        calls = []
        valid = json.dumps({"paragraphs": [{"text": 'He said "yes".', "evidence_ids": ["e_1"]}]})

        class Model:
            async def chat_json(self, messages, **_):
                calls.append(messages[0]["content"])
                if messages[0]["content"].startswith("You are a strict JSON formatter"):
                    return valid
                # Unescaped inner quotes: the content is fine, the JSON encoding is not.
                return '{"paragraphs":[{"text":"He said "yes".","evidence_ids":["e_1"]}]}'

        body = await structured_call(Model(), CancellationScope(), "section", "", {}, SectionBody)
        self.assertEqual(body.paragraphs[0].text, 'He said "yes".')
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("You are a strict JSON formatter"))

    async def test_failed_support_check_marks_paragraphs_for_review_instead_of_failing(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def no_check(messages, **kwargs):
            if "TASK: support_check\n" in messages[0]['content']:
                return json.dumps({"findings": []})
            return await original(messages, **kwargs)

        self.llm.chat_json = no_check
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
                                             length="short", outline=outline, section=outline.sections[0],
                                             cards=self.cards, papers=self.papers)
        self.assertTrue(body.paragraphs)
        self.assertTrue(all(item.status == "needs_revision" for item in findings))
        self.assertEqual(self.llm.calls['section_repair'], 0)

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

    async def test_custom_paragraph_length_sets_the_target_and_condense_threshold(self):
        outline = await self.outline(1)
        original = self.llm.chat_json
        seen = {}

        async def long_draft(messages, **kwargs):
            content = messages[0]["content"]
            result = json.loads(await original(messages, **kwargs))
            if "TASK: section\n" in content:
                seen.update(json.loads(messages[1]["content"]), prompt=content)
            if any(f"TASK: {task}\n" in content for task in ("section", "section_edit")):
                # 500 characters: within the default 900 limit but beyond 1.5× a 200-character target.
                result["paragraphs"][0]["text"] = "综" * 500 + "（p_001、p_002）"
            return json.dumps(result)

        self.llm.chat_json = long_draft
        body, _ = await write_section(self.llm, CancellationScope(), topic="Settings", language="zh",
                                      length="standard", outline=outline, section=outline.sections[0],
                                      cards=self.cards, papers=self.papers, paragraph_length=200)
        self.assertEqual(seen["paragraph_target"], {"characters": 200})
        self.assertEqual(seen["paragraph_limits"]["size"], 300)
        self.assertIn("每段约 200 字", seen["prompt"])
        self.assertEqual(self.llm.calls["section_condense"], 1)

    async def test_english_paragraph_length_is_counted_in_words(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def wordy(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]["content"] for task in ("section", "section_edit")):
                result["paragraphs"][0]["text"] = " ".join(["evidence"] * 160) + " (p_001, p_002)"  # 162 words
            return json.dumps(result)

        self.llm.chat_json = wordy
        await write_section(self.llm, CancellationScope(), topic="Settings", language="en", length="standard",
                            outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers,
                            paragraph_length=150)
        self.assertEqual(self.llm.calls["section_condense"], 0)

    def test_paragraph_length_bounds_follow_the_output_language(self):
        base = {"source_run_id": str(uuid4()), "paper_uids": ["a", "b"], "topic": "T"}
        self.assertEqual(ReviewCreate(**base, language="zh", paragraph_count=4, paragraph_length=1200).paragraph_length, 1200)
        with self.assertRaises(ValueError):
            ReviewCreate(**base, language="en", paragraph_length=1200)
        with self.assertRaises(ValueError):
            ReviewCreate(**base, language="zh", paragraph_length=60)
        with self.assertRaises(ValueError):
            ReviewCreate(**base, paragraph_count=11)

    async def test_generated_paragraph_limits_depend_on_length_not_paper_count(self):
        original = self.llm.chat_json
        for length, limit in (("short", 3), ("standard", 5), ("detailed", 7)):
            attempts = 0

            async def too_many_once(messages, **kwargs):
                nonlocal attempts
                attempts += 1
                result = json.loads(await original(messages, **kwargs))
                for section in result["sections"]:
                    section["paragraphs"] *= 8 if attempts == 1 else limit
                return json.dumps(result)

            self.llm.chat_json = too_many_once
            outline = await self.outline(3, length)
            self.assertEqual(attempts, 2)
            self.assertTrue(all(len(section.paragraphs) == limit for section in outline.sections))

    async def test_large_evidence_reuses_all_notes_and_checks_original_quotes_without_llm_compression(self):
        papers = [snapshot(FinalPaperResult(rank=i, uid=str(i), title=f"Study {i}",
                           abstract=" ".join(f"Study {i} observation {j} examined shared workflows across many settings "
                                             f"{'and contexts ' * 40}." for j in range(8))), "source", i) for i in range(1, 11)]
        # Eight long verbatim sentences per paper: enough evidence to exceed the writing prompt's budget.
        cards = []
        for i, p in enumerate(papers, 1):
            items = []
            for sentence in p.abstract.split(". ")[:8]:
                start = p.abstract.index(sentence)
                items.append(ReviewEvidence(category="finding", text=f"Study {i} examined shared workflows.",
                                            quote=sentence, id=f"e_{i:02}{start:06}", paper_id=p.id, start=start,
                                            end=start + len(sentence), abstract_hash=p.abstract_hash))
            cards.append(EvidenceCard(paper_id=p.id, signature="test", evidence=items))
        outline = await prepare_outline(self.llm, CancellationScope(), topic="Workflows", language="en",
                                       length="standard", cards=cards, papers=papers, section_count=1)
        original = self.llm.chat_json
        checked = set()
        planned = [card for card in cards if card.paper_id in outline.sections[0].paper_ids]

        async def inspect(messages, **kwargs):
            task = messages[0]["content"].split("TASK: ")[1].split("\n")[0]
            data = json.loads(messages[1]["content"])
            self.assertNotEqual(task, "evidence_group")
            if task in {"section", "section_edit"}:
                notes = [item for card in planned for item in card.evidence]
                self.assertEqual(len(data["abstract_evidence"]), len(notes))
                for note, item in zip(data["abstract_evidence"], notes, strict=True):
                    self.assertEqual(note["id"], item.id)
                    self.assertEqual(note["text"], item.text)
                    self.assertNotIn("quote", note)
                    self.assertNotIn("abstract_hash", note)
            if task == "support_check":
                quotes = {item.id: item.quote for c in cards for item in c.evidence}
                for paragraph in data["paragraphs"]:
                    for item in paragraph["abstract_quotes"]:
                        self.assertIn(item["quote"], quotes.values())
                        checked.add(item["paper_id"])
            return await original(messages, **kwargs)

        self.llm.chat_json = inspect
        await write_section(self.llm, CancellationScope(), topic="Workflows", language="en", length="standard",
                            outline=outline, section=outline.sections[0], cards=cards, papers=papers)
        self.assertEqual(checked, {card.paper_id for card in planned})

    async def test_generated_sections_derive_sources_from_their_paragraph_plans(self):
        original = self.llm.chat_json

        async def paragraph_sources(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            result["sections"][0].pop("paper_ids")
            result["sections"][1]["paper_ids"] = ["p_001"]
            return json.dumps(result)

        self.llm.chat_json = paragraph_sources
        outline = await self.outline(3)
        self.assertTrue(all(set(section.paper_ids) == {"p_001", "p_002"} for section in outline.sections))
        self.assertEqual(self.llm.calls["outline"], 1)


if __name__ == "__main__":
    unittest.main()
