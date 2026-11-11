"""Review job transactions reuse the run queue, fencing and credential lifetime."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from paperseek_app.models import LeaseToken, PendingEvent, RunDetail, RunVersions
from paperseek_app.review.models import (
    ReviewContent, ReviewDetail, ReviewError, ReviewJobContext, ReviewJobRequest,
)
from paperseek_community.persistence.sqlite import SQLiteRunUnitOfWork
from paperseek_community.persistence.sqlite_reviews import SQLiteReviewRepository
from paperseek_community.persistence.sqlite_support import json_dump, timestamp, utc
from paperseek_core.review.evidence import PROMPT_VERSION, validate_outline


class SQLiteReviewUnitOfWork(SQLiteReviewRepository, SQLiteRunUnitOfWork):
    def submit_review_job(self, review_id: str, request: ReviewJobRequest, *,
                          route: dict, credentials: dict, idempotency_key: str, request_digest: str) -> RunDetail:
        now_value = utc(self.clock())
        now = timestamp(now_value)
        with self._transaction() as connection:
            # Take the SQLite write lock before checking idempotency or activity.
            connection.execute(text("UPDATE reviews SET version=version WHERE id=:id AND deleted_at IS NULL"),
                               {"id": review_id})
            prior = connection.execute(text("""SELECT run_id,request_digest FROM review_jobs
                WHERE review_id=:id AND idempotency_key=:key"""),
                {"id": review_id, "key": idempotency_key}).mappings().one_or_none()
            if prior:
                if prior["request_digest"] != request_digest:
                    raise ReviewError("idempotency_conflict")
                return self._get_run_in_transaction(connection, prior["run_id"])
            review = self._reserve_edit(connection, review_id, request.expected_version)
            content = review.content
            if request.action != "prepare_outline":
                if not content.outline or request.outline_revision != content.outline_revision:
                    raise ReviewError("review_outline_confirmation_required")
                validate_outline(content.outline, [card for card in content.cards
                                                  if content.evidence_errors.get(card.paper_id) != "review_evidence_unusable"])
                if request.action == "rewrite_section":
                    if request.section_id not in {s.id for s in content.outline.sections}:
                        raise ReviewError("review_section_not_found", status=404)
                    previous = next((s for s in content.sections if s.section_id == request.section_id), None)
                    if previous is None:
                        raise ReviewError("review_section_not_found", status=404)
                    if request.section_revision != previous.revision:
                        raise ReviewError("review_version_conflict")
                content.approved_outline_revision = request.outline_revision
            content.model_route = route
            self._content(connection, review)
            run_id = self._insert_run(
                connection, question=content.topic, source="review", config_snapshot={"llm": route},
                versions=RunVersions(workflow_version="review.stage.v1", prompt_version=PROMPT_VERSION),
                owner_subject="community", now=now, deadline_at=timestamp(now_value + timedelta(hours=2)),
            )
            connection.execute(text("UPDATE runs SET kind='review' WHERE id=:id"), {"id": run_id})
            context = ReviewJobContext(review_id=review_id, run_id=run_id, action=request.action,
                                       outline_revision=request.outline_revision, section_id=request.section_id,
                                       section_revision=request.section_revision, instruction=request.instruction)
            connection.execute(text("""INSERT INTO review_jobs
                (run_id,review_id,context_json,idempotency_key,request_digest) VALUES (:run,:review,:context,:key,:digest)"""),
                {"run": run_id, "review": review_id, "context": context.model_dump_json(),
                 "key": idempotency_key, "digest": request_digest})
            self.credential_store.save(run_id, credentials, connection=connection)
        return self.get_run(run_id)

    def get_review_job(self, review_id: str, run_id: str) -> RunDetail:
        with self.engine.connect() as connection:
            self._review(connection, review_id)
            found = connection.execute(text("SELECT run_id FROM review_jobs WHERE review_id=:review AND run_id=:run"),
                                       {"review": review_id, "run": run_id}).scalar_one_or_none()
            if found is None:
                raise ReviewError("review_job_not_found", status=404)
            return self._get_run_in_transaction(connection, run_id)

    def review_job_context(self, run_id: str) -> ReviewJobContext:
        with self.engine.connect() as connection:
            row = connection.execute(text("""SELECT j.context_json FROM review_jobs j JOIN reviews v ON v.id=j.review_id
                WHERE j.run_id=:run AND v.deleted_at IS NULL"""), {"run": run_id}).scalar_one_or_none()
        if row is None:
            raise ReviewError("review_job_not_found", status=404)
        return ReviewJobContext.model_validate_json(row)

    def persist_review_progress(self, lease: LeaseToken, review_id: str, expected_version: int,
                                content: ReviewContent, events: list[PendingEvent], *,
                                part: str = "", complete: bool = False) -> ReviewDetail:
        now = timestamp(self.clock())
        with self._transaction() as connection:
            sequence = self._reserve_sequences(connection, lease, len(events), now)
            context_row = connection.execute(text("SELECT context_json FROM review_jobs WHERE run_id=:run AND review_id=:id"),
                                              {"run": lease.run_id, "id": review_id}).scalar_one_or_none()
            if context_row is None:
                raise ReviewError("review_job_not_found", status=404)
            context = ReviewJobContext.model_validate_json(context_row)
            review = self._review(connection, review_id)
            if review.version != expected_version or not review.active_job or review.active_job.id != lease.run_id:
                raise ReviewError("review_version_conflict")
            if context.action != "prepare_outline" and review.content.outline_revision != context.outline_revision:
                raise ReviewError("review_version_conflict")
            connection.execute(text("""UPDATE reviews SET version=version+1,document_json=:doc,updated_at=:now
                WHERE id=:id AND version=:version"""),
                {"id": review_id, "version": expected_version, "doc": content.model_dump_json(), "now": now})
            self._insert_events(connection, lease.run_id, sequence - len(events) + 1, events, now)
            if part == "outline":
                if content.outline is None or content.outline_revision != review.content.outline_revision + 1:
                    raise ReviewError("review_version_conflict")
                self._revision(connection, review_id, part, content.outline_revision, "model",
                               content.outline.model_dump_json(), now)
            elif part:
                body = next(s for s in content.sections if s.section_id == part)
                prior = next((s for s in review.content.sections if s.section_id == part), None)
                if body.revision != (prior.revision + 1 if prior else 1):
                    raise ReviewError("review_version_conflict")
                self._revision(connection, review_id, part, body.revision, "model", body.model_dump_json(), now)
            if complete:
                active = {s.id for s in content.outline.sections} if content.outline else set()
                warning = bool(content.evidence_errors or content.consistency_findings) or any(
                    s.validation == "needs_revision" or s.stale for s in content.sections if s.section_id in active)
                connection.execute(text("""UPDATE runs SET status=:status,current_step='completed',
                    warning_codes_json=:warnings,lease_owner=NULL,lease_expires_at=NULL,finished_at=:now,updated_at=:now
                    WHERE id=:id"""),
                    {"id": lease.run_id, "now": now, "status": "completed_with_warnings" if warning else "completed",
                     "warnings": json_dump(["review_needs_attention"] if warning else [])})
                connection.execute(text("DELETE FROM run_credentials WHERE run_id=:id"), {"id": lease.run_id})
            elif events:
                connection.execute(text("UPDATE runs SET current_step=:step WHERE id=:id"),
                                   {"id": lease.run_id, "step": events[-1].stage})
        return self.get_review(review_id)

    def delete_review(self, review_id: str, expected_version: int) -> None:
        now = timestamp(self.clock())
        with self._transaction() as connection:
            self._reserve_edit(connection, review_id, expected_version)
            jobs = connection.execute(text("SELECT run_id FROM review_jobs WHERE review_id=:id"),
                                      {"id": review_id}).scalars().all()
            for run_id in jobs:
                connection.execute(text("UPDATE runs SET deleted_at=:now,updated_at=:now WHERE id=:run"),
                                   {"run": run_id, "now": now})
                connection.execute(text("""INSERT INTO run_cleanup_jobs
                    (run_id,status,attempt,lease_owner,lease_expires_at,error_code,created_at,updated_at)
                    VALUES (:run,'pending',0,NULL,NULL,'',:now,:now) ON CONFLICT(run_id) DO NOTHING"""),
                    {"run": run_id, "now": now})
                connection.execute(text("DELETE FROM run_credentials WHERE run_id=:run"), {"run": run_id})
            # Keep the idempotency tombstone but discard user content and version history.
            connection.execute(text("DELETE FROM review_revisions WHERE review_id=:id"), {"id": review_id})
            connection.execute(text("DELETE FROM review_jobs WHERE review_id=:id"), {"id": review_id})
            connection.execute(text("UPDATE reviews SET deleted_at=:now,document_json='{}' WHERE id=:id"),
                               {"id": review_id, "now": now})
