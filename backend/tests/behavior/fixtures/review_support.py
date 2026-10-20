from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

from paperseek_community.composition import execute_fixture_search
from paperseek_community.settings import CommunityDeploymentSettings


def review_settings():
    return CommunityDeploymentSettings(llm_api_type="openai_chat", llm_base_url="https://model.example/v1",
        llm_model="test-writing", llm_api_key="", openalex_api_key="", semantic_scholar_api_key="",
        pubmed_api_key="", wos_api_key="", google_scholar_api_key="", contact_email="")

ROUTE = {"provider": "custom", "api_type": "openai_chat", "base_url": "https://model.example/v1", "model": "test-writing"}


class ReviewLLM:
    def __init__(self):
        self.calls = Counter()
        self.block = ""
        self.entered = asyncio.Event()

    async def chat_json(self, messages, **_):
        if messages[0]["content"].startswith("You are a strict JSON formatter"):
            # The formatting pass may only re-encode content; a fixture cannot invent structure.
            self.calls["format"] += 1
            return messages[1]["content"]
        task = messages[0]["content"].split("TASK: ", 1)[1].split("\n", 1)[0]
        self.calls[task] += 1
        if self.block == task:
            self.entered.set()
            await asyncio.Event().wait()
        data = json.loads(messages[1]["content"])
        if task == "evidence":
            result = {"cards": [{"paper_id": paper["id"], "evidence": [
                {"category": "finding", "text": "Evidence described in the abstract.", "quote": paper["abstract"][:200]}]
            } for paper in data["papers"]]}
        elif task == "paper_selection":
            result = {"papers": [{"id": item["id"], "theme": "Fixture theme", "reason": "Covers a distinct setting."}
                                 for item in data["candidates"][:data["count"]]]}
        elif task == "outline":
            # Up to ten papers per paragraph, as the planner allows; the rest are excluded with a reason.
            ids = [paper["id"] for paper in data["papers"]][:10]
            spare = [paper["id"] for paper in data["papers"]][10:]
            count = data["section_count"]
            result = {"focus": "Compare methods and their study settings.",
                      "excluded": [{"paper_id": paper, "reason": "Fixture: outside the planned argument."} for paper in spare],
                      "sections": [
                {"id": "theme" if i == 0 else "summary" if count >= 5 and i == count - 1 else f"theme_{i + 1}",
                 "title": "Summary" if count >= 5 and i == count - 1 else f"Common findings {i + 1}",
                 "kind": "conclusion" if count >= 5 and i == count - 1 else "theme",
                 "goal": "Compare the selected evidence.", "paper_ids": ids,
                 "paragraphs": [{"goal": "Compare findings across study settings.", "paper_ids": ids}]
                 * data.get("paragraphs_per_section", 1)}
                for i in range(count)]}
        elif task in {"section", "section_repair", "section_edit"}:
            result = {"paragraphs": [{"text": _inline(data["abstract_evidence"]),
                                       "evidence_ids": [item["id"] for item in data["abstract_evidence"]]}
                                      for _ in data["section"].get("paragraphs") or [None]]}
        elif task == "section_condense":
            result = {"text": _inline(data["abstract_evidence"]),
                      "evidence_ids": data["paragraph"]["evidence_ids"]}
        elif task == "outline_assign":
            room = {target["id"]: target["available_slots"] for target in data["targets"]}
            result = {}
            for paper in data["papers"]:
                target = next((key for key, free in room.items() if free > 0), None)
                if target:
                    room[target] -= 1
                result[paper["id"]] = {"target": target or "exclude", "reason": "" if target else "Fixture: no room."}
        elif task == "support_check":
            result = {"findings": [{"paragraph": p["index"], "status": "supported", "reason": "Fixture evidence check."}
                                    for p in data["paragraphs"]],
                      "omissions": [{"paper_id": item["paper_id"], "status": "accepted", "reason": "A valid editorial choice."}
                                    for item in data.get("omitted_papers", [])]}
        elif task == "consistency_check":
            result = {"has_material_issue": False, "findings": []}
        else:
            raise AssertionError(task)
        return json.dumps(result)


def _inline(evidence) -> str:
    """Name each cited study beside its own sentence, as the writing prompt asks."""
    papers = dict.fromkeys(item["paper_id"] for item in evidence)
    return " ".join(f"One study reports a related finding ({paper})." for paper in papers)


def create_source(directory: Path, count: int = 2):
    fixture_data = json.loads((Path(__file__).parent / "m1_fixed_source.json").read_text(encoding="utf-8"))
    template = fixture_data["records"][0]
    fixture_data["records"] = []
    for index in range(1, count + 1):
        paper = json.loads(json.dumps(template))
        paper.update(uid=f"fixed-{index:03}", title=f"Synthetic review study {index}",
                     abstract=f"Study {index} compares durable workflows and recovery boundaries under setting {index}.")
        fixture_data["records"].append(paper)
    fixture_data["llm_responses"][-2] = json.dumps({"scores": [[i, 9] for i in range(1, count + 1)]})
    fixture_data["llm_responses"][-1] = json.dumps({"reasons": [[i, "Relevant fixture."] for i in range(1, count + 1)]})
    fixture = directory / "review-source.json"
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    return execute_fixture_search(directory / "paperseek.db", fixture, fixture_data["question"], owner_id="fixture")


import unittest

from paperseek_app.execution import CancellationScope
from paperseek_core.models import FinalPaperResult
from paperseek_core.review.evidence import snapshot, validate_cards
from paperseek_core.review.models import EvidenceBatch
from paperseek_core.review.planning import (
    prepare_outline,
)


class ReviewCase(unittest.IsolatedAsyncioTestCase):
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

    def corpus(self, count):
        papers = [snapshot(FinalPaperResult(rank=i, uid=str(i), title=f"Study {i}", abstract=f"Finding {i}."),
                           "source", i) for i in range(1, count + 1)]
        cards = [validate_cards(EvidenceBatch(cards=[{"paper_id": p.id, "evidence": [{
            "category": "finding", "text": p.abstract, "quote": p.abstract}]}]), [p], "test")[0] for p in papers]
        return papers, cards

    async def outline_for(self, papers, cards, count):
        return await prepare_outline(self.llm, CancellationScope(), topic="Themes", language="en", length="standard",
                                     section_count=count, cards=cards, papers=papers)
