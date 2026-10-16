"""Shared SQLite connection, transaction and fenced-write primitives.

Repository classes inherit this context explicitly. Cross-table writes still
use the caller's connection and belong to one transaction.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from paperseek_app.errors import RunError, RunNotFound, RunOwnershipLost, RunPersistenceError, RunStateError
from paperseek_app.models import LeaseToken, PendingEvent, PersistedCheckpoint, RunDetail, RunStatus, RunVersions, StoredArtifact
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.persistence.migrate import database_url
from paperseek_community.persistence.sqlite_support import (
    TERMINAL_STATUSES, json_dump as _json_dump, json_load as _json_load,
    parse_datetime as _datetime, system_clock as _system_clock, timestamp as _timestamp,
)


class SQLiteRepositoryBase:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        credential_store: EncryptedRunCredentialStore | None = None,
        submission_credentials: dict[str, dict[str, str]] | None = None,
        claim_kind: str | None = "research",
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.clock = clock or _system_clock
        self.credential_store = credential_store or EncryptedRunCredentialStore(self.database_path)
        self.submission_credentials = submission_credentials
        self.claim_kind = claim_kind
        self.engine = self._create_engine()

    def _create_engine(self) -> Engine:
        engine = create_engine(
            database_url(self.database_path),
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            dbapi_connection.create_function("paperseek_casefold", 1, str.casefold, deterministic=True)
            dbapi_connection.create_function("paperseek_strip", 1, str.strip, deterministic=True)
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 10000")
            cursor.close()

        return engine

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        try:
            with self.engine.begin() as connection:
                yield connection
        except RunError:
            raise
        except SQLAlchemyError as exc:
            raise RunPersistenceError("The run transaction could not be committed.") from exc

    @staticmethod
    def _raise_write_rejected(connection: Connection, lease: LeaseToken, now: str) -> None:
        row = connection.execute(
            text(
                """
                SELECT status, lease_owner, lease_generation, lease_expires_at
                FROM runs WHERE id = :run_id
                """
            ),
            {"run_id": lease.run_id},
        ).mappings().one_or_none()
        if row is None:
            raise RunNotFound(f"Run {lease.run_id} was not found.")
        if row["status"] in TERMINAL_STATUSES:
            raise RunStateError(f"Terminal run {lease.run_id} cannot accept durable writes.")
        if (
            row["lease_owner"] != lease.owner_id
            or int(row["lease_generation"]) != lease.lease_generation
            or not row["lease_expires_at"]
            or row["lease_expires_at"] <= now
        ):
            raise RunOwnershipLost(f"Run {lease.run_id} lease ownership was lost.")
        raise RunOwnershipLost(f"Run {lease.run_id} rejected the durable write.")

    @staticmethod
    def _checkpoint(row) -> PersistedCheckpoint:
        return PersistedCheckpoint(
            run_id=row["run_id"],
            step_key=row["step_key"],
            idempotency_key=row["idempotency_key"],
            event_sequence=int(row["event_sequence"]),
            state=_json_load(row["state_json"]),
            attempt=int(row["attempt"]),
            lease_generation=int(row["lease_generation"]),
            workflow_version=row["workflow_version"],
            schema_version=int(row["schema_version"]),
            created_at=_datetime(row["created_at"]),
            state_artifact_id=row["state_artifact_id"] or "",
        )

    @staticmethod
    def _artifact(row) -> StoredArtifact:
        return StoredArtifact(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            role=row["role"],
            visibility=row["visibility"],
            filename=row["filename"],
            media_type=row["media_type"],
            storage_key=row["storage_key"],
            sha256=row["sha256"],
            size=int(row["size"]),
            schema_version=int(row["schema_version"]),
            created_at=_datetime(row["created_at"]),
        )

    def _reserve_sequences(
        self,
        connection: Connection,
        lease: LeaseToken,
        count: int,
        now: str,
    ) -> int:
        row = connection.execute(
            text(
                """
                UPDATE runs
                SET last_event_sequence = last_event_sequence + :count,
                    updated_at = :now
                WHERE id = :run_id
                  AND status = :running
                  AND lease_owner = :owner_id
                  AND lease_generation = :lease_generation
                  AND lease_expires_at > :now
                RETURNING last_event_sequence
                """
            ),
            {
                "count": count,
                "now": now,
                "run_id": lease.run_id,
                "running": RunStatus.RUNNING.value,
                "owner_id": lease.owner_id,
                "lease_generation": lease.lease_generation,
            },
        ).mappings().one_or_none()
        if row is None:
            self._raise_write_rejected(connection, lease, now)
        return int(row["last_event_sequence"])

    @staticmethod
    def _insert_events(
        connection: Connection,
        run_id: str,
        start_sequence: int,
        events: list[PendingEvent],
        occurred_at: str,
    ) -> None:
        for offset, item in enumerate(events):
            connection.execute(
                text(
                    """
                    INSERT INTO run_events (
                        run_id, sequence, event_type, stage,
                        occurred_at, schema_version, payload_json
                    ) VALUES (
                        :run_id, :sequence, :event_type, :stage,
                        :occurred_at, :schema_version, :payload_json
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "sequence": start_sequence + offset,
                    "event_type": item.event_type,
                    "stage": item.stage,
                    "occurred_at": occurred_at,
                    "schema_version": item.schema_version,
                    "payload_json": _json_dump(item.payload),
                },
            )

    @staticmethod
    def _insert_artifacts(
        connection: Connection,
        run_id: str,
        artifacts: list[StoredArtifact],
    ) -> None:
        for artifact in artifacts:
            if artifact.run_id != run_id:
                raise RunPersistenceError("An artifact belongs to a different run.")
            connection.execute(
                text(
                    """
                    INSERT INTO run_artifacts (
                        id, run_id, kind, role, visibility, filename,
                        media_type, storage_key, sha256, size,
                        schema_version, created_at
                    ) VALUES (
                        :id, :run_id, :kind, :role, :visibility, :filename,
                        :media_type, :storage_key, :sha256, :size,
                        :schema_version, :created_at
                    )
                    """
                ),
                {
                    "id": artifact.id,
                    "run_id": artifact.run_id,
                    "kind": artifact.kind,
                    "role": artifact.role.value,
                    "visibility": artifact.visibility.value,
                    "filename": artifact.filename,
                    "media_type": artifact.media_type,
                    "storage_key": artifact.storage_key,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                    "schema_version": artifact.schema_version,
                    "created_at": _timestamp(artifact.created_at),
                },
            )

    def get_run(self, run_id: str) -> RunDetail:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT r.*,
                               (SELECT COUNT(*) FROM run_results rr WHERE rr.run_id = r.id) AS result_count
                        FROM runs r
                        WHERE r.id = :run_id AND r.deleted_at IS NULL
                        """
                    ),
                    {"run_id": run_id},
                ).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RunPersistenceError("The run could not be read.") from exc
        if row is None:
            raise RunNotFound(f"Run {run_id} was not found.")
        return self._run_detail(row)

    def _get_run_in_transaction(self, connection: Connection, run_id: str) -> RunDetail:
        row = connection.execute(
            text(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM run_results rr WHERE rr.run_id = r.id) AS result_count
                FROM runs r WHERE r.id = :run_id
                """
            ),
            {"run_id": run_id},
        ).mappings().one()
        return self._run_detail(row)

    @staticmethod
    def _run_detail(row) -> RunDetail:
        return RunDetail(
            display_title=row["display_title"],
            id=row["id"],
            kind=row["kind"],
            question=row["question"],
            source=row["source"],
            status=RunStatus(row["status"]),
            config_snapshot=_json_load(row["config_json"]),
            current_step=row["current_step"],
            attempt=int(row["attempt"]),
            lease_generation=int(row["lease_generation"]),
            lease_owner=row["lease_owner"] or "",
            lease_expires_at=_datetime(row["lease_expires_at"]),
            last_event_sequence=int(row["last_event_sequence"]),
            error_code=row["error_code"],
            warnings=list(_json_load(row["warning_codes_json"])),
            created_at=_datetime(row["created_at"]),
            deadline_at=_datetime(row["deadline_at"]),
            started_at=_datetime(row["started_at"]),
            cancel_requested_at=_datetime(row["cancel_requested_at"]),
            finished_at=_datetime(row["finished_at"]),
            updated_at=_datetime(row["updated_at"]),
            result_count=int(row["result_count"]),
            versions=RunVersions(
                workflow_version=row["workflow_version"],
                engine_version=row["engine_version"],
                prompt_version=row["prompt_version"],
                config_schema_version=int(row["config_schema_version"]),
                artifact_schema_version=int(row["artifact_schema_version"]),
            ),
        )
