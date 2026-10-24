from __future__ import annotations

import json

from fixtures.review_support import ReviewCase

from paperseek_app.execution import CancellationScope
from paperseek_core.review.generation import (
    check_support,
    write_section,
)
from paperseek_core.review.models import SectionBody
from paperseek_core.review.planning import (
    prepare_outline,
)


class ReviewGenerationTests(ReviewCase):
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

    async def test_extra_paragraphs_are_merged_into_the_last_planned_one_without_a_retry(self):
        outline = await self.outline(1)
        original = self.llm.chat_json

        async def one_too_many(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if "TASK: section" + chr(10) in messages[0]["content"]:
                extra = dict(result["paragraphs"][0], text="A further point on the same settings (p_002).",
                             evidence_ids=[self.cards[1].evidence[0].id])
                result["paragraphs"].append(extra)
            return json.dumps(result)

        self.llm.chat_json = one_too_many
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls["section"], 1)
        self.assertEqual(len(body.paragraphs), 1)
        self.assertIn(self.cards[1].evidence[0].id, body.paragraphs[0].evidence_ids)
        self.assertEqual(findings[0].status, "needs_revision")
        self.assertIn("extra ones were merged", findings[0].reason)

    async def test_fewer_paragraphs_than_planned_keep_the_draft_and_mark_it(self):
        outline = await self.outline(1)
        outline.sections[0].paragraphs.append(outline.sections[0].paragraphs[0].model_copy(deep=True))
        original = self.llm.chat_json

        async def one_too_few(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}" + chr(10) in messages[0]["content"] for task in ("section", "section_edit", "section_repair")):
                result["paragraphs"] = result["paragraphs"][:1]
            return json.dumps(result)

        self.llm.chat_json = one_too_few
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls["section"], 3)
        self.assertEqual(len(body.paragraphs), 1)
        self.assertEqual(findings[0].status, "needs_revision")
        self.assertIn("instead of the 2 planned", findings[0].reason)

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

    async def test_missing_duplicate_and_blank_omission_checks_are_repaired_once(self):
        outline = await self.outline(count=1, length="short")
        accepted = {"paper_id": "p_002", "status": "accepted", "reason": "A different setting."}
        for defect in ([], [accepted, accepted], [{**accepted, "reason": " \n "}]):
            calls = []
            body = SectionBody(paragraphs=[{"text": "A finding (p_001).",
                "evidence_ids": [self.cards[0].evidence[0].id]}],
                omitted=[{"paper_id": "p_002", "reason": "A different setting."}])

            async def check(messages, **_):
                calls.append(messages)
                return json.dumps({"findings": [{"paragraph": 0, "status": "supported", "reason": "Supported."}],
                                   "omissions": defect if len(calls) == 1 else [accepted]})

            self.llm.chat_json = check
            with self.subTest(defect=defect):
                await check_support(self.llm, CancellationScope(), body, self.cards, self.papers,
                                    section=outline.sections[0])
                self.assertEqual(len(calls), 2)
                self.assertFalse(body.omitted[0].check_incomplete)
                self.assertFalse(body.omitted[0].questioned)
                self.assertEqual(body.omitted[0].check_reason, "")

    async def test_unfinished_omission_check_keeps_draft_without_rewriting_supported_text(self):
        outline = await self.outline(count=1, length="short")
        original = self.llm.chat_json

        async def missing_checks(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            if any(f"TASK: {task}\n" in messages[0]["content"] for task in ("section", "section_edit")):
                result["paragraphs"] = [{"text": "One study reports an association (p_001).",
                                        "evidence_ids": [self.cards[0].evidence[0].id]}]
                result["omitted"] = [{"paper_id": "p_002", "reason": "A different setting."}]
            if "TASK: support_check\n" in messages[0]["content"]:
                result.pop("omissions", None)
            return json.dumps(result)

        self.llm.chat_json = missing_checks
        body, findings = await write_section(self.llm, CancellationScope(), topic="Settings", language="en",
            length="short", outline=outline, section=outline.sections[0], cards=self.cards, papers=self.papers)
        self.assertEqual(self.llm.calls["support_check"], 2)
        self.assertEqual(self.llm.calls["section_repair"], 0)
        self.assertEqual(body.paragraphs[0].text, "One study reports an association (p_001).")
        self.assertTrue(body.omitted[0].check_incomplete)
        self.assertFalse(body.omitted[0].questioned)
        self.assertEqual(findings[0].status, "needs_revision")
        self.assertIn("omission reason", findings[0].reason)
