from __future__ import annotations

import json
from uuid import uuid4

from fixtures.review_support import ReviewCase
from pydantic import ValidationError

from paperseek_app.execution import CancellationScope
from paperseek_app.review.models import PaperSuggestionRequest, ReviewCreate
from paperseek_core.review.models import ReviewOutline, SectionBody, SupportCheck
from paperseek_core.review.selection import Candidate, suggest_papers


class ReviewSelectionTests(ReviewCase):
    async def test_auto_selection_uses_first_256_usable_candidates_in_rank_order(self):
        candidates = [Candidate("missing", "No abstract", "")] + [
            Candidate(str(i), f"Study {i}", f"Abstract {i}") for i in range(1, 258)]
        received = []

        async def select(messages, **_):
            received.extend(json.loads(messages[1]["content"])["candidates"])
            return json.dumps({"papers": [{"id": "c1"}, {"id": "c256"}]})

        self.llm.chat_json = select
        selected = await suggest_papers(self.llm, CancellationScope(), topic="Settings", language="en",
                                        candidates=candidates, count=2)
        self.assertEqual(len(received), 256)
        self.assertEqual(received[-1]["title"], "Study 256")
        self.assertEqual([item.uid for item in selected], ["1", "256"])

    def test_256_papers_fit_creation_plans_and_omission_checks_but_257_are_rejected(self):
        ids = [f"p_{i:03}" for i in range(256)]
        common = {"source_run_id": str(uuid4()), "topic": "Settings"}
        self.assertEqual(len(ReviewCreate(**common, paper_uids=ids).paper_uids), 256)
        self.assertEqual(PaperSuggestionRequest(**common, count=256).count, 256)
        with self.assertRaises(ValidationError):
            ReviewCreate(**common, paper_uids=[*ids, "extra"])
        with self.assertRaises(ValidationError):
            PaperSuggestionRequest(**common, count=257)
        excluded = [{"paper_id": paper, "reason": "Different setting."} for paper in ids]
        outline = ReviewOutline(sections=[{"id": "s1", "title": "Theme", "goal": "Compare",
            "paper_ids": ids, "paragraphs": [{"goal": "Compare", "paper_ids": ids}]}])
        self.assertEqual(len(outline.sections[0].paper_ids), 256)
        self.assertEqual(len(ReviewOutline(sections=outline.sections, excluded=excluded).excluded), 256)
        body = SectionBody(paragraphs=[{"text": "Finding", "evidence_ids": ["e_one"]}], omitted=excluded)
        self.assertEqual(len(body.omitted), 256)
        check = SupportCheck(findings=[{"paragraph": 0, "status": "supported"}], omissions=[
            {"paper_id": paper, "status": "accepted", "reason": "Different setting."} for paper in ids])
        self.assertEqual(len(check.omissions), 256)
