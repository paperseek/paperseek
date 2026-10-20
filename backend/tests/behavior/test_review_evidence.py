from __future__ import annotations

import asyncio
import json
import unittest

from fixtures.review_support import ReviewCase

from paperseek_app.execution import CancellationScope
from paperseek_core.errors import LLMRequestError
from paperseek_core.models import FinalPaperResult
from paperseek_core.review.evidence import snapshot, validate_cards
from paperseek_core.review.generation import (
    write_section,
)
from paperseek_core.review.models import (
    EvidenceBatch,
    EvidenceCard,
    ReviewEvidence,
    SectionBody,
)
from paperseek_core.review.planning import (
    extract_evidence,
    prepare_outline,
)


class ReviewEvidenceTests(ReviewCase):
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

    def test_quote_repair_reports_all_affected_papers(self):
        batch = EvidenceBatch(cards=[{"paper_id": p.id, "evidence": [{"category": "finding", "text": "Observation",
            "quote": "Nothing in this sentence comes from the supplied abstract whatsoever."}]} for p in self.papers])
        with self.assertRaises(ValueError) as caught:
            validate_cards(batch, self.papers, "test")
        for paper in self.papers:
            self.assertIn(paper.id, str(caught.exception))

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

    def test_cross_language_long_quote_keeps_the_result_and_original_offsets(self):
        abstract = ("Background describes the longstanding problem and prior work. " * 12
                    + "The treatment reduced mortality by 35 percent.")
        paper = snapshot(FinalPaperResult(rank=1, uid="long", title="Trial", abstract=abstract), "source", 1)
        for observation in ("治疗使死亡率降低了百分之三十五。", "治疗使死亡率降低了35%。"):
            with self.subTest(observation=observation):
                batch = EvidenceBatch(cards=[{"paper_id": paper.id, "evidence": [
                    {"category": "finding", "text": observation, "quote": abstract}]}])
                item = validate_cards(batch, [paper], "test")[0].evidence[0]
                self.assertEqual(item.quote, abstract)
                self.assertEqual(paper.abstract[item.start:item.end], abstract)

    async def test_oversized_quote_requests_a_shorter_span_before_clipping(self):
        result_sentence = "The treatment reduced mortality by 35 percent."
        abstract = "Background describes the longstanding problem and prior work. " * 40 + result_sentence
        paper = snapshot(FinalPaperResult(rank=1, uid="long", title="Trial", abstract=abstract), "source", 1)
        calls = []

        async def shorten(messages, **_):
            calls.append(messages)
            quote = abstract if len(calls) == 1 else result_sentence
            return json.dumps({"cards": [{"paper_id": paper.id, "evidence": [
                {"category": "finding", "text": "治疗使死亡率降低了35%。", "quote": quote}]}]})

        self.llm.chat_json = shorten
        cards = await extract_evidence(self.llm, CancellationScope(), [paper], "zh", "test")
        self.assertEqual(len(calls), 2)
        self.assertEqual(cards[0].evidence[0].quote, result_sentence)
        self.assertIn("2000", calls[-1][-1]["content"])

    async def test_oversized_quote_fallback_requires_a_locatable_supporting_sentence(self):
        result_sentence = "The treatment reduced mortality by 35 percent."
        abstract = "Background describes the longstanding problem and prior work. " * 40 + result_sentence
        paper = snapshot(FinalPaperResult(rank=1, uid="long", title="Trial", abstract=abstract), "source", 1)
        for observation, retained in ((result_sentence, True), ("治疗使死亡率降低了35%。", False)):
            calls = []

            async def too_long(messages, **_):
                calls.append(messages)
                return json.dumps({"cards": [{"paper_id": paper.id, "evidence": [
                    {"category": "finding", "text": observation, "quote": abstract},
                    {"category": "method", "text": "Background", "quote": abstract.split(".")[0] + "."}]}]})

            self.llm.chat_json = too_long
            with self.subTest(observation=observation):
                cards = await extract_evidence(self.llm, CancellationScope(), [paper], "zh", "test")
                self.assertEqual(len(calls), 2)
                findings = [item for item in cards[0].evidence if item.category == "finding"]
                self.assertEqual(bool(findings), retained)
                if retained:
                    self.assertEqual(findings[0].quote, result_sentence)


from paperseek_core.review.evidence import (
    normalize_abstract,
    validate_body,
)


