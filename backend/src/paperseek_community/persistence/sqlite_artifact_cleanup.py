from __future__ import annotations

from datetime import timedelta

from paperseek_community.persistence.sqlite_base import SQLiteRepositoryBase

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from paperseek_app.errors import (
    RunError,
    RunNotFound,
    RunOwnershipLost,
    RunPersistenceError,
    RunStateError,
)
from paperseek_app.models import CleanupLease, DeletionReceipt, StoredArtifact
from paperseek_community.persistence.sqlite_support import (
    MAX_CLEANUP_ATTEMPTS,
    TERMINAL_STATUSES,
    parse_datetime as _datetime,
    timestamp as _timestamp,
    utc as _utc,
)


class SQLiteArtifactCleanupRepository(SQLiteRepositoryBase):
    def get_artifact(self, artifact_id: str) -> StoredArtifact:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT a.*
                        FROM run_artifacts a
                        JOIN runs r ON r.id = a.run_id
                        WHERE a.id = :artifact_id AND r.deleted_at IS NULL
                        """
                    ),
                    {"artifact_id": artifact_id},
                ).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("The artifact metadata could not be read.") from exc
        if row is None:
            raise RunPersistenceError("The artifact metadata was not found.")
        return self._artifact(row)

    def list_artifacts(self, run_id: str) -> list[StoredArtifact]:
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """
                        SELECT a.*
                        FROM run_artifacts a
                        JOIN runs r ON r.id = a.run_id
                        WHERE a.run_id = :run_id AND r.deleted_at IS NULL
                        ORDER BY a.kind, a.schema_version, a.filename
                        """
                    ),
                    {"run_id": run_id},
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Run artifacts could not be read.") from exc
        return [self._artifact(row) for row in rows]

    def stage_run_deletion(self, run_id: str) -> DeletionReceipt:
        now = _timestamp(self.clock())
        with self._transaction() as connection:
            row = connection.execute(
                text("SELECT status, deleted_at, kind FROM runs WHERE id = :run_id"),
                {"run_id": run_id},
            ).mappings().one_or_none()
            if row is None or row["kind"] != "research":
                raise RunNotFound(f"Run {run_id} was not found.")
            if row["deleted_at"] is None and row["status"] not in TERMINAL_STATUSES:
                raise RunStateError("An active run cannot be deleted.")
            deleted_at = row["deleted_at"] or now
            if row["deleted_at"] is None:
                connection.execute(
                    text(
                        """
                        UPDATE runs
                        SET deleted_at = :deleted_at, updated_at = :deleted_at
                        WHERE id = :run_id AND deleted_at IS NULL
                        """
                    ),
                    {"run_id": run_id, "deleted_at": deleted_at},
                )
            connection.execute(
                text(
                    """
                    INSERT INTO run_cleanup_jobs (
                        run_id, status, attempt, lease_owner, lease_expires_at,
                        error_code, created_at, updated_at
                    ) VALUES (
                        :run_id, 'pending', 0, NULL, NULL, '', :now, :now
                    )
                    ON CONFLICT(run_id) DO NOTHING
                    """
                ),
                {"run_id": run_id, "now": now},
            )
        return DeletionReceipt(
            run_id=run_id,
            status="cleanup_pending",
            deleted_at=_datetime(deleted_at),
        )

    def active_run_ids(self) -> list[str]:
        """Every unfinished task: searches and review-writing jobs alike."""
        return self._run_ids("status NOT IN :terminal")

    def history_run_ids(self) -> list[str]:
        """Finished searches still shown in history."""
        return self._run_ids("kind = 'research' AND status IN :terminal")

    def _run_ids(self, condition: str) -> list[str]:
        query = text(f"SELECT id FROM runs WHERE deleted_at IS NULL AND {condition} ORDER BY created_at, id")
        try:
            with self.engine.connect() as connection:
                return list(connection.execute(
                    query.bindparams(bindparam("terminal", expanding=True)),
                    {"terminal": sorted(TERMINAL_STATUSES)},
                ).scalars())
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Runs could not be read.") from exc

    def claim_cleanup_job(
        self,
        owner_id: str,
        lease_for: timedelta,
    ) -> CleanupLease | None:
        owner_id = owner_id.strip()
        if not owner_id or lease_for <= timedelta(0):
            raise ValueError("Cleanup claims require an owner and positive lease.")
        now_value = _utc(self.clock())
        now = _timestamp(now_value)
        expires_at = _timestamp(now_value + lease_for)
        with self._transaction() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, run_id, attempt
                    FROM run_cleanup_jobs
                    WHERE attempt < :max_attempts
                      AND (
                          status IN ('pending', 'failed')
                          OR (status = 'running' AND lease_expires_at <= :now)
                      )
                    ORDER BY created_at, id
                    LIMIT 1
                    """
                ),
                {"max_attempts": MAX_CLEANUP_ATTEMPTS, "now": now},
            ).mappings().one_or_none()
            if row is None:
                return None
            attempt = int(row["attempt"]) + 1
            updated = connection.execute(
                text(
                    """
                    UPDATE run_cleanup_jobs
                    SET status = 'running',
                        attempt = :attempt,
                        lease_owner = :owner_id,
                        lease_expires_at = :expires_at,
                        error_code = '',
                        updated_at = :now
                    WHERE id = :job_id
                      AND attempt = :previous_attempt
                      AND (
                          status IN ('pending', 'failed')
                          OR (status = 'running' AND lease_expires_at <= :now)
                      )
                    """
                ),
                {
                    "attempt": attempt,
                    "owner_id": owner_id,
                    "expires_at": expires_at,
                    "now": now,
                    "job_id": int(row["id"]),
                    "previous_attempt": int(row["attempt"]),
                },
            )
            if updated.rowcount != 1:
                return None
        return CleanupLease(
            job_id=int(row["id"]),
            run_id=row["run_id"],
            owner_id=owner_id,
            attempt=attempt,
            expires_at=_datetime(expires_at),
        )

    def list_cleanup_artifacts(self, lease: CleanupLease) -> list[StoredArtifact]:
        now = _timestamp(self.clock())
        try:
            with self.engine.connect() as connection:
                current = connection.execute(
                    text(
                        """
                        SELECT id
                        FROM run_cleanup_jobs
                        WHERE id = :job_id
                          AND run_id = :run_id
                          AND status = 'running'
                          AND attempt = :attempt
                          AND lease_owner = :owner_id
                          AND lease_expires_at > :now
                        """
                    ),
                    {
                        "job_id": lease.job_id,
                        "run_id": lease.run_id,
                        "attempt": lease.attempt,
                        "owner_id": lease.owner_id,
                        "now": now,
                    },
                ).one_or_none()
                if current is None:
                    raise RunOwnershipLost("The cleanup lease was lost.")
                rows = connection.execute(
                    text(
                        """
                        SELECT * FROM run_artifacts
                        WHERE run_id = :run_id
                        ORDER BY kind, schema_version, filename
                        """
                    ),
                    {"run_id": lease.run_id},
                ).mappings().all()
        except RunError:
            raise
        except SQLAlchemyError as exc:
            raise RunPersistenceError("Cleanup artifacts could not be read.") from exc
        return [self._artifact(row) for row in rows]

    def complete_cleanup(self, lease: CleanupLease) -> None:
        now = _timestamp(self.clock())
        with self._transaction() as connection:
            current = connection.execute(
                text(
                    """
                    SELECT id
                    FROM run_cleanup_jobs
                    WHERE id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                      AND attempt = :attempt
                      AND lease_owner = :owner_id
                      AND lease_expires_at > :now
                    """
                ),
                {
                    "job_id": lease.job_id,
                    "run_id": lease.run_id,
                    "attempt": lease.attempt,
                    "owner_id": lease.owner_id,
                    "now": now,
                },
            ).one_or_none()
            if current is None:
                raise RunOwnershipLost("The cleanup lease was lost before completion.")
            deleted = connection.execute(
                text("DELETE FROM runs WHERE id = :run_id AND deleted_at IS NOT NULL"),
                {"run_id": lease.run_id},
            )
            if deleted.rowcount != 1:
                raise RunOwnershipLost("The deleted run could not be finalized.")

    def fail_cleanup(self, lease: CleanupLease, error_code: str) -> None:
        now = _timestamp(self.clock())
        with self._transaction() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE run_cleanup_jobs
                    SET status = 'failed',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        error_code = :error_code,
                        updated_at = :now
                    WHERE id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                      AND attempt = :attempt
                      AND lease_owner = :owner_id
                    """
                ),
                {
                    "error_code": error_code,
                    "now": now,
                    "job_id": lease.job_id,
                    "run_id": lease.run_id,
                    "attempt": lease.attempt,
                    "owner_id": lease.owner_id,
                },
            )
            if updated.rowcount != 1:
                raise RunOwnershipLost("The cleanup lease was lost before failure was recorded.")


__all__ = ["SQLiteArtifactCleanupRepository"]
