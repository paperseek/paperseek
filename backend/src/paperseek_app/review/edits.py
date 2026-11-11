"""Storage-independent review summary and edit rules shared by both editions."""
from paperseek_app.review.models import OutlineSave, ReviewContent, ReviewDetail, ReviewError, ReviewSummary, SectionSave
from paperseek_core.review.evidence import fingerprint, validate_body, validate_outline
from paperseek_core.review.models import WrittenSection


class ReviewEdits:
    @staticmethod
    def summary(review: ReviewDetail) -> ReviewSummary:
        return ReviewEdits.summarize(
            review.content, review_id=review.id, display_title=review.display_title, updated_at=review.updated_at,
            latest_status=review.latest_job.status.value if review.latest_job else "",
            active=review.active_job is not None)

    @staticmethod
    def summarize(content: ReviewContent, *, review_id: str, display_title: str, updated_at,
                  latest_status: str, active: bool) -> ReviewSummary:
        """The list row for a review, from its document and the status of its latest task."""
        sections = content.outline.sections if content.outline else []
        current = {section.section_id: section for section in content.sections}
        complete = sum(section.id in current for section in sections)
        pending = sum(current[s.id].stale or current[s.id].validation == "needs_revision"
                      for s in sections if s.id in current)
        phase = "draft"
        if sections:
            phase = "complete" if complete == len(sections) else "outline"
        if pending:
            phase = "needs_attention"
        if content.consistency_findings:
            phase = "needs_attention"
        if active:
            phase = "writing" if content.approved_outline_revision else "outline"
        elif latest_status in {"failed", "cancelled"}:
            phase = "needs_attention"
        return ReviewSummary(id=review_id, display_title=display_title, topic=content.topic, paper_count=len(content.papers),
                             abstract_count=sum(p.has_abstract for p in content.papers),
                             completed_sections=complete, pending_sections=pending, section_count=len(sections), phase=phase,
                             active=active, updated_at=updated_at)

    def save_outline(self, review_id: str, request: OutlineSave) -> ReviewDetail:
        with self._transaction() as connection:
            review = self._reserve_edit(connection, review_id, request.expected_version)
            content = review.content
            if content.outline_revision != request.expected_revision:
                raise ReviewError("review_version_conflict")
            validate_outline(request.outline, [card for card in content.cards
                                              if content.evidence_errors.get(card.paper_id) != "review_evidence_unusable"])
            old = {section.id: section for section in content.outline.sections} if content.outline else {}
            new = {section.id: section for section in request.outline.sections}
            changed = {key for key in new if key not in old or new[key] != old[key]}
            if content.outline and content.outline.focus != request.outline.focus:
                changed.update(new)
            removed = set(old) - set(new)
            for body in content.sections:
                if body.section_id in changed | removed or ((changed or removed) and body.section_id in new
                                                 and new[body.section_id].kind != "theme"):
                    body.stale = True
            content.outline = request.outline
            content.section_count = len(request.outline.sections)
            content.outline_revision += 1
            content.approved_outline_revision = 0
            content.consistency_signature = ""
            content.consistency_findings = []
            self._content(connection, review)
            self._revision(connection, review_id, "outline", content.outline_revision, "user",
                           content.outline.model_dump_json(), self._edit_time(connection))
        return self.get_review(review_id)

    def save_section(self, review_id: str, section_id: str, request: SectionSave) -> ReviewDetail:
        with self._transaction() as connection:
            review = self._reserve_edit(connection, review_id, request.expected_version)
            content = review.content
            section = next((s for s in content.sections if s.section_id == section_id), None)
            plan = next((s for s in content.outline.sections if s.id == section_id), None) if content.outline else None
            if section is None or plan is None:
                raise ReviewError("review_section_not_found", status=404)
            if section.revision != request.expected_revision:
                raise ReviewError("review_version_conflict")
            validate_body(request.body, content.cards, plan.paper_ids)
            edited = WrittenSection(section_id=section_id, revision=section.revision + 1, body=request.body,
                                    author="user", signature=fingerprint([section.signature, request.body.model_dump()]),
                                    validation="user_edited")
            content.sections = [edited if s.section_id == section_id else s for s in content.sections]
            content.consistency_signature = ""
            content.consistency_findings = []
            if plan.kind == "theme":
                summary_ids = {s.id for s in content.outline.sections if s.kind != "theme"}
                for body in content.sections:
                    if body.section_id in summary_ids:
                        body.stale = True
            self._content(connection, review)
            self._revision(connection, review_id, section_id, edited.revision, "user", edited.model_dump_json(),
                           self._edit_time(connection))
        return self.get_review(review_id)

