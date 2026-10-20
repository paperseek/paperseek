from __future__ import annotations

from contextlib import closing, redirect_stdout
from fastapi.testclient import TestClient
import io
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from paperseek_community.api import create_app
from paperseek_community.cli import main as cli_main
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_app import build_info
from paperseek_app.execution import WorkflowExecution
from paperseek_core.models import QueryLoopBudget, ResearchWorkflowRequest


FIXTURE = Path(__file__).parents[1] / "behavior" / "fixtures" / "m1_fixed_source.json"


def _settings() -> CommunityDeploymentSettings:
    return CommunityDeploymentSettings(
        llm_api_type="openai_chat",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key="",
        openalex_api_key="",
        semantic_scholar_api_key="",
        pubmed_api_key="",
        wos_api_key="",
        google_scholar_api_key="",
        contact_email="",
    )


def _fixed_resolver(run, _cancellation) -> WorkflowExecution:
    source = FixedSourceAdapter.from_file(FIXTURE)
    return WorkflowExecution(
        llm=FixedLLMAdapter(source.fixture.llm_responses),
        source=source,
        request=ResearchWorkflowRequest(
            question=run.question,
            query_budget=QueryLoopBudget(
                target_min=1,
                target_max=50,
                retrieval_pool_min=1,
                query_portfolio_enabled=False,
            ),
        ),
    )


class M1EntryIntegrationTest(unittest.TestCase):
    def test_cli_migrate_applies_the_current_alembic_head_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            outputs = []
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli_main(["migrate", "--database", str(database)])
                outputs.append(json.loads(output.getvalue()))
            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                artifact_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='run_artifacts'"
                ).fetchone()
                cleanup_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='run_cleanup_jobs'"
                ).fetchone()

        self.assertEqual(exit_code, 0)
        self.assertEqual(outputs, [{"schema_version": version, "status": "migrated"}] * 2)
        self.assertGreater(version, 0)
        self.assertEqual(artifact_table, ("run_artifacts",))
        self.assertEqual(cleanup_table, ("run_cleanup_jobs",))

    def test_cli_creates_fixed_run_and_rest_and_cli_read_the_same_result(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "search",
                        fixture["question"],
                        "--database",
                        str(database),
                        "--source",
                        "openalex",
                    ],
                    deployment_settings=_settings(),
                    execution_resolver=_fixed_resolver,
                )
            cli_payload = json.loads(output.getvalue())
            with TestClient(create_app(database)) as client:
                detail = client.get(f"/api/v1/runs/{cli_payload['run_id']}")
                events = client.get(f"/api/v1/runs/{cli_payload['run_id']}/events")
                results = client.get(f"/api/v1/runs/{cli_payload['run_id']}/results")

        self.assertEqual(exit_code, 0)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(events.status_code, 200)
        self.assertEqual(results.status_code, 200)
        self.assertEqual(detail.json()["status"], fixture["expected"]["status"])
        self.assertEqual(cli_payload["status"], detail.json()["status"])
        self.assertEqual(cli_payload["results"], results.json()["results"])
        event_payloads = [
            json.loads(line[6:])
            for line in events.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["sequence"] for event in event_payloads],
            list(range(1, len(event_payloads) + 1)),
        )


    def test_public_schema_hash_matches_the_served_api(self):
        with tempfile.TemporaryDirectory() as temp:
            schema = create_app(Path(temp) / "paperseek.db").openapi()
        canonical = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(
            build_info.PUBLIC_OPENAPI_SHA256,
            hashlib.sha256(canonical).hexdigest().upper(),
        )


if __name__ == "__main__":
    unittest.main()
