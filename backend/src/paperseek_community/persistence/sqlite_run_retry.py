"""Requeue a finished run so a single pipeline step is executed again.

A retry keeps every checkpoint that is still valid upstream of the chosen step
and drops the checkpoints of that step and of everything that consumes its
output. The run is put back in the queue; the embedded worker then resumes the
workflow from the surviving checkpoints, so only the requested step (and the
steps that follow it) run again.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from paperseek_app.errors import (
    RunNotFound,
    RunPersistenceError,
    RunStateError,
)
from paperseek_app.models import (
    ArtifactRole,
    PendingEvent,
    RunDetail,
    RunStatus,
    StoredArtifact,
)
from paperseek_app.state import RETRYABLE_STATUSES, validate_transition
from paperseek_community.persistence.sqlite_base import SQLiteRepositoryBase
from paperseek_community.persistence.sqlite_support import (
    timestamp as _timestamp,
    utc as _utc,
)

# A manual retry grants the run a fresh budget slice, so a run whose original
# deadline already elapsed is not rejected the moment it is claimed again.
RETRY_DEADLINE_SECONDS = 3600


class SQLiteRunRetryRepository(SQLiteRepositoryBase):
    def retry_from_step(
        self,
        run_id: str,
        *,
        checkpoint_patterns: tuple[str, ...],
        step: str,
        deadline_for: timedelta = timedelta(seconds=RETRY_DEADLINE_SECONDS),
    ) -> tuple[RunDetail, list[StoredArtifact]]:
        if deadline_for <= timedelta(0):
            raise ValueError("A run deadline must be positive.")
        patterns = tuple(dict.fromkeys(pattern for pattern in checkpoint_patterns if pattern))
        if not patterns:
            raise RunPersistenceError("A step retry requires at least one checkpoint pattern.")
        # Same matching rule as the resume composer: exact key or key prefix.
        match = " OR ".join(
            f"(step_key = :key{index} OR step_key LIKE :like{index})"
            for index in range(len(patterns))
        )
        match_parameters: dict[str, str] = {}
        for index, pattern in enumerate(patterns):
            match_parameters[f"key{index}"] = pattern
            match_parameters[f"like{index}"] = f"{pattern}%"

        now_value = _utc(self.clock())
        now = _timestamp(now_value)
        deadline_at = _timestamp(now_value + deadline_for)
        with self._transaction() as connection:
            row = connection.execute(
                text("SELECT status FROM runs WHERE id = :run_id AND deleted_at IS NULL"),
                {"run_id": run_id},
            ).mappings().one_or_none()
            if row is None:
                raise RunNotFound(f"Run {run_id} was not found.")
            status = RunStatus(row["status"])
            if status not in RETRYABLE_STATUSES:
                raise RunStateError(f"Run {run_id} is {status.value} and cannot be retried.")
            validate_transition(status, RunStatus.QUEUED)

            dropped = connection.execute(
                text(
                    f"""
                    SELECT step_key, state_artifact_id
                    FROM run_checkpoints
                    WHERE run_id = :run_id AND ({match})
                    """
                ),
                {"run_id": run_id, **match_parameters},
            ).mappings().all()
            completions = connection.execute(
                text(
                    """
                    SELECT id FROM run_artifacts
                    WHERE run_id = :run_id AND role = :role
                    """
                ),
                {"run_id": run_id, "role": ArtifactRole.COMPLETION_REQUIRED.value},
            ).mappings().all()
            artifact_ids = [
                *(item["state_artifact_id"] for item in dropped if item["state_artifact_id"]),
                *(item["id"] for item in completions),
            ]
            orphans: list[StoredArtifact] = []
            if artifact_ids:
                placeholders = ", ".join(f":artifact{index}" for index in range(len(artifact_ids)))
                artifact_parameters = {
                    "run_id": run_id,
                    **{f"artifact{index}": value for index, value in enumerate(artifact_ids)},
                }
                orphans = [
                    self._artifact(item)
                    for item in connection.execute(
                        text(
                            f"""
                            SELECT * FROM run_artifacts
                            WHERE run_id = :run_id AND id IN ({placeholders})
                            """
                        ),
                        artifact_parameters,
                    ).mappings().all()
                ]

            connection.execute(
                text("DELETE FROM run_results WHERE run_id = :run_id"), {"run_id": run_id}
            )
            if dropped:
                connection.execute(
                    text(
                        f"""
                        DELETE FROM run_checkpoints
                        WHERE run_id = :run_id AND ({match})
                        """
                    ),
                    {"run_id": run_id, **match_parameters},
                )
            if artifact_ids:
                connection.execute(
                    text(
                        f"""
                        DELETE FROM run_artifacts
                        WHERE run_id = :run_id
                          AND id IN ({", ".join(f":artifact{index}" for index in range(len(artifact_ids)))})
                        """
                    ),
                    {
                        "run_id": run_id,
                        **{
                            f"artifact{index}": value
                            for index, value in enumerate(artifact_ids)
                        },
                    },
                )

            updated = connection.execute(
                text(
                    """
                    UPDATE runs
                    SET status = :queued,
                        current_step = :queued,
                        error_code = '',
                        warning_codes_json = '[]',
                        finished_at = NULL,
                        cancel_requested_at = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        deadline_at = :deadline_at,
                        updated_at = :now,
                        last_event_sequence = last_event_sequence + 1
                    WHERE id = :run_id AND status = :current
                    RETURNING last_event_sequence
                    """
                ),
                {
                    "queued": RunStatus.QUEUED.value,
                    "current": status.value,
                    "deadline_at": deadline_at,
                    "now": now,
                    "run_id": run_id,
                },
            ).mappings().one_or_none()
            if updated is None:
                raise RunStateError(f"Run {run_id} changed while its retry was requested.")
            self._insert_events(
                connection,
                run_id,
                int(updated["last_event_sequence"]),
                [
                    PendingEvent(
                        event_type="run.retry_requested",
                        stage="lifecycle",
                        payload={"code": step, "from_status": status.value},
                    )
                ],
                now,
            )
            return self._get_run_in_transaction(connection, run_id), orphans


__all__ = ["RETRY_DEADLINE_SECONDS", "SQLiteRunRetryRepository"]
