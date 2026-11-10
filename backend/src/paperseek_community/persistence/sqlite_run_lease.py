from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from paperseek_community.persistence.sqlite_base import SQLiteRepositoryBase

from sqlalchemy import text
from sqlalchemy.engine import Connection

from paperseek_app.errors import (
    IdempotencyConflict,
    RunNotFound,
    RunOwnershipLost,
    RunPersistenceError,
    RunStateError,
)
from paperseek_app.models import LeaseToken, PendingEvent, RunDetail, RunStatus, RunVersions
from paperseek_app.state import validate_transition
from paperseek_community.credentials import CredentialPersistenceError
from paperseek_community.persistence.sqlite_support import (
    MAX_ATTEMPTS,
    TERMINAL_STATUSES,
    json_dump as _json_dump,
    parse_datetime as _datetime,
    timestamp as _timestamp,
    utc as _utc,
)


class SQLiteRunLeaseRepository(SQLiteRepositoryBase):
    def create_run(
        self,
        question: str,
        source: str,
        config_snapshot: dict,
        versions: RunVersions,
        deadline_for: timedelta,
    ) -> RunDetail:
        question = question.strip()
        source = source.strip()
        if not question or not source:
            raise ValueError("A run requires a question and source.")
        now_value = _utc(self.clock())
        if deadline_for <= timedelta(0):
            raise ValueError("A run deadline must be positive.")
        now = _timestamp(now_value)
        deadline_at = _timestamp(now_value + deadline_for)
        with self._transaction() as connection:
            run_id = self._insert_run(
                connection,
                question=question,
                source=source,
                config_snapshot=config_snapshot,
                versions=versions,
                owner_subject=None,
                now=now,
                deadline_at=deadline_at,
            )
        return self.get_run(run_id)

    def submit_run(
        self,
        subject: str,
        idempotency_key: str,
        request_digest: str,
        question: str,
        source: str,
        config_snapshot: dict,
        versions: RunVersions,
        deadline_for: timedelta,
    ) -> RunDetail:
        subject = subject.strip()
        idempotency_key = idempotency_key.strip()
        request_digest = request_digest.strip()
        question = question.strip()
        source = source.strip()
        if not all((subject, idempotency_key, request_digest, question, source)):
            raise ValueError("A public run submission requires complete identifiers and search input.")
        if deadline_for <= timedelta(0):
            raise ValueError("A run deadline must be positive.")
        now_value = _utc(self.clock())
        now = _timestamp(now_value)
        deadline_at = _timestamp(now_value + deadline_for)
        try:
            with self._transaction() as connection:
                prior = connection.execute(
                    text(
                        """
                        SELECT request_digest, run_id FROM run_submissions
                        WHERE subject = :subject AND idempotency_key = :idempotency_key
                        """
                    ),
                    {"subject": subject, "idempotency_key": idempotency_key},
                ).mappings().one_or_none()
                if prior is not None:
                    if prior["request_digest"] != request_digest:
                        raise IdempotencyConflict("The idempotency key belongs to another request.")
                    run_id = prior["run_id"]
                else:
                    run_id = self._insert_run(
                        connection,
                        question=question,
                        source=source,
                        config_snapshot=config_snapshot,
                        versions=versions,
                        owner_subject=subject,
                        now=now,
                        deadline_at=deadline_at,
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO run_submissions (
                                subject, idempotency_key, request_digest, run_id, created_at
                            ) VALUES (
                                :subject, :idempotency_key, :request_digest, :run_id, :created_at
                            )
                            """
                        ),
                        {
                            "subject": subject,
                            "idempotency_key": idempotency_key,
                            "request_digest": request_digest,
                            "run_id": run_id,
                            "created_at": now,
                        },
                    )
                    if self.submission_credentials is not None:
                        try:
                            self.credential_store.save(
                                run_id, self.submission_credentials, connection=connection,
                            )
                        except Exception as exc:
                            raise CredentialPersistenceError(
                                "Temporary credentials could not be stored safely."
                            ) from exc
        except RunPersistenceError as exc:
            with self.engine.connect() as connection:
                prior = connection.execute(
                    text(
                        """
                        SELECT request_digest, run_id FROM run_submissions
                        WHERE subject = :subject AND idempotency_key = :idempotency_key
                        """
                    ),
                    {"subject": subject, "idempotency_key": idempotency_key},
                ).mappings().one_or_none()
                if prior is not None:
                    if prior["request_digest"] != request_digest:
                        raise IdempotencyConflict("The idempotency key belongs to another request.") from exc
                    return self.get_run(prior["run_id"])
            raise
        return self.get_run(run_id)

    @classmethod
    def _insert_run(
        cls,
        connection: Connection,
        *,
        question: str,
        source: str,
        config_snapshot: dict,
        versions: RunVersions,
        owner_subject: str | None,
        now: str,
        deadline_at: str,
    ) -> str:
        run_id = str(uuid4())
        connection.execute(
            text(
                """
                INSERT INTO runs (
                    id, owner_subject, question, source, status, config_json, current_step,
                    attempt, lease_generation, last_event_sequence,
                    workflow_version, engine_version, prompt_version,
                    config_schema_version, artifact_schema_version,
                    error_code, warning_codes_json, created_at, deadline_at, updated_at
                ) VALUES (
                    :id, :owner_subject, :question, :source, :status, :config_json, '',
                    0, 0, 1,
                    :workflow_version, :engine_version, :prompt_version,
                    :config_schema_version, :artifact_schema_version,
                    '', '[]', :now, :deadline_at, :now
                )
                """
            ),
            {
                "id": run_id,
                "owner_subject": owner_subject,
                "question": question,
                "source": source,
                "status": RunStatus.QUEUED.value,
                "config_json": _json_dump(config_snapshot),
                "workflow_version": versions.workflow_version,
                "engine_version": versions.engine_version,
                "prompt_version": versions.prompt_version,
                "config_schema_version": versions.config_schema_version,
                "artifact_schema_version": versions.artifact_schema_version,
                "now": now,
                "deadline_at": deadline_at,
            },
        )
        cls._insert_events(
            connection,
            run_id,
            1,
            [PendingEvent(event_type="run.created", stage="lifecycle", payload={"source": source})],
            now,
        )
        return run_id

    def claim_run(self, run_id: str, owner_id: str, lease_for: timedelta) -> LeaseToken:
        owner_id = owner_id.strip()
        if not owner_id:
            raise ValueError("A lease owner is required.")
        now_value = _utc(self.clock())
        now = _timestamp(now_value)
        expires_at = _timestamp(now_value + lease_for)
        rejected_code = ""
        row = None
        with self._transaction() as connection:
            candidate = connection.execute(
                text(
                    """
                    SELECT status, attempt, lease_expires_at, deadline_at
                    FROM runs WHERE id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).mappings().one_or_none()
            if candidate is None:
                raise RunNotFound(f"Run {run_id} was not found.")
            if candidate["status"] in TERMINAL_STATUSES:
                raise RunStateError(f"Terminal run {run_id} cannot be claimed.")
            if candidate["status"] == RunStatus.CANCELLING.value:
                raise RunOwnershipLost(f"Run {run_id} is awaiting cancellation.")
            reclaim = (
                candidate["status"] == RunStatus.RUNNING.value
                and candidate["lease_expires_at"]
                and candidate["lease_expires_at"] <= now
            )
            if candidate["deadline_at"] <= now:
                rejected_code = "run_deadline_exceeded"
            elif reclaim and int(candidate["attempt"]) >= MAX_ATTEMPTS:
                rejected_code = "executor_recovery_exhausted"
            elif candidate["status"] != RunStatus.QUEUED.value and not reclaim:
                raise RunOwnershipLost(f"Run {run_id} already has a current lease.")

            if rejected_code:
                self._fail_unclaimable_run(connection, run_id, rejected_code, now)
            else:
                row = self._claim_candidate(
                    connection,
                    run_id,
                    owner_id,
                    expires_at,
                    now,
                )
        if rejected_code:
            raise RunStateError(f"Run {run_id} cannot be claimed: {rejected_code}.")
        assert row is not None
        return LeaseToken(
            run_id=run_id,
            owner_id=owner_id,
            lease_generation=int(row["lease_generation"]),
            expires_at=_datetime(row["lease_expires_at"]),
        )

    def claim_next_run(self, owner_id: str, lease_for: timedelta) -> LeaseToken | None:
        owner_id = owner_id.strip()
        if not owner_id:
            raise ValueError("A lease owner is required.")
        now_value = _utc(self.clock())
        now = _timestamp(now_value)
        expires_at = _timestamp(now_value + lease_for)
        row = None
        with self._transaction() as connection:
            locked = False
            while True:
                candidate = connection.execute(
                    text(
                        """
                        SELECT id, status, attempt, deadline_at
                        FROM runs
                        WHERE (:kind IS NULL OR kind = :kind) AND (status = :queued
                           OR (status = :running AND lease_expires_at <= :now)
                           OR (status = :cancelling AND lease_expires_at <= :now))
                        ORDER BY created_at, id
                        LIMIT 1
                        """
                    ),
                    {
                        "queued": RunStatus.QUEUED.value,
                        "kind": self.claim_kind,
                        "running": RunStatus.RUNNING.value,
                        "cancelling": RunStatus.CANCELLING.value,
                        "now": now,
                    },
                ).mappings().one_or_none()
                if candidate is None:
                    break
                if not locked:
                    # Empty polls must not block an uncommitted submission.
                    # Re-read under the write lock before mutating a candidate:
                    # Python's SQLite driver does not begin on SELECT alone.
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    locked = True
                    continue
                if candidate["status"] == RunStatus.CANCELLING.value:
                    self._cancel_expired_run(connection, candidate["id"], now)
                    continue
                error_code = ""
                if candidate["deadline_at"] <= now:
                    error_code = "run_deadline_exceeded"
                elif (
                    candidate["status"] == RunStatus.RUNNING.value
                    and int(candidate["attempt"]) >= MAX_ATTEMPTS
                ):
                    error_code = "executor_recovery_exhausted"
                if error_code:
                    self._fail_unclaimable_run(
                        connection,
                        candidate["id"],
                        error_code,
                        now,
                    )
                    continue
                row = self._claim_candidate(
                    connection,
                    candidate["id"],
                    owner_id,
                    expires_at,
                    now,
                )
                break
        if row is None:
            return None
        return LeaseToken(
            run_id=row["id"],
            owner_id=owner_id,
            lease_generation=int(row["lease_generation"]),
            expires_at=_datetime(row["lease_expires_at"]),
        )

    def _cancel_expired_run(self, connection: Connection, run_id: str, now: str) -> None:
        row = connection.execute(
            text(
                """
                UPDATE runs
                SET status = 'cancelled', current_step = 'cancelled',
                    lease_owner = NULL, lease_expires_at = NULL,
                    finished_at = :now, updated_at = :now,
                    last_event_sequence = last_event_sequence + 1
                WHERE id = :run_id AND status = 'cancelling'
                  AND lease_expires_at <= :now
                RETURNING last_event_sequence
                """
            ),
            {"run_id": run_id, "now": now},
        ).mappings().one_or_none()
        if row is not None:
            connection.execute(text("DELETE FROM run_credentials WHERE run_id = :run_id"), {"run_id": run_id})
            self._insert_events(
                connection, run_id, int(row["last_event_sequence"]),
                [PendingEvent(event_type="run.cancelled", stage="lifecycle")], now,
            )

    def _claim_candidate(
        self,
        connection: Connection,
        run_id: str,
        owner_id: str,
        expires_at: str,
        now: str,
    ):
        row = connection.execute(
            text(
                """
                UPDATE runs
                SET status = :running,
                    attempt = attempt + 1,
                    lease_generation = lease_generation + 1,
                    lease_owner = :owner_id,
                    lease_expires_at = CASE
                        WHEN :expires_at < deadline_at THEN :expires_at
                        ELSE deadline_at
                    END,
                    started_at = COALESCE(started_at, :now),
                    updated_at = :now,
                    last_event_sequence = last_event_sequence + 1
                WHERE id = :run_id
                  AND deadline_at > :now
                  AND (
                    status = :queued
                    OR (status = :running AND lease_expires_at <= :now)
                  )
                RETURNING id, lease_generation, attempt,
                          last_event_sequence, lease_expires_at
                """
            ),
            {
                "running": RunStatus.RUNNING.value,
                "queued": RunStatus.QUEUED.value,
                "owner_id": owner_id,
                "expires_at": expires_at,
                "now": now,
                "run_id": run_id,
            },
        ).mappings().one_or_none()
        if row is None:
            self._raise_claim_rejected(connection, run_id)
        self._insert_events(
            connection,
            run_id,
            int(row["last_event_sequence"]),
            [
                PendingEvent(
                    event_type="run.claimed",
                    stage="lifecycle",
                    payload={
                        "attempt": int(row["attempt"]),
                        "lease_generation": int(row["lease_generation"]),
                    },
                )
            ],
            now,
        )
        return row

    def _fail_unclaimable_run(
        self,
        connection: Connection,
        run_id: str,
        error_code: str,
        now: str,
    ) -> None:
        row = connection.execute(
            text(
                """
                UPDATE runs
                SET status = :failed,
                    current_step = 'failed',
                    error_code = :error_code,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    finished_at = :now,
                    updated_at = :now,
                    last_event_sequence = last_event_sequence + 1
                WHERE id = :run_id
                  AND status IN (:queued, :running)
                RETURNING last_event_sequence
                """
            ),
            {
                "failed": RunStatus.FAILED.value,
                "queued": RunStatus.QUEUED.value,
                "running": RunStatus.RUNNING.value,
                "error_code": error_code,
                "now": now,
                "run_id": run_id,
            },
        ).mappings().one()
        connection.execute(text("DELETE FROM run_credentials WHERE run_id = :run_id"), {"run_id": run_id})
        self._insert_events(
            connection,
            run_id,
            int(row["last_event_sequence"]),
            [
                PendingEvent(
                    event_type="run.failed",
                    stage="lifecycle",
                    payload={"code": error_code},
                )
            ],
            now,
        )

    def renew_lease(self, lease: LeaseToken, lease_for: timedelta) -> LeaseToken:
        now_value = _utc(self.clock())
        now = _timestamp(now_value)
        expires_at = _timestamp(now_value + lease_for)
        with self._transaction() as connection:
            row = connection.execute(
                text(
                    """
                    UPDATE runs
                    SET lease_expires_at = CASE
                            WHEN :expires_at < deadline_at THEN :expires_at
                            ELSE deadline_at
                        END,
                        updated_at = :now
                    WHERE id = :run_id
                      AND status = :running
                      AND lease_owner = :owner_id
                      AND lease_generation = :lease_generation
                      AND lease_expires_at > :now
                      AND deadline_at > :now
                    RETURNING lease_generation, lease_expires_at
                    """
                ),
                {
                    "expires_at": expires_at,
                    "now": now,
                    "run_id": lease.run_id,
                    "running": RunStatus.RUNNING.value,
                    "owner_id": lease.owner_id,
                    "lease_generation": lease.lease_generation,
                },
            ).mappings().one_or_none()
            if row is None:
                self._raise_write_rejected(connection, lease, now)
        return lease.model_copy(update={"expires_at": _datetime(row["lease_expires_at"])})

    def request_cancel(self, run_id: str) -> RunDetail:
        now = _timestamp(self.clock())
        with self._transaction() as connection:
            current = connection.execute(
                text("SELECT status FROM runs WHERE id = :run_id"),
                {"run_id": run_id},
            ).scalar_one_or_none()
            if current is None:
                raise RunNotFound(f"Run {run_id} was not found.")
            status = RunStatus(current)
            if status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                return self._get_run_in_transaction(connection, run_id)
            if status in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_WARNINGS,
                RunStatus.FAILED,
            }:
                raise RunStateError(f"Terminal run {run_id} cannot be cancelled.")

            target = (
                RunStatus.CANCELLED
                if status is RunStatus.QUEUED
                else RunStatus.CANCELLING
            )
            validate_transition(status, target)
            finished_at = now if target is RunStatus.CANCELLED else None
            row = connection.execute(
                text(
                    """
                    UPDATE runs
                    SET status = :target,
                        current_step = :current_step,
                        cancel_requested_at = :now,
                        finished_at = COALESCE(:finished_at, finished_at),
                        lease_owner = CASE WHEN :target = :cancelled THEN NULL ELSE lease_owner END,
                        lease_expires_at = CASE WHEN :target = :cancelled THEN NULL ELSE lease_expires_at END,
                        updated_at = :now,
                        last_event_sequence = last_event_sequence + 1
                    WHERE id = :run_id AND status = :current
                    RETURNING last_event_sequence
                    """
                ),
                {
                    "target": target.value,
                    "current_step": target.value,
                    "now": now,
                    "finished_at": finished_at,
                    "cancelled": RunStatus.CANCELLED.value,
                    "run_id": run_id,
                    "current": status.value,
                },
            ).mappings().one_or_none()
            if row is None:
                raise RunOwnershipLost(f"Run {run_id} changed while cancellation was requested.")
            if target is RunStatus.CANCELLED:
                connection.execute(text("DELETE FROM run_credentials WHERE run_id = :run_id"), {"run_id": run_id})
            self._insert_events(
                connection,
                run_id,
                int(row["last_event_sequence"]),
                [
                    PendingEvent(
                        event_type=f"run.{target.value}",
                        stage="lifecycle",
                        payload={"requested_from": status.value},
                    )
                ],
                now,
            )
        return self.get_run(run_id)

    def complete_cancel(self, lease: LeaseToken) -> RunDetail:
        now = _timestamp(self.clock())
        with self._transaction() as connection:
            validate_transition(RunStatus.CANCELLING, RunStatus.CANCELLED)
            row = connection.execute(
                text(
                    """
                    UPDATE runs
                    SET status = :cancelled,
                        current_step = :cancelled,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        finished_at = :now,
                        updated_at = :now,
                        last_event_sequence = last_event_sequence + 1
                    WHERE id = :run_id
                      AND status = :cancelling
                      AND lease_owner = :owner_id
                      AND lease_generation = :lease_generation
                    RETURNING last_event_sequence
                    """
                ),
                {
                    "cancelled": RunStatus.CANCELLED.value,
                    "cancelling": RunStatus.CANCELLING.value,
                    "now": now,
                    "run_id": lease.run_id,
                    "owner_id": lease.owner_id,
                    "lease_generation": lease.lease_generation,
                },
            ).mappings().one_or_none()
            if row is None:
                self._raise_write_rejected(connection, lease, now)
            connection.execute(text("DELETE FROM run_credentials WHERE run_id = :run_id"), {"run_id": lease.run_id})
            self._insert_events(
                connection,
                lease.run_id,
                int(row["last_event_sequence"]),
                [PendingEvent(event_type="run.cancelled", stage="lifecycle")],
                now,
            )
        return self.get_run(lease.run_id)

    @staticmethod
    def _raise_claim_rejected(connection: Connection, run_id: str) -> None:
        row = connection.execute(
            text("SELECT status FROM runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().one_or_none()
        if row is None:
            raise RunNotFound(f"Run {run_id} was not found.")
        if row["status"] in TERMINAL_STATUSES:
            raise RunStateError(f"Terminal run {run_id} cannot be claimed.")
        raise RunOwnershipLost(f"Run {run_id} already has a current lease.")


__all__ = ["SQLiteRunLeaseRepository"]
