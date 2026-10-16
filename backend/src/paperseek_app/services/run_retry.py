"""Retry a single research pipeline step by requeueing its run."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta

from paperseek_app.errors import ArtifactError, RunStateError
from paperseek_app.models import RunDetail
from paperseek_app.ports import ArtifactStore, RunUnitOfWork
from paperseek_app.run_steps import invalidated_checkpoints
from paperseek_app.state import RETRYABLE_STATUSES


class RunRetryService:
    def __init__(
        self,
        uow: RunUnitOfWork,
        artifact_store: ArtifactStore,
        *,
        save_credentials: Callable[[str, dict[str, dict[str, str]]], None] | None = None,
        resolve_credentials: Callable[[RunDetail], dict[str, dict[str, str]]] | None = None,
        deadline_seconds: int = 3600,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("A run deadline must be positive.")
        self.uow = uow
        self.artifact_store = artifact_store
        self.save_credentials = save_credentials
        self.resolve_credentials = resolve_credentials
        self.deadline_seconds = deadline_seconds

    def retry_from_step(
        self,
        run_id: str,
        step: str,
        *,
        credentials: Mapping[str, dict[str, str]] | None = None,
    ) -> RunDetail:
        """Requeue ``run_id`` so ``step`` and everything after it run again."""
        patterns = invalidated_checkpoints(step)
        # ``get_run`` raises RunNotFound; the state is validated again, atomically,
        # inside the requeue transaction.
        current = self.uow.get_run(run_id)
        if current.status not in RETRYABLE_STATUSES:
            raise RunStateError(f"Run {run_id} is {current.status.value} and cannot be retried.")
        self._restore_credentials(run_id, current, credentials)
        detail, orphans = self.uow.retry_from_step(
            run_id,
            checkpoint_patterns=patterns,
            step=step,
            deadline_for=timedelta(seconds=self.deadline_seconds),
        )
        # Rows are already gone, so a failed file removal only leaves an orphan
        # that the artifact cleanup job can collect later.
        for artifact in orphans:
            try:
                self.artifact_store.delete(artifact)
            except ArtifactError:
                continue
        return detail

    def _restore_credentials(
        self,
        run_id: str,
        run: RunDetail,
        credentials: Mapping[str, dict[str, str]] | None,
    ) -> None:
        """A finished run drops its per-run credentials; put the keys back.

        Keys that still resolve (a completed run keeps its own) are left alone,
        so only the kinds a finished run lost are written again.
        """
        if self.save_credentials is None:
            return
        resolved = {
            str(kind): {str(name): str(value) for name, value in values.items()}
            for kind, values in (credentials or {}).items()
            if values.get("api_key")
        }
        if self.resolve_credentials is not None:
            for kind, values in self.resolve_credentials(run).items():
                resolved.setdefault(kind, values)
        if resolved:
            self.save_credentials(run_id, resolved)


__all__ = ["RunRetryService"]
