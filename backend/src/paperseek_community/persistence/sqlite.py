"""Community unit of work assembled over one explicit SQLite transaction context."""
from paperseek_community.persistence.sqlite_artifact_cleanup import SQLiteArtifactCleanupRepository
from paperseek_community.persistence.sqlite_event_checkpoint import SQLiteEventCheckpointRepository
from paperseek_community.persistence.sqlite_result_history import SQLiteResultHistoryRepository
from paperseek_community.persistence.sqlite_run_lease import SQLiteRunLeaseRepository
from paperseek_community.persistence.sqlite_run_retry import SQLiteRunRetryRepository


class SQLiteRunUnitOfWork(
    SQLiteRunLeaseRepository,
    SQLiteEventCheckpointRepository,
    SQLiteResultHistoryRepository,
    SQLiteArtifactCleanupRepository,
    SQLiteRunRetryRepository,
):
    pass