class ReviewEvidenceTest(unittest.TestCase):
    def test_shared_quote_preserves_distinct_scope_and_research_observations(self):
        from paperseek_core.review.evidence import usable_cards

        paper = snapshot(FinalPaperResult(rank=1, uid='one', title='Study',
                         abstract='Retention improved in the adult participants.'), 'source', 1)
        batch = EvidenceBatch(cards=[{'paper_id': paper.id, 'evidence': [
            {'category': 'scope', 'text': 'Participants were adults.', 'quote': paper.abstract},
            {'category': 'finding', 'text': 'Retention improved.', 'quote': paper.abstract},
        ]}])
        cards = validate_cards(batch, [paper], 'test')
        self.assertEqual([item.category for item in cards[0].evidence], ['scope', 'finding'])
        self.assertEqual(usable_cards(cards, [paper]), cards)

    def test_typographic_quotes_restore_exact_source_text_offsets_and_identity(self):
        sentence = ("First, a valid and reliable flow experience instrument for addressing the different "
                    "dimensions of users' flow experience was developed.")
        for source, generated in ((sentence, sentence.replace("'", "’")),
                                  ('Users’ "flow experience" was measured.', 'Users\' “flow experience” was measured.'),
                                  ('The ‘flow’ measure was reliable.', "The 'flow' measure was reliable.")):
            with self.subTest(source=source):
                paper = snapshot(FinalPaperResult(rank=32, uid="32", title="Flow", abstract="原文😀。 " + source), "run", 32)
                batch = EvidenceBatch(cards=[{"paper_id": paper.id, "evidence": [
                    {"category": "finding", "text": "A reliable flow measure was developed.", "quote": source}]}])
                expected = validate_cards(batch, [paper], "test")[0].evidence[0]
                batch.cards[0].evidence[0].quote = generated
                actual = validate_cards(batch, [paper], "test")[0].evidence[0]
                self.assertEqual(actual, expected)
                self.assertEqual(actual.quote, source)
                self.assertEqual(paper.abstract[actual.start:actual.end], source)
                self.assertEqual(actual.abstract_hash, paper.abstract_hash)

    def test_near_copies_resolve_to_the_source_text_and_inventions_are_rejected(self):
        source = ("We adapt a dynamic private-provision- of-public-goods model. In particular, instead of relying on "
                  "extrinsic motives the model uses intrinsic ones. Scores did not increase (n = 659, p < 0.05).")
        paper = snapshot(FinalPaperResult(rank=1, uid="one", title="Paper", abstract=source), "run", 1)

        def check(quote):
            batch = EvidenceBatch(cards=[{"paper_id": paper.id, "evidence": [
                {"category": "finding", "text": "Claim", "quote": quote}]}])
            return validate_cards(batch, [paper], "test")[0].evidence[0]

        # Small models change hyphenation, case and spacing; evidence keeps the abstract's own words.
        for quote, original in (("We adapt a dynamic private-provision-of-public-goods model.", "private-provision- of-public-goods"),
                                ("Instead of relying on extrinsic motives the model uses intrinsic ones.", "instead of relying"),
                                ("Scores did not increase (n=659, p<0.05).", "(n = 659, p < 0.05")):
            with self.subTest(quote=quote):
                item = check(quote)
                self.assertIn(original, item.quote)
                self.assertEqual(paper.abstract[item.start:item.end], item.quote)
        for quote in ("Funding from large firms pays most open source programmers.", "Scores rose sharply"):
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, "Non-verbatim"):
                check(quote)

    def test_escaped_linebreaks_match_old_snapshots_without_changing_source_offsets(self):
        raw = r"An association was observed\nin two settings."
        paper = snapshot(FinalPaperResult(rank=1, uid="one", title="Paper", abstract=raw), "source", 1)
        self.assertEqual(paper.abstract, "An association was observed in two settings.")
        self.assertEqual(paper.original_abstract, raw)
        old = paper.model_copy(update={"abstract": raw})
        batch = EvidenceBatch(cards=[{"paper_id": paper.id, "evidence": [{"category": "finding",
                              "text": "Observed association", "quote": paper.abstract}]}])
        item = validate_cards(batch, [old], "test")[0].evidence[0]
        self.assertEqual(old.abstract[item.start:item.end], item.quote)
        self.assertEqual(item.quote, raw)
        batch.cards[0].evidence[0].quote = "No association was found in any of the trials we reviewed."
        with self.assertRaisesRegex(ValueError, "Non-verbatim"):
            validate_cards(batch, [old], "test")

    def test_degree_notice_is_not_a_research_abstract_but_short_findings_are_retained(self):
        notice = "This thesis was submitted for the award of Doctor of Philosophy and was awarded by Brunel University London."
        self.assertEqual(normalize_abstract(notice), "")
        self.assertTrue(normalize_abstract(notice + " We found a positive association."))
        self.assertEqual(normalize_abstract("Retention increased."), "Retention increased.")

    def test_missing_short_html_and_unicode_abstracts(self):
        self.assertEqual(normalize_abstract(" <p>No abstract available.</p> "), "")
        self.assertEqual(normalize_abstract("<p>How to cite:</p>"), "")
        self.assertEqual(normalize_abstract("How to cite scientific datasets is the subject of this study."),
                         "How to cite scientific datasets is the subject of this study.")
        self.assertEqual(normalize_abstract("短摘要。"), "短摘要。")
        self.assertEqual(normalize_abstract("<p>A &amp; B</p><p>方法😀</p>"), "A & B 方法😀")

    def test_quotes_are_verified_and_offsets_computed_from_native_abstract(self):
        paper = snapshot(FinalPaperResult(rank=1, uid="one", title="Paper", abstract="原文😀。This is evidence.",
                                         relevance_reason="Invented finding, not evidence."), "source", 1)
        draft = EvidenceBatch(cards=[{"paper_id": paper.id, "evidence": [
            {"category": "finding", "text": "Finding", "quote": "This is evidence."}]}])
        card = validate_cards(draft, [paper], "signature")[0]
        evidence = card.evidence[0]
        self.assertEqual(paper.abstract[evidence.start:evidence.end], evidence.quote)
        draft.cards[0].evidence[0].quote = "Invented finding, not evidence."
        with self.assertRaises(ValueError):
            validate_cards(draft, [paper], "signature")
        with self.assertRaises(ValueError):
            validate_body(SectionBody(paragraphs=[{"text": "False citation", "evidence_ids": ["other-review"]}]), [card], [paper.id])
