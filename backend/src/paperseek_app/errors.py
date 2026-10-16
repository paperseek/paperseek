class RunError(RuntimeError):
    pass


class RunNotFound(RunError):
    pass


class RunOwnershipLost(RunError):
    pass


class RunPersistenceError(RunError):
    pass


class RunStateError(RunError):
    pass


class IdempotencyConflict(RunStateError):
    pass


class ActiveRunExists(RunStateError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} is already active.")
        self.run_id = run_id


class InvalidCursor(RunError):
    pass


class InvalidResultQuery(RunError):
    pass


class IncompatibleCheckpoint(RunError):
    pass


class UnknownRunStep(RunError):
    pass


class ArtifactError(RunError):
    pass


class ArtifactConflict(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


class ArtifactPersistenceError(ArtifactError):
    pass


class ArtifactNotFound(ArtifactError):
    pass
