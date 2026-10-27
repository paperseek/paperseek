from __future__ import annotations

from datetime import datetime
from typing import Any

from paperseek_community.persistence.sqlite_base import SQLiteRepositoryBase

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from paperseek_app.errors import RunNotFound, RunOwnershipLost, RunPersistenceError
from paperseek_app.models import ArtifactRole, LeaseToken, PendingEvent, PersistedResult, ResultPage, HistoryItem, HistoryPage, RunDetail, RunStatus, StoredArtifact
from paperseek_app.state import validate_transition
from paperseek_community.persistence.sqlite_support import (
    json_dump as _json_dump,
    json_load as _json_load,
    parse_datetime as _datetime,
    timestamp as _timestamp,
)
from paperseek_core.acceptance import ACCEPTANCE_FILTERS
from paperseek_core.journal_metrics import JOURNAL_FILTERS, JOURNAL_FLAGS, PLATFORMS
from paperseek_core.models import FinalPaperResult, ResearchResult


class SQLiteResultHistoryRepository(SQLiteRepositoryBase):
    def complete_run(
        self,
        lease: LeaseToken,
        events: list[PendingEvent],
        result: ResearchResult | None,
        artifacts: list[StoredArtifact],
    ) -> RunDetail:
        now = _timestamp(self.clock())
        result_count = len(result.ranked) if result is not None else 0
        warning_count = len(result.warnings) if result is not None else 0
        terminal_status = (
            RunStatus.COMPLETED_WITH_WARNINGS
            if warning_count
            else RunStatus.COMPLETED
        )
        terminal = PendingEvent(
            event_type=f"run.{terminal_status.value}",
            stage="lifecycle",
            payload={"result_count": result_count, "warning_count": warning_count},
        )
        all_events = [*events, terminal]
        with self._transaction() as connection:
            end_sequence = self._reserve_sequences(connection, lease, len(all_events), now)
            if result is None:
                raise RunPersistenceError("A completed run requires a final result.")
            final_artifacts = [
                item
                for item in artifacts
                if item.kind == "final_result"
                and item.role is ArtifactRole.COMPLETION_REQUIRED
            ]
            if len(final_artifacts) != 1:
                raise RunPersistenceError(
                    "A completed run requires one final-result artifact."
                )
            self._insert_artifacts(connection, lease.run_id, artifacts)
            final_artifact_id = final_artifacts[0].id
            validate_transition(RunStatus.RUNNING, terminal_status)
            self._insert_events(
                connection,
                lease.run_id,
                end_sequence - len(all_events) + 1,
                all_events,
                now,
            )
            connection.execute(text("DELETE FROM run_results WHERE run_id = :run_id"), {"run_id": lease.run_id})
            for rank, record in enumerate(result.ranked, 1):
                connection.execute(
                    text(
                        """
                        INSERT INTO run_results (
                            run_id, rank, record_json, record_artifact_id
                        ) VALUES (
                            :run_id, :rank, :record_json, :record_artifact_id
                        )
                        """
                    ),
                    {
                        "run_id": lease.run_id,
                        "rank": rank,
                        "record_json": _json_dump(record.model_dump(mode="json")),
                        "record_artifact_id": final_artifact_id,
                    },
                )
            updated = connection.execute(
                text(
                    """
                    UPDATE runs
                    SET status = :completed,
                        current_step = 'completed',
                        warning_codes_json = :warning_codes_json,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        finished_at = :now,
                        updated_at = :now
                    WHERE id = :run_id
                      AND status = :running
                      AND lease_owner = :owner_id
                      AND lease_generation = :lease_generation
                    """
                ),
                {
                    "completed": terminal_status.value,
                    "running": RunStatus.RUNNING.value,
                    "warning_codes_json": _json_dump(result.warnings),
                    "now": now,
                    "run_id": lease.run_id,
                    "owner_id": lease.owner_id,
                    "lease_generation": lease.lease_generation,
                },
            )
            if updated.rowcount != 1:
                raise RunOwnershipLost("The run lease was lost before completion.")
        return self.get_run(lease.run_id)

    def fail_run(
        self,
        lease: LeaseToken,
        events: list[PendingEvent],
        error_code: str,
    ) -> RunDetail:
        now = _timestamp(self.clock())
        terminal = PendingEvent(
            event_type="run.failed",
            stage="lifecycle",
            payload={"code": error_code},
        )
        all_events = [*events, terminal]
        with self._transaction() as connection:
            end_sequence = self._reserve_sequences(connection, lease, len(all_events), now)
            validate_transition(RunStatus.RUNNING, RunStatus.FAILED)
            self._insert_events(
                connection,
                lease.run_id,
                end_sequence - len(all_events) + 1,
                all_events,
                now,
            )
            updated = connection.execute(
                text(
                    """
                    UPDATE runs
                    SET status = :failed,
                        current_step = 'failed',
                        error_code = :error_code,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        finished_at = :now,
                        updated_at = :now
                    WHERE id = :run_id
                      AND status = :running
                      AND lease_owner = :owner_id
                      AND lease_generation = :lease_generation
                    """
                ),
                {
                    "failed": RunStatus.FAILED.value,
                    "running": RunStatus.RUNNING.value,
                    "error_code": error_code,
                    "now": now,
                    "run_id": lease.run_id,
                    "owner_id": lease.owner_id,
                    "lease_generation": lease.lease_generation,
                },
            )
            if updated.rowcount != 1:
                raise RunOwnershipLost("The run lease was lost before failure was committed.")
        return self.get_run(lease.run_id)


    def get_results(
        self,
        run_id: str,
        *,
        after_rank: int = 0,
        limit: int | None = None,
        uids: list[str] | None = None,
    ) -> list[PersistedResult]:
        if after_rank < 0 or (limit is not None and limit <= 0):
            raise ValueError("Result page bounds are invalid.")
        statement = """
            SELECT rr.*
            FROM run_results rr
            JOIN runs r ON r.id = rr.run_id
            WHERE rr.run_id = :run_id
              AND rr.rank > :after_rank
              AND r.deleted_at IS NULL
        """
        parameters: dict[str, Any] = {"run_id": run_id, "after_rank": after_rank}
        if uids is not None:
            statement += " AND json_extract(rr.record_json, '$.uid') IN (SELECT value FROM json_each(:uids))"
            parameters["uids"] = _json_dump(uids)
        statement += " ORDER BY rr.rank"
        if limit is not None:
            statement += " LIMIT :limit"
            parameters["limit"] = limit
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    text(statement),
                    parameters,
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Run results could not be read.") from exc
        return [
            PersistedResult(
                run_id=row["run_id"],
                rank=int(row["rank"]),
                record=FinalPaperResult.model_validate(_json_load(row["record_json"])),
                record_artifact_id=row["record_artifact_id"] or "",
            )
            for row in rows
        ]

    def query_results(
        self, run_id: str, *, offset: int, limit: int, query: str | None,
        min_score: float | None, metadata: str | None, sort: str,
        journal: str | None = None, acceptance: str | None = None,
    ) -> ResultPage:
        fields = ("title", "authors_text", "abstract", "doi", "venue", "source", "keywords_text")
        search_text = " || ' ' || ".join(f"COALESCE(json_extract(record_json, '$.{field}'), '')" for field in fields)
        base = """
            WITH records AS (
                SELECT rr.*, json_extract(record_json, '$.score') AS score
                FROM run_results rr JOIN runs r ON r.id = rr.run_id
                WHERE rr.run_id = :run_id AND r.deleted_at IS NULL
            )
        """
        conditions = []
        parameters = {"run_id": run_id, "offset": offset, "limit": limit}
        if query and query.strip():
            conditions.append(f"instr(paperseek_casefold({search_text}), :query) > 0")
            parameters["query"] = query.strip().casefold()
        if min_score is not None:
            conditions.append("score >= :min_score")
            parameters["min_score"] = min_score
        if metadata:
            field = {"abstract": "abstract", "doi": "doi", "pdf": "pdf_url"}[metadata]
            conditions.append(f"paperseek_strip(COALESCE(json_extract(record_json, '$.{field}'), '')) != ''")
        if journal:
            platform, value_field, best = JOURNAL_FILTERS[journal]
            if not platform:
                conditions.append("(" + " OR ".join(
                    f"json_extract(record_json, '$.journal_metrics.{name}') IS NOT NULL" for name in PLATFORMS) + ")")
            elif value_field in JOURNAL_FLAGS:
                conditions.append(f"json_extract(record_json, '$.journal_metrics.{platform}.{value_field}') = 1")
            else:
                conditions.append(f"json_extract(record_json, '$.journal_metrics.{platform}.{value_field}') <= :journal_best")
                parameters["journal_best"] = best
        if acceptance:
            conditions.append("COALESCE(json_extract(record_json, '$.acceptance.venue'), '') != ''"
                              " AND COALESCE(json_extract(record_json, '$.acceptance.tier'), '') IN (SELECT value FROM json_each(:tiers))")
            parameters["tiers"] = _json_dump(list(ACCEPTANCE_FILTERS[acceptance]))
        where = " AND ".join(conditions) or "1"
        ordering = {
            "rank": "rank",
            "score": "score IS NULL, score DESC, rank",
            "citations": "COALESCE(json_extract(record_json, '$.citation_count'), 0) DESC, rank",
            "year": "COALESCE(NULLIF(json_extract(record_json, '$.publish_year'), 0), json_extract(record_json, '$.year'), 0) DESC, rank",
            "impact": "COALESCE(json_extract(record_json, '$.journal_metrics.jcr.impact_factor'), -1) DESC, "
                      "COALESCE(json_extract(record_json, '$.journal_metrics.openalex.two_year_mean_citedness'), -1) DESC, rank",
        }[sort]
        try:
            with self.engine.connect() as connection:
                summary = connection.execute(text(base + f"""
                    SELECT COUNT(*) AS total_count, COUNT(score) AS scored_count,
                           COALESCE(SUM(score >= 5), 0) AS high_score_count,
                           MAX(score) AS max_score,
                           COALESCE(SUM(CASE WHEN {where} THEN 1 ELSE 0 END), 0) AS matched_count
                    FROM records
                """), parameters).mappings().one()
                rows = connection.execute(text(base + f"""
                    SELECT * FROM records WHERE {where}
                    ORDER BY {ordering} LIMIT :limit OFFSET :offset
                """), parameters).mappings().all()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Run results could not be queried.") from exc
        return ResultPage(
            **summary,
            results=[PersistedResult(
                run_id=row["run_id"], rank=row["rank"],
                record=FinalPaperResult.model_validate(_json_load(row["record_json"])),
                record_artifact_id=row["record_artifact_id"] or "",
            ) for row in rows],
        )

    def search_history(self, *, query: str, offset: int, limit: int) -> HistoryPage:
        where = """
            deleted_at IS NULL AND kind = 'research'
            AND instr(paperseek_casefold(display_title || ' ' || question || ' ' || source || ' ' || status || ' ' || error_code), :query) > 0
        """
        parameters = {"query": query.strip().casefold(), "offset": offset, "limit": limit}
        try:
            with self.engine.connect() as connection:
                count = connection.execute(text(f"SELECT COUNT(*) FROM runs WHERE {where}"), parameters).scalar_one()
                rows = connection.execute(text(f"""
                    SELECT r.id, r.display_title, r.question, r.source, r.status, r.error_code, r.warning_codes_json,
                           r.created_at, r.finished_at,
                           (SELECT COUNT(*) FROM run_results rr WHERE rr.run_id = r.id) AS result_count
                    FROM runs r WHERE {where}
                    ORDER BY r.created_at DESC, r.id DESC LIMIT :limit OFFSET :offset
                """), parameters).mappings().all()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Run history could not be searched.") from exc
        return HistoryPage(matched_count=count, items=[HistoryItem(
            display_title=row["display_title"],
            id=row["id"], question=row["question"], source=row["source"], status=row["status"],
            error_code=row["error_code"], result_count=row["result_count"],
            warnings=_json_load(row["warning_codes_json"]),
            created_at=_datetime(row["created_at"]), finished_at=_datetime(row["finished_at"]),
        ) for row in rows])

    def rename_history(self, run_id: str, title: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(text("""UPDATE runs SET display_title=:title
                WHERE id=:id AND deleted_at IS NULL AND kind='research' RETURNING id"""),
                {"id": run_id, "title": title.strip()}).one_or_none()
            if row is None:
                raise RunNotFound(run_id)

    def list_history(
        self,
        *,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> list[RunDetail]:
        if limit <= 0 or (before_created_at is None) != (before_id is None):
            raise ValueError("History page bounds are invalid.")
        cursor_clause = ""
        parameters: dict[str, Any] = {"limit": limit}
        if before_created_at is not None and before_id is not None:
            cursor_clause = """
                AND (
                    r.created_at < :before_created_at
                    OR (r.created_at = :before_created_at AND r.id < :before_id)
                )
            """
            parameters.update(
                {
                    "before_created_at": _timestamp(before_created_at),
                    "before_id": before_id,
                }
            )
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    text(
                        f"""
                        SELECT r.*,
                               (SELECT COUNT(*) FROM run_results rr WHERE rr.run_id = r.id) AS result_count
                        FROM runs r
                        WHERE r.deleted_at IS NULL AND r.kind = 'research'
                          {cursor_clause}
                        ORDER BY r.created_at DESC, r.id DESC
                        LIMIT :limit
                        """
                    ),
                    parameters,
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Run history could not be read.") from exc
        return [self._run_detail(row) for row in rows]


__all__ = ["SQLiteResultHistoryRepository"]
