from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from paperseek_app.errors import (
    ArtifactNotFound,
    ArtifactPersistenceError,
    InvalidCursor,
    RunNotFound,
    RunStateError,
)
from paperseek_app.models import (
    ArtifactRole,
    ArtifactVisibility,
    ArtifactWrite,
    PendingEvent,
)
from paperseek_app.services import (
    RunCleanupService,
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_community.api import create_app
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_community.persistence.migrate import database_url
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.models import (
    CheckpointDraft,
    FinalPaperResult,
    ResearchAssemblyRequest,
    ResearchResult,
    SearchPlan,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float = 1) -> None:
        self.value += timedelta(seconds=seconds)


def _upgrade_to(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(files("paperseek_community.migrations")))
    config.set_main_option(
        "sqlalchemy.url",
        database_url(database).render_as_string(hide_password=False).replace("%", "%%"),
    )
    command.upgrade(config, revision)


class M3SseReadHistoryExportCleanupContractTest(unittest.TestCase):
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
        self.cleanup_service = RunCleanupService(self.uow, self.store)

    def _stored(
        self,
        run_id: str,
        *,
        kind: str,
        filename: str,
        content: bytes,
        media_type: str = "application/json",
    ):
        return self.store.put(
            ArtifactWrite(
                run_id=run_id,
                kind=kind,
                role=ArtifactRole.COMPLETION_REQUIRED,
                visibility=ArtifactVisibility.USER,
                filename=filename,
                media_type=media_type,
                schema_version=1,
                content=content,
            )
        )

    def _complete(self, *, result_count: int = 1, warnings: list[str] | None = None):
        run = self.submission_service.create_run(
            f"Question {self.clock.value.isoformat()}", "openalex"
        )
        lease = self.uow.claim_run(run.id, "contract-worker", timedelta(seconds=60))
        self.execution_service.append_checkpoint(
            lease,
            [PendingEvent(event_type="workflow.progress", stage="workflow", payload={"current": 1, "total": 1, "unit": "stage"})],
            CheckpointDraft(
                step_key="assemble_final_result",
                idempotency_key="final-before-complete",
                state={"final": True, "step_workflow_version": "m2.research-workflow.v2"},
                workflow_version=run.versions.workflow_version,
                schema_version=1,
            ),
        )
        warning_codes = list(warnings or [])
        records = [
            FinalPaperResult(
                rank=rank,
                title=f"Paper {rank}",
                uid=f"paper-{rank}",
                score=float(100 - rank),
                source="Journal",
                provider="openalex",
            )
            for rank in range(1, result_count + 1)
        ]
        export_json = {
            "question": run.question,
            "ranked": [record.model_dump(mode="json") for record in records],
        }
        if warning_codes:
            export_json["warnings"] = warning_codes
        artifacts = [
            self._stored(
                run.id,
                kind="final_result",
                filename="final_result.json",
                content=(json.dumps({"ranked": export_json["ranked"]}) + "\n").encode(),
            ),
            self._stored(
                run.id,
                kind="export_json",
                filename="export.json",
                content=(json.dumps(export_json, sort_keys=True) + "\n").encode(),
            ),
            self._stored(
                run.id,
                kind="export_csv",
                filename="export.csv",
                content=b"rank,title\n1,Paper 1\n",
                media_type="text/csv",
            ),
            self._stored(
                run.id,
                kind="citation_map",
                filename="citation_map.json",
                content=b'{"edges":[]}\n',
            ),
        ]
        result = ResearchResult(
            question=run.question,
            search_intent="contract",
            search_plan={"intent": "contract"},
            source="openalex",
            final_query="contract",
            db="OPENALEX",
            ranked=records,
            warnings=warning_codes,
        )
        completed = self.uow.complete_run(lease, [], result, artifacts)
        self.clock.advance()
        return completed

    def test_schema_five_preserves_schema_four_runs_and_adds_cleanup_lifecycle(self) -> None:
        database = self.data_dir / "upgrade-from-v4.db"
        _upgrade_to(database, "0004_m3_recovery")
        existing_id = "00000000-0000-4000-8000-000000000005"
        now = self.clock().isoformat(timespec="microseconds")
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, question, source, status, config_json, current_step,
                    attempt, lease_generation, last_event_sequence,
                    workflow_version, engine_version, prompt_version,
                    config_schema_version, artifact_schema_version, error_code,
                    created_at, deadline_at, updated_at
                ) VALUES (?, 'Preserve this run', 'openalex', 'queued', '{}', '',
                          0, 0, 0, 'm2.research-workflow.v1', '1.0.0b5',
                          'm2.frozen-search.v1', 1, 1, '', ?, ?, ?)
                """,
                (existing_id, now, (self.clock() + timedelta(hours=1)).isoformat(timespec="microseconds"), now),
            )
            connection.commit()

        _upgrade_to(database, "0005_m3_read_cleanup")
        with closing(sqlite3.connect(database)) as connection:
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            cleanup_columns = {row[1] for row in connection.execute("PRAGMA table_info(run_cleanup_jobs)")}
            preserved = connection.execute(
                "SELECT question, deleted_at, warning_codes_json FROM runs WHERE id = ?",
                (existing_id,),
            ).fetchone()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual(user_version, 5)
        self.assertTrue({"deleted_at", "warning_codes_json"}.issubset(run_columns))
        self.assertTrue(
            {"run_id", "status", "attempt", "lease_owner", "lease_expires_at", "error_code"}.issubset(cleanup_columns)
        )
        self.assertEqual(preserved, ("Preserve this run", None, "[]"))

    def test_sse_replays_only_missing_strict_sequences_with_public_event_fields(self) -> None:
        run = self._complete()
        events = self.query_service.list_events(run.id)
        cursor = events[2].sequence

        with TestClient(create_app(self.database)) as client:
            response = client.get(
                f"/api/v1/runs/{run.id}/events",
                headers={"Last-Event-ID": str(cursor)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        frames = [frame for frame in response.text.split("\n\n") if frame.strip()]
        replayed = []
        for frame in frames:
            lines = frame.splitlines()
            self.assertEqual(lines[0].split(": ", 1)[0], "id")
            payload = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
            replayed.append(payload)
            self.assertEqual(
                set(payload),
                {"run_id", "sequence", "event_type", "stage", "occurred_at", "schema_version", "payload"},
            )
            self.assertNotIn("storage_key", json.dumps(payload))
        self.assertEqual([item["sequence"] for item in replayed], [item.sequence for item in events if item.sequence > cursor])
        self.assertTrue(all(item["run_id"] == run.id for item in replayed))

    def test_result_cursor_pages_are_stable_and_cursor_scope_is_enforced(self) -> None:
        run = self._complete(result_count=5)

        first = self.query_service.get_result_page(run.id, page_size=2)
        second = self.query_service.get_result_page(run.id, cursor=first.next_cursor, page_size=2)
        third = self.query_service.get_result_page(run.id, cursor=second.next_cursor, page_size=2)

        self.assertEqual([item.rank for item in first.results], [1, 2])
        self.assertEqual([item.rank for item in second.results], [3, 4])
        self.assertEqual([item.rank for item in third.results], [5])
        self.assertIsNone(third.next_cursor)
        other = self._complete()
        with self.assertRaises(InvalidCursor):
            self.query_service.get_result_page(other.id, cursor=first.next_cursor, page_size=2)

    def test_history_includes_active_runs_and_deleted_runs_hide_immediately(self) -> None:
        oldest = self._complete()
        middle = self._complete(warnings=["citation_expansion_failed_base_candidates_retained"])
        newest = self._complete()
        queued = self.submission_service.create_run("Active history", "openalex")

        first = self.query_service.get_history_page(page_size=2)
        second = self.query_service.get_history_page(cursor=first.next_cursor, page_size=2)
        self.assertEqual([item.id for item in first.items], [queued.id, newest.id])
        self.assertEqual([item.id for item in second.items], [middle.id, oldest.id])
        self.assertIsNone(second.next_cursor)
        self.assertEqual(
            second.items[0].warnings,
            ["citation_expansion_failed_base_candidates_retained"],
        )

        receipt = self.cleanup_service.delete_history_run(middle.id)
        self.assertEqual(receipt.status, "cleanup_pending")
        with self.assertRaises(RunNotFound):
            self.query_service.get_run(middle.id)
        self.assertNotIn(
            middle.id,
            [item.id for item in self.query_service.get_history_page(page_size=10).items],
        )

    def test_active_history_survives_a_fresh_client_and_remains_searchable(self) -> None:
        run = self.submission_service.create_run("Recover after refresh", "openalex")
        for status in ("queued", "running", "cancelling"):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("UPDATE runs SET status=? WHERE id=?", (status, run.id))
                    connection.commit()
                with TestClient(create_app(self.database)) as client:
                    for query in ({}, {"query": "recover"}):
                        page = client.get("/api/v1/history", params=query).json()
                        self.assertEqual([(item["id"], item["status"]) for item in page["items"]], [(run.id, status)])
                    self.assertEqual(client.delete(f"/api/v1/history/{run.id}").status_code, 409)

    def test_user_artifacts_export_and_citation_map_never_expose_internal_objects_or_paths(self) -> None:
        run = self._complete()
        internal = next(
            item
            for item in self.query_service.list_artifacts(run.id)
            if item.visibility is ArtifactVisibility.INTERNAL
        )

        descriptors = self.query_service.list_user_artifacts(run.id)
        self.assertEqual(
            {item.filename for item in descriptors},
            {"final_result.json", "export.json", "export.csv", "citation_map.json"},
        )
        serialized = json.dumps([item.model_dump(mode="json") for item in descriptors])
        self.assertNotIn("storage_key", serialized)
        self.assertNotIn(str(self.data_dir), serialized)
        self.assertNotIn(internal.id, {item.id for item in descriptors})
        with self.assertRaises(ArtifactNotFound):
            self.query_service.read_user_artifact(run.id, internal.id)

        json_export = self.query_service.get_export(run.id, "json")
        csv_export = self.query_service.get_export(run.id, "csv")
        citation_map = self.query_service.get_citation_map(run.id)
        self.assertEqual(json.loads(json_export.content)["ranked"][0]["title"], "Paper 1")
        self.assertEqual(csv_export.content, b"rank,title\n1,Paper 1\n")
        self.assertEqual(json.loads(citation_map.content), {"edges": []})

    def test_delete_rejects_active_run_then_cleans_content_and_every_artifact(self) -> None:
        active = self.submission_service.create_run("Still queued", "openalex")
        with self.assertRaises(RunStateError):
            self.cleanup_service.delete_history_run(active.id)

        completed = self._complete(result_count=2)
        run_directory = self.data_dir / "artifacts" / "runs" / completed.id
        self.assertTrue(run_directory.exists())
        receipt = self.cleanup_service.delete_history_run(completed.id)
        self.assertEqual(receipt.run_id, completed.id)
        self.assertTrue(
            self.cleanup_service.cleanup_next_deleted_run("cleanup-worker", lease_seconds=30)
        )
        self.assertFalse(run_directory.exists())
        with closing(sqlite3.connect(self.database)) as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (completed.id,)).fetchone()[0]
                for table in ("run_events", "run_checkpoints", "run_results", "run_artifacts", "run_cleanup_jobs")
            }
            run_count = connection.execute("SELECT COUNT(*) FROM runs WHERE id = ?", (completed.id,)).fetchone()[0]
        self.assertEqual(run_count, 0)
        self.assertEqual(set(counts.values()), {0})

    def test_cleanup_failure_stays_durable_and_retryable_without_restoring_visibility(self) -> None:
        completed = self._complete()
        self.cleanup_service.delete_history_run(completed.id)
        with patch.object(self.store, "delete", side_effect=ArtifactPersistenceError("controlled")):
            self.assertTrue(
                self.cleanup_service.cleanup_next_deleted_run("cleanup-worker", lease_seconds=30)
            )

        with self.assertRaises(RunNotFound):
            self.query_service.get_run(completed.id)
        with closing(sqlite3.connect(self.database)) as connection:
            failed = connection.execute(
                "SELECT status, attempt, error_code FROM run_cleanup_jobs WHERE run_id = ?",
                (completed.id,),
            ).fetchone()
        self.assertEqual(failed, ("failed", 1, "artifact_delete_failed"))
        self.assertTrue(
            self.cleanup_service.cleanup_next_deleted_run("cleanup-worker", lease_seconds=30)
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs WHERE id = ?", (completed.id,)).fetchone()[0], 0)

    def test_public_read_delete_endpoints_use_durable_models_and_uniform_errors(self) -> None:
        completed = self._complete()
        active = self.submission_service.create_run("Active", "openalex")
        internal = next(
            item
            for item in self.query_service.list_artifacts(completed.id)
            if item.visibility is ArtifactVisibility.INTERNAL
        )

        with TestClient(create_app(self.database)) as client:
            history = client.get("/api/v1/history", params={"page_size": 1})
            results = client.get(f"/api/v1/runs/{completed.id}/results", params={"page_size": 1})
            artifacts = client.get(f"/api/v1/runs/{completed.id}/artifacts")
            export = client.get(f"/api/v1/runs/{completed.id}/export", params={"format": "json"})
            citation = client.get(f"/api/v1/runs/{completed.id}/citation-map")
            descriptor = next(item for item in artifacts.json()["artifacts"] if item["filename"] == "export.csv")
            download = client.get(descriptor["download_url"])
            hidden = client.get(f"/api/v1/runs/{completed.id}/artifacts/{internal.id}")
            bad_cursor = client.get(f"/api/v1/runs/{completed.id}/results", params={"cursor": "bad!"})
            bad_page = client.get("/api/v1/history", params={"page_size": 201})
            active_delete = client.delete(f"/api/v1/history/{active.id}")
            deleted = client.delete(f"/api/v1/history/{completed.id}")
            missing = client.get(f"/api/v1/runs/{completed.id}")

        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["id"], active.id)
        self.assertEqual(results.json()["results"][0]["title"], "Paper 1")
        self.assertEqual(export.json()["ranked"][0]["title"], "Paper 1")
        self.assertEqual(citation.json(), {"edges": []})
        self.assertEqual(download.content, b"rank,title\n1,Paper 1\n")
        self.assertNotIn("storage_key", json.dumps(artifacts.json()))
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json()["error"]["code"], "artifact_not_found")
        self.assertEqual(bad_cursor.status_code, 400)
        self.assertEqual(bad_cursor.json()["error"]["code"], "invalid_cursor")
        self.assertEqual(bad_page.status_code, 422)
        self.assertEqual(bad_page.json()["error"]["code"], "invalid_request")
        self.assertEqual(active_delete.status_code, 409)
        self.assertEqual(active_delete.json()["error"]["code"], "run_not_terminal")
        self.assertEqual(deleted.status_code, 202)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "run_not_found")

    def test_warning_codes_are_in_history_and_json_export_but_clean_export_stays_unchanged(self) -> None:
        clean = FinalResultAssembler().assemble(
            ResearchAssemblyRequest(
                question="Clean",
                source="openalex",
                search_plan=SearchPlan(intent="clean"),
                final_query="clean",
            )
        )
        warned = FinalResultAssembler().assemble(
            ResearchAssemblyRequest(
                question="Warned",
                source="openalex",
                search_plan=SearchPlan(intent="warned"),
                final_query="warned",
                warnings=["citation_expansion_failed_base_candidates_retained"],
            )
        )
        clean_export = json.loads(next(item.content for item in clean.artifacts if item.filename == "export.json"))
        warned_export = json.loads(next(item.content for item in warned.artifacts if item.filename == "export.json"))
        self.assertNotIn("warnings", clean_export)
        self.assertEqual(
            warned_export["warnings"],
            ["citation_expansion_failed_base_candidates_retained"],
        )


if __name__ == "__main__":
    unittest.main()
