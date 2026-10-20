from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paperseek_app.errors import RunOwnershipLost, RunPersistenceError, RunStateError
from paperseek_app.models import (
    ArtifactRole,
    ArtifactVisibility,
    ArtifactWrite,
    PendingEvent,
    RunStatus,
)
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_app.state import validate_transition
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.fixed_source import FixedLLMAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_core.models import (
    CheckpointDraft,
    QueryLoopBudget,
    ResearchResult,
    ResearchWorkflowRequest,
)
from paperseek_core.sources.contracts import SourceCapabilities

FIXTURE = Path(__file__).parent / "fixtures" / "m1_fixed_source.json"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FailingSource:
    name = "openalex"
    capabilities = SourceCapabilities(source=name, lanes=("relevance",))

    async def search(self, _request):
        raise RuntimeError("private upstream response body")


class M1VerticalSliceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "paperseek.db"
        self.clock = MutableClock()
        migrate_database(self.database)
        self.uow = SQLiteRunUnitOfWork(self.database, clock=self.clock)
        self.addCleanup(self.uow.close)
        self.store = FileArtifactStore(self.temp.name)
        self.submission_service = RunSubmissionService(self.uow)
        self.execution_service = RunExecutionService(self.uow, self.store)
        self.query_service = RunQueryService(self.uow, self.store)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _create_and_claim(self, owner: str = "worker-a"):
        run = self.submission_service.create_run(
            self.fixture["question"],
            self.fixture["source"],
            {"fixture_id": self.fixture["fixture_id"]},
        )
        lease = self.uow.claim_run(run.id, owner, timedelta(seconds=30))
        return run, lease



    def test_event_and_checkpoint_transaction_rolls_back_together(self):
        run, lease = self._create_and_claim()
        before = self.query_service.list_events(run.id)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_m1_checkpoint
                BEFORE INSERT ON run_checkpoints
                BEGIN
                    SELECT RAISE(ABORT, 'forced checkpoint failure');
                END
                """
            )

        with self.assertRaises(RunPersistenceError):
            self.execution_service.append_checkpoint(
                lease,
                [PendingEvent(event_type="source.transient", stage="source", payload={})],
                CheckpointDraft(
                    step_key="source.search",
                    idempotency_key="forced-failure",
                    state={"count": 1},
                    workflow_version=self.query_service.get_run(run.id).versions.workflow_version,
                ),
            )

        after = self.query_service.list_events(run.id)
        self.assertEqual([event.sequence for event in after], [event.sequence for event in before])
        self.assertIsNone(self.uow.get_checkpoint(run.id))
        self.assertEqual(self.query_service.get_run(run.id).last_event_sequence, before[-1].sequence)

    def test_concurrent_observer_writes_allocate_one_continuous_sequence(self):
        run, lease = self._create_and_claim()

        def append(index: int) -> None:
            self.uow.append_events_and_checkpoint(
                lease,
                [PendingEvent(event_type="source.progress", stage="source", payload={"index": index})],
                None,
                None,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(append, range(12)))

        events = self.query_service.list_events(run.id)
        self.assertEqual([event.sequence for event in events], list(range(1, 15)))
        self.assertEqual(
            sorted(event.payload["index"] for event in events if event.event_type == "source.progress"),
            list(range(12)),
        )

    def test_source_failure_is_terminal_redacted_and_cannot_be_completed(self):
        run, lease = self._create_and_claim()
        detail = asyncio.run(
            self.execution_service.execute_claimed_workflow(
                lease,
                llm=FixedLLMAdapter(self.fixture["llm_responses"][:2]),
                source=FailingSource(),
                request=ResearchWorkflowRequest(
                    question=self.fixture["question"],
                    query_budget=QueryLoopBudget(
                        target_min=1,
                        target_max=50,
                        retrieval_pool_min=1,
                        query_portfolio_enabled=False,
                    ),
                ),
            )
        )

        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "query_primary_source_failed")
        self.assertEqual(self.query_service.get_results(run.id), [])
        self.assertEqual(self.uow.get_checkpoint(run.id).step_key, "query.plan")
        self.assertNotIn(b"private upstream response body", self.database.read_bytes())
        with self.assertRaises((RunOwnershipLost, RunStateError)):
            self.uow.complete_run(lease, [], None, [])

    def test_state_machine_rejects_terminal_and_illegal_transitions(self):
        validate_transition(RunStatus.QUEUED, RunStatus.RUNNING)
        validate_transition(RunStatus.RUNNING, RunStatus.COMPLETED)
        validate_transition(RunStatus.RUNNING, RunStatus.FAILED)
        with self.assertRaises(RunStateError):
            validate_transition(RunStatus.QUEUED, RunStatus.COMPLETED)
        with self.assertRaises(RunStateError):
            validate_transition(RunStatus.COMPLETED, RunStatus.RUNNING)

    def test_visible_partial_failure_commits_completed_with_warnings(self):
        run, lease = self._create_and_claim()
        result = ResearchResult(
            question=self.fixture["question"],
            search_intent="fixture",
            search_plan={},
            source=self.fixture["source"],
            final_query=self.fixture["question"],
            db=self.fixture["source"].upper(),
            warnings=["abstract_enrichment_failed:10.5555/fixture"],
        )

        final_artifact = self.store.put(
            ArtifactWrite(
                run_id=run.id,
                kind="final_result",
                role=ArtifactRole.COMPLETION_REQUIRED,
                visibility=ArtifactVisibility.USER,
                filename="final_result.json",
                media_type="application/json",
                schema_version=1,
                content=b"{}\n",
            )
        )
        detail = self.uow.complete_run(lease, [], result, [final_artifact])

        self.assertEqual(detail.status, RunStatus.COMPLETED_WITH_WARNINGS)
        terminal = self.query_service.list_events(run.id)[-1]
        self.assertEqual(terminal.event_type, "run.completed_with_warnings")
        self.assertEqual(terminal.payload["warning_count"], 1)

    def test_migration_is_idempotent(self):
        with closing(sqlite3.connect(self.database)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        migrate_database(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
            self.assertEqual(
                connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), schema
            )


if __name__ == "__main__":
    unittest.main()
