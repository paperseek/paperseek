from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from paperseek_app.errors import (
    ArtifactConflict,
    ArtifactIntegrityError,
    ArtifactPersistenceError,
    RunPersistenceError,
)
from paperseek_app.execution import CancellationScope, WorkflowExecution
from paperseek_app.models import (
    ArtifactRole,
    ArtifactVisibility,
    ArtifactWrite,
    PendingEvent,
    RunStatus,
    RunVersions,
)
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_app.worker import RunExecutor
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_community.persistence.migrate import database_url
from paperseek_core.errors import LLMRequestError
from paperseek_core.models import (
    CheckpointDraft,
    QueryLoopBudget,
    ResearchWorkflowRequest,
)
from paperseek_core.sources.contracts import SourceError

FIXTURE = Path(__file__).parent / "fixtures" / "m1_fixed_source.json"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def _upgrade_to(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option(
        "script_location",
        str(files("paperseek_community.migrations")),
    )
    config.set_main_option(
        "sqlalchemy.url",
        database_url(database).render_as_string(hide_password=False).replace("%", "%%"),
    )
    command.upgrade(config, revision)


class M3DurableArtifactExecutorContractTest(unittest.TestCase):
    def test_progress_only_execution_skips_snapshots_and_preserves_final_results(self):
        self._create_run()
        baseline = asyncio.run(self._executor().run_once())
        expected = [item.record for item in self.uow.get_results(baseline.id)]
        expected_files = {item.kind: self.query_service.read_user_artifact(baseline.id, item.id).content
                          for item in self.query_service.list_user_artifacts(baseline.id)}
        run = self._create_run()
        service = RunExecutionService(self.uow, self.store, recover_search=False, store_derived_exports=False)
        with ExitStack() as patches:
            for module in ('query_loop', 'retrieval', 'citation', 'candidate_selection',
                           'decision_selection', 'ranking', 'workflow.research'):
                patches.enter_context(patch(f'paperseek_core.{module}.CheckpointDraft',
                    side_effect=AssertionError('Disabled snapshots must not be constructed')))
            patches.enter_context(patch.object(self.uow, 'list_checkpoints',
                side_effect=AssertionError('Progress-only execution must not load recovery state')))
            detail = asyncio.run(self._executor(service).run_once())
        self.assertEqual(detail.status, baseline.status)
        self.assertEqual([item.record for item in self.uow.get_results(run.id)], expected)
        self.assertEqual(self.uow.list_checkpoints(run.id), [])
        self.assertTrue(self.uow.list_artifacts(run.id))
        self.assertEqual({item.kind for item in self.uow.list_artifacts(run.id)}, {'bibliometric_corpus', 'final_result'})
        self.assertTrue(all(item.role == ArtifactRole.COMPLETION_REQUIRED for item in self.uow.list_artifacts(run.id)))
        downloads = self.query_service.list_user_artifacts(run.id)
        self.assertEqual({item.kind for item in downloads}, set(expected_files))
        for item in downloads:
            self.assertEqual(self.query_service.read_user_artifact(run.id, item.id).content, expected_files[item.kind], item.kind)
        self.assertEqual(self.query_service.get_export(run.id, 'json').content, expected_files['export_json'])
        self.assertEqual(self.query_service.get_export(run.id, 'csv').content, expected_files['export_csv'])
        self.assertEqual(self.query_service.get_citation_map(run.id).content, expected_files['citation_map'])
        self.assertEqual(self.uow.list_events(run.id)[-1].event_type, 'run.completed')

    def test_cancel_during_artifact_write_finishes_without_waiting_for_lease_expiry(self):
        run = self._create_run()
        put = self.store.put

        def cancel_on_artifact(draft):
            artifact = put(draft)
            if draft.kind == "final_result":
                self.submission_service.request_cancel(run.id)
            return artifact

        with patch.object(self.store, "put", side_effect=cancel_on_artifact):
            detail = asyncio.run(self._executor().run_once())
        self.assertEqual(detail.status, RunStatus.CANCELLED)
        self.assertEqual(self.uow.get_results(run.id), [])

    def test_terminal_race_during_completion_does_not_crash_worker(self):
        run = self._create_run()
        complete = self.uow.complete_run

        def cancel_before_commit(lease, *args):
            self.submission_service.request_cancel(run.id)
            self.uow.complete_cancel(lease)
            return complete(lease, *args)

        with patch.object(self.uow, "complete_run", side_effect=cancel_before_commit):
            detail = asyncio.run(self._executor().run_once())
        self.assertEqual(detail.status, RunStatus.CANCELLED)
        self.assertEqual(self.uow.get_results(run.id), [])

    def test_model_failure_code_is_persisted_without_private_details_or_results(self):
        run = self._create_run()
        failure = LLMRequestError("private upstream detail", code="provider_quota_exhausted")
        execution = self._execution(None, None)
        execution.llm.chat_json = AsyncMock(side_effect=failure)
        executor = RunExecutor(self.execution_service, lambda *_: execution, owner_id="test-worker")
        detail = asyncio.run(executor.run_once())
        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "provider_quota_exhausted")
        events = self.uow.list_events(run.id)
        self.assertEqual(events[-1].event_type, "run.failed")
        self.assertEqual(events[-1].payload["code"], detail.error_code)
        self.assertNotIn("private upstream", str(events))
        self.assertEqual(self.query_service.get_results(run.id), [])
        self.assertEqual(self.query_service.list_artifacts(run.id), [])

    def test_source_rate_limit_is_persisted_without_private_upstream_data(self):
        run = self._create_run()
        execution = self._execution(None, None)
        execution.source.search = AsyncMock(side_effect=SourceError("openalex", "private upstream detail", status=429, body="api_key=secret"))
        executor = RunExecutor(self.execution_service, lambda *_: execution, owner_id="test-worker")
        detail = asyncio.run(executor.run_once())
        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "source_rate_limited")
        events = self.uow.list_events(run.id)
        source_failure = next(event for event in events if event.event_type == "query.failed")
        self.assertEqual(source_failure.payload["http_status"], 429)
        self.assertEqual(events[-1].payload["code"], "source_rate_limited")
        self.assertNotIn("private upstream", str(events))
        self.assertNotIn("secret", str(events))
        self.assertEqual(self.query_service.get_results(run.id), [])

    def test_execution_can_use_an_ephemeral_observer_without_persistence(self):
        class Observer:
            def __init__(self):
                self.events = []

            def on_event(self, event):
                self.events.append(event)

            def on_checkpoint(self, checkpoint):
                # Transient observers deliberately do not retain checkpoint state.
                return None

        observer = Observer()
        result = asyncio.run(self._execution(None, None).run(observer, CancellationScope()))
        self.assertTrue(result.ranked)
        self.assertEqual(observer.events[-1].event_type, "workflow.completed")
        self.assertEqual(self.query_service.get_history_page().items, [])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.database = self.data_dir / "paperseek.db"
        self.clock = MutableClock()
        migrate_database(self.database)
        self.store = FileArtifactStore(self.data_dir)
        self.uow = SQLiteRunUnitOfWork(self.database, clock=self.clock)
        self.addCleanup(self.uow.close)
        self.submission_service = RunSubmissionService(self.uow)
        self.execution_service = RunExecutionService(self.uow, self.store)
        self.query_service = RunQueryService(self.uow, self.store)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _create_run(self):
        return self.submission_service.create_run(
            self.fixture["question"],
            self.fixture["source"],
            {"fixture_id": self.fixture["fixture_id"]},
        )

    @staticmethod
    def _execution(_run, _cancellation) -> WorkflowExecution:
        source = FixedSourceAdapter.from_file(FIXTURE)
        return WorkflowExecution(
            llm=FixedLLMAdapter(source.fixture.llm_responses),
            source=source,
            request=ResearchWorkflowRequest(
                question=source.fixture.question,
                query_budget=QueryLoopBudget(
                    target_min=1,
                    target_max=50,
                    retrieval_pool_min=1,
                    query_portfolio_enabled=False,
                ),
            ),
        )

    def _executor(
        self,
        service: RunExecutionService | None = None,
    ) -> RunExecutor:
        return RunExecutor(
            service or self.execution_service,
            self._execution,
            owner_id="embedded-worker",
            lease_seconds=60,
        )

    def test_alembic_upgrades_m1_database_without_losing_runs_and_enables_sqlite_guards(self) -> None:
        database = self.data_dir / "upgrade-from-m1.db"
        _upgrade_to(database, "0001_m1_run_vertical")
        existing_id = str(uuid4())
        now = self.clock().isoformat(timespec="microseconds")
        versions = RunVersions()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, question, source, status, config_json, current_step,
                    attempt, lease_generation, last_event_sequence,
                    workflow_version, engine_version, prompt_version,
                    config_schema_version, artifact_schema_version,
                    error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', '', 0, 0, 0, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    existing_id,
                    "persisted before M3",
                    "openalex",
                    RunStatus.QUEUED.value,
                    versions.workflow_version,
                    versions.engine_version,
                    versions.prompt_version,
                    versions.config_schema_version,
                    versions.artifact_schema_version,
                    now,
                    now,
                ),
            )
            connection.commit()

        migrate_database(database)
        upgraded = SQLiteRunUnitOfWork(database, clock=self.clock)
        self.addCleanup(upgraded.close)

        upgraded_run = upgraded.get_run(existing_id)
        self.assertEqual(upgraded_run.question, "persisted before M3")
        self.assertGreater(upgraded_run.deadline_at, upgraded_run.created_at)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 11)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'run%'"
                )
            }
            checkpoint_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(run_checkpoints)")
            }
            result_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(run_results)")
            }
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(runs)")
            }
        self.assertEqual(
            tables,
            {
                "runs",
                "run_events",
                "run_checkpoints",
                "run_results",
                "run_artifacts",
                "run_cleanup_jobs",
                "run_submissions",
                "run_credentials",
            },
        )
        self.assertIn("state_artifact_id", checkpoint_columns)
        self.assertIn("record_artifact_id", result_columns)
        self.assertIn("cancel_requested_at", run_columns)
        self.assertIn("deadline_at", run_columns)
        self.assertIn("deleted_at", run_columns)
        self.assertIn("warning_codes_json", run_columns)
        with upgraded.engine.connect() as connection:
            self.assertEqual(connection.execute(text("PRAGMA foreign_keys")).scalar_one(), 1)
            self.assertEqual(connection.execute(text("PRAGMA journal_mode")).scalar_one(), "wal")
            self.assertEqual(connection.execute(text("PRAGMA busy_timeout")).scalar_one(), 10000)

    def test_file_store_supports_long_checkpoint_paths(self) -> None:
        draft = ArtifactWrite(
            run_id=str(uuid4()), kind="checkpoint", role=ArtifactRole.RECOVERY,
            visibility=ArtifactVisibility.INTERNAL, filename="state-" + "c" * 64 + ".json",
            media_type="application/json", schema_version=1, content=b'{"checkpoint": true}',
        )

        def store_with(padding):
            return FileArtifactStore(self.data_dir / ("deep-" + "a" * 40) / ("nested-" + "b" * padding))

        # Exceed Windows MAX_PATH for the file whatever the temp root's length, without
        # making the directory itself too long to clean up.
        store = store_with(40)
        store = store_with(40 + max(0, 266 - len(str(store._target(draft)))))
        self.assertGreater(len(str(store._target(draft))), 260)
        stored = store.put(draft)
        self.assertEqual(store.put(draft), stored)
        self.assertEqual(store.read(stored), draft.content)
        self.assertFalse(Path(stored.storage_key).is_absolute())
        with self.assertRaises(ArtifactConflict):
            store.put(draft.model_copy(update={"content": b"conflict"}))
        store.delete(stored)
        store.delete(stored)
        with self.assertRaises(ArtifactPersistenceError):
            store.read(stored)

    def test_file_store_is_atomic_immutable_and_integrity_checked(self) -> None:
        run_id = str(uuid4())
        draft = ArtifactWrite(
            run_id=run_id,
            kind="candidate_pool",
            role=ArtifactRole.RECOVERY,
            visibility=ArtifactVisibility.INTERNAL,
            filename="batch-1.jsonl",
            media_type="application/x-ndjson",
            schema_version=1,
            content=b'{"uid":"fixed-001"}\n',
        )

        stored = self.store.put(draft)
        expected = (
            self.data_dir
            / "artifacts"
            / "runs"
            / run_id
            / "candidate_pool"
            / "1"
            / "batch-1.jsonl"
        )
        self.assertEqual(stored, self.store.put(draft))
        self.assertEqual(expected.read_bytes(), draft.content)
        self.assertEqual(self.store.read(stored), draft.content)
        self.assertEqual(stored.size, len(draft.content))
        self.assertEqual(len(stored.sha256), 64)

        with self.assertRaises(ArtifactConflict):
            self.store.put(draft.model_copy(update={"content": b"different"}))

        failed = draft.model_copy(update={"filename": "link-failure.jsonl"})
        with patch(
            "paperseek_community.artifacts.files.os.link",
            side_effect=OSError("synthetic link failure"),
        ):
            with self.assertRaises(ArtifactPersistenceError):
                self.store.put(failed)
        self.assertFalse(expected.with_name("link-failure.jsonl").exists())
        self.assertEqual(list((self.data_dir / "artifacts" / ".tmp").glob("*")), [])

        expected.write_bytes(b"tampered")
        with self.assertRaises(ArtifactIntegrityError):
            self.store.read(stored)
        expected.write_bytes(draft.content)
        self.store.delete(stored)
        self.store.delete(stored)
        self.assertFalse(expected.exists())

        with self.assertRaises(ArtifactPersistenceError):
            self.store.put(draft.model_copy(update={"filename": "../escape.jsonl"}))
        self.assertFalse((self.data_dir / "escape.jsonl").exists())

    def test_checkpoint_pointer_commits_after_recovery_artifact_and_rolls_back_together(self) -> None:
        run = self._create_run()
        lease = self.uow.claim_run(run.id, "checkpoint-worker", timedelta(seconds=60))
        first = CheckpointDraft(
            step_key="retrieve.lane",
            idempotency_key="m3-checkpoint-first",
            state={"completed_lanes": ["relevance"], "candidate_count": 1},
            workflow_version=run.versions.workflow_version,
        )
        persisted = self.execution_service.append_checkpoint(
            lease,
            [PendingEvent(event_type="retrieval.progress", stage="retrieval")],
            first,
        )

        self.assertTrue(persisted.state_artifact_id)
        self.assertEqual(self.uow.get_checkpoint(run.id).state, {})
        self.assertEqual(self.execution_service.get_checkpoint(run.id).state, first.state)
        artifacts = self.query_service.list_artifacts(run.id)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].role, ArtifactRole.RECOVERY)
        self.assertEqual(artifacts[0].visibility, ArtifactVisibility.INTERNAL)
        self.assertEqual(json.loads(self.store.read(artifacts[0])), first.state)

        before_events = self.query_service.list_events(run.id)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_m3_checkpoint
                BEFORE INSERT ON run_checkpoints
                BEGIN
                    SELECT RAISE(ABORT, 'forced checkpoint failure');
                END
                """
            )
        second = CheckpointDraft(
            step_key="rank_results.batch",
            idempotency_key="m3-checkpoint-second",
            state={"completed_batch_ids": ["batch-1"]},
            workflow_version=run.versions.workflow_version,
        )
        with self.assertRaises(RunPersistenceError):
            self.execution_service.append_checkpoint(
                lease,
                [PendingEvent(event_type="ranking.progress", stage="ranking")],
                second,
            )

        self.assertEqual(
            self.execution_service.get_checkpoint(run.id).idempotency_key,
            first.idempotency_key,
        )
        self.assertEqual(self.query_service.list_events(run.id), before_events)
        self.assertEqual(len(self.query_service.list_artifacts(run.id)), 1)
        checkpoint_files = list(
            (self.data_dir / "artifacts" / "runs" / run.id / "checkpoint" / "1").glob("*.json")
        )
        self.assertEqual(len(checkpoint_files), 2)

    def test_new_executor_instance_claims_preexisting_queued_run_and_commits_required_artifacts(self) -> None:
        queued = self._create_run()
        self.uow.close()

        restarted_uow = SQLiteRunUnitOfWork(self.database, clock=self.clock)
        self.addCleanup(restarted_uow.close)
        restarted_store = FileArtifactStore(self.data_dir)
        restarted_execution = RunExecutionService(restarted_uow, restarted_store)
        restarted_query = RunQueryService(restarted_uow, restarted_store)
        completed = asyncio.run(self._executor(restarted_execution).run_once())

        self.assertIsNotNone(completed)
        self.assertEqual(completed.id, queued.id)
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(completed.attempt, 1)
        self.assertEqual(completed.lease_generation, 1)
        required = [
            item
            for item in restarted_query.list_artifacts(queued.id)
            if item.role is ArtifactRole.COMPLETION_REQUIRED
        ]
        self.assertEqual(
            {item.filename for item in required},
            {
                "final_result.json",
                "export.json",
                "export.csv",
                "export.bib",
                "export.ris",
                "candidate_pool.jsonl",
                "citation_map.json",
                "bibliometric_corpus.json",
            },
        )
        final_artifact = next(item for item in required if item.filename == "final_result.json")
        results = restarted_query.get_results(queued.id)
        self.assertEqual({item.record_artifact_id for item in results}, {final_artifact.id})
        self.assertEqual(asyncio.run(self._executor(restarted_execution).run_once()), None)

    def test_worker_runs_keep_the_execution_enrichment_labels(self) -> None:
        class Labels:
            async def annotate(self, documents):
                return {document.uid: {"journal_metrics": {"ccf": {"rank": "A", "rank_number": 1}}} for document in documents}

        queued = self._create_run()
        executor = RunExecutor(self.execution_service,
                               lambda run, cancellation: replace(self._execution(run, cancellation), enrichment=Labels()),
                               owner_id="embedded-worker", lease_seconds=60)
        self.assertEqual(asyncio.run(executor.run_once()).status, RunStatus.COMPLETED)
        results = self.query_service.get_results(queued.id)
        self.assertTrue(results)
        self.assertTrue(all(item.record.journal_metrics["ccf"]["rank"] == "A" for item in results))

    def test_completion_artifact_reference_failure_rolls_back_success_and_marks_failed(self) -> None:
        run = self._create_run()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_completion_artifact_reference
                BEFORE INSERT ON run_artifacts
                WHEN NEW.role = 'completion_required'
                BEGIN
                    SELECT RAISE(ABORT, 'forced completion artifact failure');
                END
                """
            )

        detail = asyncio.run(self._executor().run_once())

        self.assertEqual(detail.id, run.id)
        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "artifact_commit_failed")
        self.assertEqual(self.query_service.get_results(run.id), [])
        self.assertFalse(
            any(
                item.role is ArtifactRole.COMPLETION_REQUIRED
                for item in self.query_service.list_artifacts(run.id)
            )
        )
        event_types = [item.event_type for item in self.query_service.list_events(run.id)]
        self.assertNotIn("run.completed", event_types)
        self.assertEqual(event_types[-1], "run.failed")

    def test_conflicting_required_file_never_reports_success(self) -> None:
        run = self._create_run()
        self.store.put(
            ArtifactWrite(
                run_id=run.id,
                kind="final_result",
                role=ArtifactRole.COMPLETION_REQUIRED,
                visibility=ArtifactVisibility.USER,
                filename="final_result.json",
                media_type="application/json",
                schema_version=1,
                content=b'{"not":"the result"}\n',
            )
        )

        detail = asyncio.run(self._executor().run_once())

        self.assertEqual(detail.status, RunStatus.FAILED)
        self.assertEqual(detail.error_code, "artifact_persistence_failed")
        self.assertEqual(self.query_service.get_results(run.id), [])
        self.assertFalse(
            any(
                event.event_type.startswith("run.completed")
                for event in self.query_service.list_events(run.id)
            )
        )


if __name__ == "__main__":
    unittest.main()
