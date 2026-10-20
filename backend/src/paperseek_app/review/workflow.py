"""Checkpointed review stages; human confirmation belongs to submission, not this loop."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic

from paperseek_app.execution import run_blocking
from paperseek_app.models import LeaseToken, PendingEvent
from paperseek_app.review.models import (
    ReviewContent,
    ReviewDetail,
    ReviewError,
    ReviewJobContext,
)
from paperseek_app.review.ports import ReviewStore
from paperseek_core.errors import LLMRequestError
from paperseek_core.ports import CancellationToken, LLMPort
from paperseek_core.review.consistency import check_consistency
from paperseek_core.review.evidence import PROMPT_VERSION, fingerprint, usable_cards
from paperseek_core.review.generation import (
    write_section,
)
from paperseek_core.review.models import OutlineSection, ReviewPaper, WrittenSection
from paperseek_core.review.planning import (
    extract_evidence,
    prepare_outline,
)
from paperseek_core.review.structured import (
    ReviewOutputError,
)


# Evidence batches and sections written at the same time.
REVIEW_CONCURRENCY = 8

class ReviewStageWorkflow:
    def __init__(self, store: ReviewStore, lease: LeaseToken, review: ReviewDetail,
                 context: ReviewJobContext, llm: LLMPort, route: dict, cancellation: CancellationToken) -> None:
        self.store, self.lease, self.review = store, lease, review
        self.context, self.llm, self.route, self.cancellation = context, llm, route, cancellation
        self._writes = asyncio.Lock()

    async def persist(self, change: Callable[[ReviewContent], None], event: str, payload: dict,
                      *, part: str = "", complete: bool = False) -> None:
        async with self._writes:
            self.cancellation.raise_if_cancelled()
            content = self.review.content.model_copy(deep=True)
            change(content)
            self.review = await run_blocking(
                self.store.persist_review_progress, self.lease, self.review.id, self.review.version,
                content, [PendingEvent(event_type=event, stage="review." + self.context.action, payload=payload)],
                part=part, complete=complete,
            )

    async def run(self) -> None:
        if self.context.action == "prepare_outline":
            await self.prepare()
        else:
            await self.write()

    @staticmethod
    def batches(papers: list[ReviewPaper]) -> list[list[ReviewPaper]]:
        batches: list[list[ReviewPaper]] = []
        current: list[ReviewPaper] = []
        characters = 0
        for paper in papers:
            if current and (len(current) == 4 or characters + len(paper.abstract) > 24000):
                batches.append(current)
                current, characters = [], 0
            current.append(paper)
            characters += len(paper.abstract)
        if current:
            batches.append(current)
        return batches

    async def prepare(self) -> None:
        content = self.review.content
        signature = fingerprint([PROMPT_VERSION, content.corpus_hash, content.language, self.route])
        # Existing prose cites these immutable quotes, including when changing the writing model.
        usable_ids = {card.paper_id for card in usable_cards(content.cards, content.papers)}
        ready = {card.paper_id for card in content.cards
                 if content.sections or card.signature == signature and card.paper_id in usable_ids}
        papers = [paper for paper in content.papers if paper.has_abstract and paper.id not in ready]
        semaphore = asyncio.Semaphore(REVIEW_CONCURRENCY)

        async def extract(batch: list[ReviewPaper]) -> None:
            async with semaphore:
                self.cancellation.raise_if_cancelled()
                started = monotonic()
                diagnostics: list[str] = []
                try:
                    cards = await extract_evidence(self.llm, self.cancellation, batch, content.language, signature, diagnostics)
                except (LLMRequestError, ReviewOutputError) as exc:
                    def failed(value: ReviewContent) -> None:
                        for paper in batch:
                            value.evidence_errors[paper.id] = "review_evidence_failed"
                    await self.persist(failed, "review.evidence.failed", {
                        "paper_ids": [p.id for p in batch],
                        "reason": exc.reason if isinstance(exc, ReviewOutputError) else exc.code})
                    return

                def save(value: ReviewContent) -> None:
                    merged = {card.paper_id: card for card in value.cards}
                    usable_ids = {card.paper_id for card in usable_cards(cards, batch)}
                    for card in cards:
                        merged[card.paper_id] = card
                        value.evidence_errors.pop(card.paper_id, None)
                        if card.paper_id not in usable_ids:
                            value.evidence_errors[card.paper_id] = "review_evidence_unusable"
                    ready_ids = {card.paper_id for card in cards}
                    for paper in batch:
                        if paper.id not in ready_ids:
                            value.evidence_errors[paper.id] = "review_evidence_failed"
                    value.cards = list(merged.values())

                await self.persist(save, "review.evidence.completed", {
                    "paper_ids": [card.paper_id for card in cards], "elapsed_seconds": round(monotonic() - started, 3),
                    "reason": "\n".join(diagnostics)[:2000],
                })

        async with asyncio.TaskGroup() as group:
            for batch in self.batches(papers):
                group.create_task(extract(batch))
        cards = [card for card in self.review.content.cards if card.signature == signature or content.sections]
        if len(cards) < 2:
            raise ReviewError("review_insufficient_evidence", status=422)
        # A paper whose evidence still fails after its own retry is left out and flagged, rather than
        # blocking a review built from many other papers; a later run retries only those papers.
        carded = {card.paper_id for card in cards}
        failed = [paper.id for paper in content.papers if paper.has_abstract and paper.id not in carded]
        usable = usable_cards(cards, content.papers)
        if len(usable) < 2:
            raise ReviewError("review_insufficient_evidence", status=422)
        outline = await prepare_outline(self.llm, self.cancellation, topic=content.topic, language=content.language,
                                        length=content.length, cards=usable, papers=content.papers,
                                        paragraph_count=content.paragraph_count,
                                        section_count=content.section_count or (len(content.outline.sections)
                                                                               if content.outline else None))
        if content.sections and content.outline and len(outline.sections) == len(content.outline.sections):
            for section, previous in zip(outline.sections, content.outline.sections, strict=True):
                section.id = previous.id

        def save_outline(value: ReviewContent) -> None:
            usable_ids = {card.paper_id for card in usable}
            value.cards = cards
            value.evidence_errors = {card.paper_id: "review_evidence_unusable" for card in cards if card.paper_id not in usable_ids}
            value.evidence_errors.update({paper_id: "review_evidence_failed" for paper_id in failed})
            value.outline = outline
            value.section_count = len(outline.sections)
            value.outline_revision += 1
            value.approved_outline_revision = 0
            value.consistency_signature = ""
            value.consistency_findings = []
            for section in value.sections:
                section.stale = True

        await self.persist(save_outline, "review.outline.ready", {"paper_count": len(usable), "section_count": len(outline.sections)},
                           part="outline", complete=True)

    def section_signature(self, section: OutlineSection) -> str:
        content = self.review.content
        dependencies = []
        if section.kind != "theme" and content.outline:
            themes = {item.id for item in content.outline.sections if item.kind == "theme"}
            dependencies = [(body.section_id, body.revision) for body in content.sections if body.section_id in themes]
        # Unset custom sizes leave the key unchanged, so existing sections are not marked stale.
        size = [content.length] + ([content.paragraph_count, content.paragraph_length]
                                   if content.paragraph_count or content.paragraph_length else [])
        return fingerprint([PROMPT_VERSION, "writing.v4", content.topic, content.language, *size, self.route,
                            content.outline.focus if content.outline else "",
                            section.model_dump(), [card.model_dump() for card in content.cards
                                                   if card.paper_id in section.paper_ids], dependencies])

    async def write_one(self, section: OutlineSection, *, rewrite: bool = False) -> None:
        self.cancellation.raise_if_cancelled()
        content = self.review.content
        previous = next((s for s in content.sections if s.section_id == section.id), None)
        signature = self.section_signature(section)
        if previous:
            if rewrite and previous.job_id == self.lease.run_id:
                return
            if not rewrite and previous.author == "user":
                return
            if not rewrite and not previous.stale and previous.validation != "needs_revision" and (
                previous.author == "user" or previous.signature == signature
            ):
                return
        themes = {s.id for s in content.outline.sections if s.kind == "theme"}
        summaries = [{"section_id": body.section_id, "paragraphs": [p.model_dump() for p in body.body.paragraphs]}
                     for body in content.sections if body.section_id in themes] if section.kind != "theme" else []
        started = monotonic()
        diagnostics: list[str] = []
        body, findings = await write_section(
            self.llm, self.cancellation, topic=content.topic, language=content.language, length=content.length,
            paragraph_length=content.paragraph_length,
            outline=content.outline, section=section, cards=content.cards,
            papers=content.papers,
            instruction=self.context.instruction if rewrite else "", previous=previous.body if rewrite and previous else None,
            summaries=summaries, diagnostics=diagnostics,
        )

        def save(value: ReviewContent) -> None:
            current = next((s for s in value.sections if s.section_id == section.id), None)
            written = WrittenSection(
                section_id=section.id, revision=current.revision + 1 if current else 1, body=body,
                author="model", signature=signature, validation="supported" if all(
                    finding.status == "supported" for finding in findings) else "needs_revision",
                findings=findings, model=self.route.get("model", ""), job_id=self.lease.run_id,
            )
            value.sections = [s for s in value.sections if s.section_id != section.id] + [written]
            if section.kind == "theme":
                summaries = {s.id for s in value.outline.sections if s.kind != "theme"}
                for other in value.sections:
                    if other.section_id in summaries:
                        other.stale = True

        await self.persist(save, "review.section.completed", {
            "section_id": section.id, "elapsed_seconds": round(monotonic() - started, 3),
            "reason": "\n".join(diagnostics)[:2000],
        }, part=section.id)

    async def write(self) -> None:
        content = self.review.content
        if not content.outline or content.approved_outline_revision != self.context.outline_revision:
            raise ReviewError("review_outline_confirmation_required")
        if self.context.action == "rewrite_section":
            section = next((s for s in content.outline.sections if s.id == self.context.section_id), None)
            if section is None:
                raise ReviewError("review_section_not_found", status=404)
            await self.write_one(section, rewrite=True)
        else:
            semaphore = asyncio.Semaphore(REVIEW_CONCURRENCY)
            failures: list[ReviewOutputError] = []

            async def theme(section: OutlineSection) -> None:
                async with semaphore:
                    try:
                        await self.write_one(section)
                    except ReviewOutputError as exc:
                        # One section's invalid output must not cancel sections still being written;
                        # every completed section is persisted and a retry redoes only the rest.
                        failures.append(exc)

            async with asyncio.TaskGroup() as group:
                for section in content.outline.sections:
                    if section.kind == "theme":
                        group.create_task(theme(section))
            # Summaries consume the now-persisted theme paragraphs and citations, not each other,
            # so they are written side by side once every theme is done.
            async with asyncio.TaskGroup() as group:
                for section in content.outline.sections:
                    if section.kind != "theme":
                        group.create_task(theme(section))
            if failures:
                raise failures[0]
        content = self.review.content
        active = {section.id for section in content.outline.sections}
        signature = fingerprint([(s.section_id, s.revision) for s in content.sections if s.section_id in active])
        if signature != content.consistency_signature:
            findings = await check_consistency(self.llm, self.cancellation, content.outline, content.sections)

            def save_check(value: ReviewContent) -> None:
                value.consistency_findings = findings
                value.consistency_signature = signature

            await self.persist(save_check, "review.validation.completed", {"issue_count": len(findings)})
        await self.persist(lambda _: None, "review.completed", {"section_count": len(active)}, complete=True)
