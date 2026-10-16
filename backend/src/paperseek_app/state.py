from paperseek_app.errors import RunStateError
from paperseek_app.models import RunStatus


ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_WARNINGS,
            RunStatus.FAILED,
        }
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED}),
    # A finished run stays terminal for the worker; only an explicit per-step
    # retry may requeue it, and only from the states listed in RETRYABLE_STATUSES.
    RunStatus.COMPLETED: frozenset({RunStatus.QUEUED}),
    RunStatus.COMPLETED_WITH_WARNINGS: frozenset({RunStatus.QUEUED}),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED}),
    RunStatus.CANCELLED: frozenset({RunStatus.QUEUED}),
}


# Runs whose steps can be retried: they are finished, so no worker is using them.
RETRYABLE_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_WARNINGS,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise RunStateError(f"Run cannot transition from {current.value} to {target.value}.")
