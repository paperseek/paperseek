from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from paperseek_app.execution import WorkflowExecution
from paperseek_app.models import RunStatus
from paperseek_app.services import RunQueryService, RunSubmissionService
from paperseek_app.submission import RunSubmission, prepare_run_submission
from paperseek_app.worker import RunExecutor
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.cli import _parser
from paperseek_community.cli import main as cli_main
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.models import (
    FinalPaperResult,
    QueryLoopBudget,
    ResearchResult,
    ResearchWorkflowRequest,
)
from paperseek_core.ports import CancellationToken

FIXTURE = Path(__file__).parent / "fixtures" / "m1_fixed_source.json"


def _settings(*, llm_api_key: str = "") -> CommunityDeploymentSettings:
    return CommunityDeploymentSettings(
        llm_api_type="openai_chat",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key=llm_api_key,
        openalex_api_key="",
        semantic_scholar_api_key="",
        pubmed_api_key="",
        wos_api_key="",
        google_scholar_api_key="",
        contact_email="",
    )


def _fixed_resolver(run, _cancellation: CancellationToken) -> WorkflowExecution:
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


class M4CliProductEntryContract(unittest.TestCase):
    def test_search_json_includes_all_256_persisted_results(self) -> None:
        result = ResearchResult(
            question="complete CLI output", search_intent="", search_plan={},
            source="openalex", final_query="complete CLI output", db="OPENALEX",
            ranked=[FinalPaperResult(rank=i + 1, uid=f"paper-{i}", title=f"Paper {i}") for i in range(256)],
        )
        result = result.model_copy(update={"artifacts": FinalResultAssembler().artifacts_for(result)})

        async def run(*_args):
            return result

        with tempfile.TemporaryDirectory() as temp, patch.object(WorkflowExecution, "run", run):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    ["search", result.question, "--database", str(Path(temp) / "paperseek.db"), "--output", "json"],
                    deployment_settings=_settings(), execution_resolver=_fixed_resolver,
                )
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["result_count"], 256)
        self.assertEqual([item["uid"] for item in payload["results"]], [f"paper-{i}" for i in range(256)])

    def test_parser_exposes_formal_commands_without_fixture_product_option(self) -> None:
        parser = _parser()
        help_text = parser.format_help()
        self.assertIn("worker", help_text)
        self.assertIn("doctor", help_text)
        with self.assertRaises(SystemExit):
            parser.parse_args(["search", "question", "--source-fixture", "fixture.json"])
        serve = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
        ).choices["serve"]
        self.assertIn("--no-worker", serve.format_help())


    def test_search_without_llm_key_fails_explicitly_without_network_live_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "search",
                        "missing credential",
                        "--database",
                        str(database),
                        "--source",
                        "arxiv",
                        "--output",
                        "json",
                    ],
                    deployment_settings=_settings(),
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "provider_authentication_failed")
        self.assertEqual(payload["results"], [])

    def test_worker_once_claims_a_queued_run_through_the_product_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            migrate_database(database)
            uow = SQLiteRunUnitOfWork(database)
            try:
                service = RunSubmissionService(uow)
                prepared = prepare_run_submission(
                    RunSubmission(
                        question="durable literature search",
                        source="openalex",
                    ),
                    _settings(),
                )
                queued = service.submit_run(
                    subject="community",
                    idempotency_key=str(uuid4()),
                    request_digest=prepared.request_digest,
                    question=prepared.question,
                    source=prepared.source,
                    config_snapshot=prepared.config_snapshot,
                )
            finally:
                uow.close()

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    ["worker", "--database", str(database), "--once"],
                    deployment_settings=_settings(),
                    execution_resolver=_fixed_resolver,
                )
            payload = json.loads(output.getvalue())
            read_uow = SQLiteRunUnitOfWork(database)
            try:
                detail = RunQueryService(
                    read_uow,
                    FileArtifactStore(database.parent),
                ).get_run(queued.id)
            finally:
                read_uow.close()

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["run_id"], queued.id)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(detail.status, RunStatus.COMPLETED)

    def test_worker_without_once_processes_multiple_runs_and_keeps_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            migrate_database(database)
            uow = SQLiteRunUnitOfWork(database)
            try:
                service = RunSubmissionService(uow)
                runs = [service.create_run(f"search {index}", "openalex") for index in range(2)]
                original_claim = RunExecutor.claim
                calls = 0

                async def stop_when_idle(executor):
                    nonlocal calls
                    calls += 1
                    lease = await original_claim(executor)
                    # Keep polling until both runs have finished, then stop like Ctrl+C.
                    if lease is None and all(uow.get_run(run.id).status == RunStatus.COMPLETED for run in runs):
                        raise KeyboardInterrupt
                    return lease

                with patch.object(RunExecutor, "claim", stop_when_idle):
                    exit_code = cli_main(
                        ["worker", "--database", str(database), "--poll-seconds", "0.001"],
                        deployment_settings=_settings(), execution_resolver=_fixed_resolver,
                    )
                self.assertEqual(exit_code, 130)
                self.assertGreaterEqual(calls, 3)
                self.assertEqual([uow.get_run(run.id).status for run in runs], [RunStatus.COMPLETED] * 2)
            finally:
                uow.close()

    def test_serve_no_worker_accepts_but_does_not_claim_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "paperseek.db"
            with patch("uvicorn.run") as run:
                exit_code = cli_main(
                    [
                        "serve",
                        "--database",
                        str(database),
                        "--no-worker",
                    ],
                    deployment_settings=_settings(),
                    execution_resolver=_fixed_resolver,
                )
            app = run.call_args.args[0]
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/runs",
                    headers={"Idempotency-Key": "cli-no-worker"},
                    json={"question": "queued only", "source": "arxiv"},
                )
                self.assertEqual(response.status_code, 202)
                run_id = response.json()["id"]
                asyncio.run(asyncio.sleep(0.1))
                detail = client.get(f"/api/v1/runs/{run_id}").json()

        self.assertEqual(exit_code, 0)
        self.assertEqual(detail["status"], "queued")

    def test_doctor_json_reports_environment_storage_and_config_without_live_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "nested" / "paperseek.db"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "doctor",
                        "--database",
                        str(database),
                        "--source",
                        "arxiv",
                        "--json",
                    ],
                    deployment_settings=_settings(),
                )
            payload = json.loads(output.getvalue())

        ids = {check["id"] for check in payload["checks"]}
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["network"], "not_run")
        self.assertTrue({"environment.python", "storage.directory", "database.access", "llm.api_key"} <= ids)


if __name__ == "__main__":
    unittest.main()
