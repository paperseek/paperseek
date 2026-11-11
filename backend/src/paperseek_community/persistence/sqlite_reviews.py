"""Community review snapshots and editable versions, separate from search history."""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from paperseek_app.review.models import (
    ReviewContent, ReviewDetail, ReviewError, ReviewGenerationFailure, ReviewJobSummary, ReviewPage, ReviewRevision,
)
from paperseek_core.review.models import WrittenSection
from paperseek_community.persistence.sqlite_base import SQLiteRepositoryBase
from paperseek_community.persistence.sqlite_support import TERMINAL_STATUSES, timestamp


from paperseek_app.review.edits import ReviewEdits


class SQLiteReviewRepository(ReviewEdits, SQLiteRepositoryBase):
    def _edit_time(self, connection):
        return timestamp(self.clock())

    def create_review(self, source_run_id: str, content: ReviewContent,
                      idempotency_key: str, request_digest: str) -> ReviewDetail:
        now = timestamp(self.clock())
        with self._transaction() as connection:
            connection.execute(text("""INSERT INTO reviews
                (id,source_run_id,document_json,version,idempotency_key,request_digest,created_at,updated_at)
                VALUES (:id,:source,:doc,1,:key,:digest,:now,:now)
                ON CONFLICT(idempotency_key) DO NOTHING"""),
                {"id": str(uuid4()), "source": source_run_id, "doc": content.model_dump_json(),
                 "key": idempotency_key, "digest": request_digest, "now": now})
            prior = connection.execute(text("SELECT id,request_digest,deleted_at FROM reviews WHERE idempotency_key=:key"),
                                       {"key": idempotency_key}).mappings().one()
            if prior["request_digest"] != request_digest or prior["deleted_at"]:
                raise ReviewError("idempotency_conflict")
            review_id = prior["id"]
        return self.get_review(review_id)

    def get_review(self, review_id: str) -> ReviewDetail:
        with self.engine.connect() as connection:
            # sqlite3 legacy transaction mode does not BEGIN on SELECT. Document
            # and job status must come from one snapshot, including at completion.
            connection.exec_driver_sql("BEGIN")
            return self._review(connection, review_id)

    def _review(self, connection: Connection, review_id: str) -> ReviewDetail:
        row = connection.execute(text("SELECT * FROM reviews WHERE id=:id AND deleted_at IS NULL"),
                                 {"id": review_id}).mappings().one_or_none()
        if row is None:
            raise ReviewError("review_not_found", status=404)
        latest_id = connection.execute(text("""SELECT j.run_id FROM review_jobs j JOIN runs r ON r.id=j.run_id
            WHERE j.review_id=:id AND r.deleted_at IS NULL ORDER BY r.created_at DESC,r.id DESC LIMIT 1"""),
            {"id": review_id}).scalar_one_or_none()
        latest = self._get_run_in_transaction(connection, latest_id) if latest_id else None
        latest_request = None
        if latest_id:
            context = connection.execute(text("SELECT context_json FROM review_jobs WHERE run_id=:id"),
                                         {"id": latest_id}).scalar_one_or_none()
            if context:
                latest_request = ReviewJobSummary.model_validate(
                    {key: value for key, value in json.loads(context).items() if key in {"action", "section_id", "instruction"}})
        failure = None
        if latest and latest.error_code == "review_model_output_invalid":
            payload = connection.execute(text("""SELECT payload_json FROM run_events
                WHERE run_id=:id AND event_type='review.output.invalid' ORDER BY sequence DESC LIMIT 1"""),
                {"id": latest.id}).scalar_one_or_none()
            if payload:
                failure = ReviewGenerationFailure.model_validate_json(payload)
        elif latest and latest.error_code in {"review_evidence_incomplete", "review_insufficient_evidence"}:
            payloads = connection.execute(text("""SELECT payload_json FROM run_events WHERE run_id=:id
                AND event_type IN ('review.evidence.completed','review.evidence.failed') ORDER BY sequence"""),
                {"id": latest.id}).scalars().all()
            reasons = [value["reason"] for payload in payloads if (value := json.loads(payload)).get("reason")]
            if reasons:
                failure = ReviewGenerationFailure(task="evidence", reason="\n".join(reasons)[:2000])
        return ReviewDetail(
            display_title=row["display_title"],
            id=row["id"], source_run_id=row["source_run_id"], version=row["version"],
            content=ReviewContent.model_validate_json(row["document_json"]),
            latest_job=latest, active_job=latest if latest and latest.status.value not in TERMINAL_STATUSES else None,
            generation_failure=failure, latest_request=latest_request,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def rename_review(self, review_id: str, title: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(text("""UPDATE reviews SET display_title=:title
                WHERE id=:id AND deleted_at IS NULL RETURNING id"""),
                {"id": review_id, "title": title.strip()}).one_or_none()
            if row is None:
                raise ReviewError("review_not_found", status=404)

    def list_reviews(self, offset: int, limit: int, query: str = "") -> ReviewPage:
        params = {"query": query.strip().casefold(), "offset": offset, "limit": limit}
        where = "deleted_at IS NULL"
        if params["query"]:
            where += " AND instr(paperseek_casefold(display_title || ' ' || json_extract(document_json, '$.topic')), :query) > 0"
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            total = connection.execute(text(f"SELECT count(*) FROM reviews WHERE {where}"), params).scalar_one()
            rows = connection.execute(text(f"""SELECT id,display_title,document_json,updated_at FROM reviews WHERE {where}
                ORDER BY updated_at DESC,id DESC LIMIT :limit OFFSET :offset"""),
                params).mappings().all()
            # One query for the page's latest tasks; a row needs only that task's status.
            latest = dict(connection.execute(text("""SELECT review_id,status FROM (
                SELECT j.review_id AS review_id, r.status AS status, row_number() OVER (
                    PARTITION BY j.review_id ORDER BY r.created_at DESC,r.id DESC) AS position
                FROM review_jobs j JOIN runs r ON r.id=j.run_id
                WHERE r.deleted_at IS NULL AND j.review_id IN :ids) WHERE position=1""").bindparams(
                    bindparam("ids", expanding=True)), {"ids": [row["id"] for row in rows]}).all()) if rows else {}
        items = [self.summarize(
            ReviewContent.model_validate_json(row["document_json"]), review_id=row["id"],
            display_title=row["display_title"], updated_at=row["updated_at"],
            latest_status=latest.get(row["id"], ""),
            active=row["id"] in latest and latest[row["id"]] not in TERMINAL_STATUSES) for row in rows]
        return ReviewPage(items=items, total_count=total,
                          next_offset=offset + limit if offset + limit < total else None)

    def _reserve_edit(self, connection: Connection, review_id: str, expected_version: int) -> ReviewDetail:
        row = connection.execute(text("""UPDATE reviews SET version=version+1,updated_at=:now
            WHERE id=:id AND version=:version AND deleted_at IS NULL
            AND NOT EXISTS (SELECT 1 FROM review_jobs j JOIN runs r ON r.id=j.run_id
                WHERE j.review_id=:id AND r.deleted_at IS NULL AND r.status IN ('queued','running','cancelling'))
            RETURNING id"""),
            {"id": review_id, "version": expected_version, "now": timestamp(self.clock())}).one_or_none()
        if row is None:
            current = self._review(connection, review_id)
            raise ReviewError("review_busy" if current.active_job else "review_version_conflict")
        return self._review(connection, review_id)

    @staticmethod
    def _content(connection: Connection, review: ReviewDetail) -> None:
        connection.execute(text("UPDATE reviews SET document_json=:doc WHERE id=:id AND version=:version"),
                           {"id": review.id, "version": review.version, "doc": review.content.model_dump_json()})

    @staticmethod
    def _revision(connection: Connection, review_id: str, part: str, revision: int,
                  author: str, content: str, now: str) -> None:
        connection.execute(text("""INSERT INTO review_revisions (review_id,part,revision,author,content_json,created_at)
            VALUES (:id,:part,:revision,:author,:content,:now)"""),
            {"id": review_id, "part": part, "revision": revision, "author": author, "content": content, "now": now})

    def review_versions(self, review_id: str, part: str) -> list[ReviewRevision]:
        from paperseek_core.review.models import ReviewOutline

        with self.engine.connect() as connection:
            self._review(connection, review_id)
            rows = connection.execute(text("""SELECT * FROM review_revisions WHERE review_id=:id AND part=:part
                ORDER BY revision DESC"""), {"id": review_id, "part": part}).mappings().all()
        return [ReviewRevision(part=part, revision=row["revision"], author=row["author"], created_at=row["created_at"],
                               outline=ReviewOutline.model_validate_json(row["content_json"]) if part == "outline" else None,
                               section=WrittenSection.model_validate_json(row["content_json"]) if part != "outline" else None)
                for row in rows]
