from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from importlib.resources import files
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from paperseek_app.errors import ActiveRunExists, IdempotencyConflict
from paperseek_app.execution import WorkflowExecution
from paperseek_app.services import RunQueryService, RunSubmissionService
from paperseek_community.api import create_app
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.execution import NEVER_CANCELLED
from paperseek_community.persistence.migrate import database_url
from paperseek_core.models import QueryLoopBudget, ResearchWorkflowRequest


FIXTURE = Path(__file__).parent / "fixtures" / "m1_fixed_source.json"


def _upgrade_to(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(files("paperseek_community.migrations")))
    config.set_main_option(
        "sqlalchemy.url",
        database_url(database).render_as_string(hide_password=False).replace("%", "%%"),
    )
    command.upgrade(config, revision)


def _execution(run, _cancellation) -> WorkflowExecution:
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


class M4SubmissionProductEntryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.database = self.data_dir / "paperseek.db"
        migrate_database(self.database)
        self.uow = SQLiteRunUnitOfWork(self.database)
        self.addCleanup(self.uow.close)
        store = FileArtifactStore(self.data_dir)
        self.submission_service = RunSubmissionService(self.uow)
        self.query_service = RunQueryService(self.uow, store)

    def _await_terminal(self, client: TestClient, run_id: str) -> dict:
        for _ in range(400):
            detail = client.get(f"/api/v1/runs/{run_id}").json()
            if detail["status"] in {"completed", "completed_with_warnings", "failed", "cancelled"}:
                return detail
            time.sleep(0.01)
        self.fail(f"Run {run_id} never reached a terminal state.")

    def test_step_retry_endpoint_requeues_and_revalidates_a_finished_run(self) -> None:
        question = json.loads(FIXTURE.read_text(encoding="utf-8"))["question"]
        payload = {"question": question, "source": "openalex", "search_budget": "fast"}
        with TestClient(create_app(self.database, execution_resolver=_execution)) as client:
            accepted = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "step-retry"},
                json=payload,
            )
            self.assertEqual(accepted.status_code, 202)
            run_id = accepted.json()["id"]
            self.assertEqual(self._await_terminal(client, run_id)["status"], "completed")

            retried = client.post(f"/api/v1/runs/{run_id}/steps/ranking/retry", json={})
            self.assertEqual(retried.status_code, 202)
            self.assertEqual(retried.json()["status"], "queued")
            self.assertEqual(retried.json()["error_code"], "")
            # The fixture resolver replays the same scripted answers, so the rerun
            # ranking may degrade; the retry must still reach a non-failed terminal state.
            self.assertIn(
                self._await_terminal(client, run_id)["status"],
                {"completed", "completed_with_warnings"},
            )

            unknown = client.post(f"/api/v1/runs/{run_id}/steps/teleport/retry", json={})
            self.assertEqual(unknown.status_code, 422)
            self.assertEqual(unknown.json()["error"]["code"], "unknown_run_step")
            missing = client.post("/api/v1/runs/missing/steps/query/retry", json={})
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json()["error"]["code"], "run_not_found")

        with TestClient(
            create_app(self.database, accept_submissions_without_worker=True)
        ) as client:
            pending = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "step-retry-live"},
                json=payload,
            )
            self.assertEqual(pending.status_code, 202)
            self.assertEqual(pending.json()["status"], "queued")
            live = client.post(
                f"/api/v1/runs/{pending.json()['id']}/steps/query/retry", json={}
            )
            self.assertEqual(live.status_code, 409)
            self.assertEqual(live.json()["error"]["code"], "run_not_retryable")

    def test_current_schema_preserves_v5_runs_and_adds_atomic_submission_records(self) -> None:
        database = self.data_dir / "upgrade-from-v5.db"
        _upgrade_to(database, "0005_m3_read_cleanup")
        with closing(sqlite3.connect(database)) as connection:
            before = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        self.assertEqual(migrate_database(database), 11)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before)
            self.assertIn(
                "owner_subject",
                {row[1] for row in connection.execute("PRAGMA table_info(runs)")},
            )
            self.assertEqual(
                {row[1] for row in connection.execute("PRAGMA table_info(run_submissions)")},
                {"subject", "idempotency_key", "request_digest", "run_id", "created_at"},
            )

    def test_submission_is_atomic_idempotent_and_enforces_one_public_active_run(self) -> None:
        first = self.submission_service.submit_run(
            subject="community",
            idempotency_key="submit-1",
            request_digest="digest-1",
            question="How should durable research runs recover?",
            source="openalex",
            config_snapshot={"search_budget": "fast"},
        )
        repeated = self.submission_service.submit_run(
            subject="community",
            idempotency_key="submit-1",
            request_digest="digest-1",
            question="How should durable research runs recover?",
            source="openalex",
            config_snapshot={"search_budget": "fast"},
        )
        self.assertEqual(repeated.id, first.id)

        with self.assertRaises(IdempotencyConflict):
            self.submission_service.submit_run(
                subject="community",
                idempotency_key="submit-1",
                request_digest="digest-2",
                question="Different request",
                source="arxiv",
            )
        with self.assertRaises(ActiveRunExists) as raised:
            self.submission_service.submit_run(
                subject="community",
                idempotency_key="submit-2",
                request_digest="digest-2",
                question="Another request",
                source="arxiv",
            )
        self.assertEqual(raised.exception.run_id, first.id)

    def test_browser_credentials_are_encrypted_outside_run_state_and_deleted(self) -> None:
        run = self.submission_service.create_run("Credential boundary", "openalex")
        store = EncryptedRunCredentialStore(self.database)
        secret = "m4-contract-secret-that-must-not-be-plain"
        store.save(run.id, {"llm": {"api_key": secret}, "source": {"api_key": "source-secret"}})

        self.assertEqual(store.resolve(run.id)["llm"]["api_key"], secret)
        self.assertNotIn(secret, self.database.read_bytes().decode("latin-1"))
        self.assertNotIn("api_key", json.dumps(self.query_service.get_run(run.id).config_snapshot))

        store.delete(run.id)
        self.assertEqual(store.resolve(run.id), {})

    def test_run_and_credentials_become_visible_together_and_retries_do_not_overwrite(self) -> None:
        store = EncryptedRunCredentialStore(self.database)
        entered, release = Event(), Event()
        original_save = store.save

        def delayed_save(run_id, credentials, **kwargs):
            original_save(run_id, credentials, **kwargs)
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("The test did not release credential persistence.")

        payload = {"question": "Atomic credentials", "credentials": {"llm": {"api_key": "first-secret"}}}
        responses = []
        with TestClient(create_app(
            self.database, accept_submissions_without_worker=True, credential_store=store,
        )) as client:
            with patch.object(store, "save", side_effect=delayed_save):
                thread = Thread(target=lambda: responses.append(client.post(
                    "/api/v1/runs", json=payload, headers={"Idempotency-Key": "atomic"},
                )))
                thread.start()
                try:
                    self.assertTrue(entered.wait(timeout=3))
                    with closing(sqlite3.connect(self.database)) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM run_credentials").fetchone()[0], 0)
                    self.assertIsNone(self.uow.claim_next_run("worker", timedelta(seconds=60)))
                finally:
                    release.set()
                    thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(responses[0].status_code, 202)
            run_id = responses[0].json()["id"]
            lease = self.uow.claim_next_run("worker", timedelta(seconds=60))
            self.assertEqual(lease.run_id, run_id)
            self.assertEqual(store.resolve(run_id), {"llm": {"api_key": "first-secret"}})
            payload["credentials"]["llm"]["api_key"] = "replacement-secret"
            retry = client.post("/api/v1/runs", json=payload, headers={"Idempotency-Key": "atomic"})
            self.assertEqual(retry.status_code, 202)
            self.assertEqual(retry.json()["id"], run_id)
            self.assertEqual(store.resolve(run_id), {"llm": {"api_key": "first-secret"}})
            self.uow.request_cancel(run_id)
            self.uow.complete_cancel(lease)
            terminal_retry = client.post("/api/v1/runs", json=payload, headers={"Idempotency-Key": "atomic"})
            self.assertEqual(terminal_retry.status_code, 202)
            self.assertEqual(store.resolve(run_id), {})

    def test_credential_failure_rolls_back_submission_and_allows_retry(self) -> None:
        store = EncryptedRunCredentialStore(self.database)
        original_save = store.save

        def fail_after_write(run_id, credentials, **kwargs):
            original_save(run_id, credentials, **kwargs)
            raise OSError("fixture storage failure")

        payload = {"question": "Rollback credentials", "credentials": {"llm": {"api_key": "secret"}}}
        with TestClient(create_app(
            self.database, accept_submissions_without_worker=True, credential_store=store,
        )) as client:
            with patch.object(store, "save", side_effect=fail_after_write):
                failed = client.post("/api/v1/runs", json=payload, headers={"Idempotency-Key": "retry"})
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(failed.json()["error"]["code"], "credential_persistence_failed")
            with closing(sqlite3.connect(self.database)) as connection:
                for table in ("runs", "run_submissions", "run_events", "run_credentials"):
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            retried = client.post("/api/v1/runs", json=payload, headers={"Idempotency-Key": "retry"})
            self.assertEqual(retried.status_code, 202)
            run_id = retried.json()["id"]
            client.post(f"/api/v1/runs/{run_id}/cancel")
            self.assertEqual(store.resolve(run_id), {})

    def test_community_rejects_hosted_service_selection_without_creating_a_run(self) -> None:
        with TestClient(create_app(self.database, accept_submissions_without_worker=True)) as client:
            for path in ("/api/v1/runs", "/api/v1/diagnostics"):
                payload = {"service": {"mode": "byok"}}
                if path.endswith("/runs"):
                    payload["question"] = "Hosted is not local Community"
                response = client.post(path, json=payload, headers={"Idempotency-Key": "hosted-choice"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "unsupported_service_mode")
            self.assertEqual(client.get("/api/v1/history").json()["items"], [])

    def test_api_submission_executes_and_vue_spa_is_the_single_root(self) -> None:
        static_dir = self.data_dir / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text(
            '<!doctype html><html><body><div id="app">Vue product entry</div></body></html>',
            encoding="utf-8",
        )
        with TestClient(
            create_app(
                self.database,
                execution_resolver=_execution,
                static_dir=static_dir,
            )
        ) as client:
            sources = client.get("/api/v1/sources")
            self.assertEqual(sources.status_code, 200)
            self.assertEqual(
                [item["id"] for item in sources.json()["sources"]],
                [
                    "openalex",
                    "arxiv",
                    "crossref",
                    "semanticscholar",
                    "pubmed",
                    "wos",
                    "googlescholar",
                    "paperhub",
                ],
            )

            response = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "browser-submit"},
                json={
                    "question": json.loads(FIXTURE.read_text(encoding="utf-8"))["question"],
                    "source": "openalex",
                    "search_budget": "fast",
                    "citation_enabled": True,
                },
            )
            self.assertEqual(response.status_code, 202)
            run_id = response.json()["id"]
            for _ in range(200):
                detail = client.get(f"/api/v1/runs/{run_id}").json()
                if detail["status"] in {"completed", "completed_with_warnings", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(detail["status"], "completed")
            self.assertEqual(client.get(f"/api/v1/runs/{run_id}/results").status_code, 200)
            audit_response = client.get(f"/api/v1/runs/{run_id}/audit")
            self.assertEqual(audit_response.status_code, 200)
            audit = audit_response.json()
            self.assertEqual(audit["query_portfolio"], [])
            self.assertEqual(audit["loop_decisions"][0]["action"], "accept")
            self.assertEqual(audit["candidate_lifecycle"]["ledger_candidates"], 1)
            self.assertEqual(audit["candidate_lifecycle"]["selected_for_llm"], 1)
            self.assertEqual(audit["candidate_lifecycle"]["final_output"], 1)
            self.assertEqual(audit["candidate_lifecycle"]["ledger_retained"], 1)
            self.assertEqual(audit["recovery"]["total"], 0)
            self.assertEqual(len(audit["candidate_queue"]["keys"]), 1)
            self.assertNotIn("api_key", json.dumps(audit).lower())
            logs = client.get(f"/api/v1/runs/{run_id}/logs")
            self.assertEqual(logs.status_code, 200)
            self.assertTrue(logs.headers["content-type"].startswith("text/plain"))
            self.assertIn("attachment;", logs.headers["content-disposition"])
            self.assertIn("000001", logs.text)
            self.assertIn("workflow.completed", logs.text)
            self.assertNotIn("api_key", logs.text.lower())
            audit_schema = client.get("/openapi.json").json()["paths"][
                "/api/v1/runs/{run_id}/audit"
            ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertEqual(audit_schema, {"$ref": "#/components/schemas/RunAudit"})
            schema = client.get("/openapi.json").json()
            expected_responses = {
                ("/api/v1/sources", "get"): "SourceListOutput",
                ("/api/v1/runs", "post"): "RunDetail",
                ("/api/v1/runs/{run_id}", "get"): "RunDetail",
                ("/api/v1/runs/{run_id}/results", "get"): "ResultPageOutput",
                ("/api/v1/runs/{run_id}/citation-map", "get"): "CitationMapOutput",
                ("/api/v1/history", "get"): "HistoryPage",
                ("/api/v1/history/{run_id}", "delete"): "DeletionReceipt",
                ("/api/v1/runs/{run_id}/cancel", "post"): "RunDetail",
            }
            for (path, method), model in expected_responses.items():
                response_schema = schema["paths"][path][method]["responses"][
                    "200" if method == "get" else "202"
                ]["content"]["application/json"]["schema"]
                self.assertEqual(response_schema, {"$ref": f"#/components/schemas/{model}"})
            self.assertIn("Vue product entry", client.get("/").text)
            for route in (
                f"/runs/{run_id}",
                f"/runs/{run_id}/results",
                f"/runs/{run_id}/citation-map",
                "/history",
                "/settings",
                "/settings/models",
                "/settings/sources",
                "/settings/general",
                "/settings/user",
            ):
                deep_link = client.get(route)
                self.assertEqual(deep_link.status_code, 200, route)
                self.assertTrue(deep_link.headers["content-type"].startswith("text/html"), route)
                self.assertIn("Vue product entry", deep_link.text, route)

            for route in (
                "/health",
                "/ready",
                "/api/v1/sources",
                "/api/v1/not-a-route",
                "/results",
                "/citation-map",
                "/settings/unknown",
                "/settings/models/unknown",
                f"/api/v1/runs/{run_id}/artifacts",
                f"/api/v1/runs/{run_id}/audit",
                f"/api/v1/runs/{run_id}/logs",
                f"/api/v1/runs/{run_id}/artifacts/not-an-artifact",
                f"/api/v1/runs/{run_id}/export?format=json",
            ):
                response = client.get(route)
                self.assertNotIn("Vue product entry", response.text, route)
                self.assertFalse(response.headers["content-type"].startswith("text/html"), route)

    def test_source_capabilities_drive_filters_and_citation_submission(self) -> None:
        with TestClient(
            create_app(
                self.database,
                execution_resolver=_execution,
            )
        ) as client:
            response = client.get("/api/v1/sources")
            self.assertEqual(response.status_code, 200)
            sources = {item["id"]: item for item in response.json()["sources"]}

            self.assertEqual(
                list(sources),
                [
                    "openalex",
                    "arxiv",
                    "crossref",
                    "semanticscholar",
                    "pubmed",
                    "wos",
                    "googlescholar",
                    "paperhub",
                ],
            )
            self.assertTrue(sources["openalex"]["default"])
            self.assertEqual(sources["wos"]["api_key"], "required")

            expected_credentials = {
                "openalex": (
                    "recommended",
                    "OpenAlex API Key",
                    "Get OpenAlex API key",
                    "https://openalex.org/settings/api",
                ),
                "semanticscholar": (
                    "optional",
                    "Semantic Scholar API Key",
                    "Request Semantic Scholar API key",
                    "https://www.semanticscholar.org/product/api#api-key-form",
                ),
                "pubmed": (
                    "optional",
                    "PubMed API Key",
                    "Get NCBI API key",
                    "https://www.ncbi.nlm.nih.gov/books/NBK25497/",
                ),
                "wos": (
                    "required",
                    "Web of Science Starter API Key",
                    "Register Web of Science Starter application",
                    "https://developer.clarivate.com/apis/wos-starter",
                ),
                "googlescholar": (
                    "required",
                    "Serper API Key",
                    "Get Serper API key",
                    "https://serper.dev/",
                ),
            }
            for source_id, expected in expected_credentials.items():
                source = sources[source_id]
                self.assertEqual(source["api_key"], expected[0])
                self.assertEqual(
                    source["credential"],
                    {
                        "input_label": expected[1],
                        "guidance_label": expected[2],
                        "guidance_url": expected[3],
                    },
                )
            for source_id in ("arxiv", "crossref", "paperhub"):
                self.assertEqual(sources[source_id]["api_key"], "not_required")
                self.assertIsNone(sources[source_id]["credential"])

            openalex = sources["openalex"]["capabilities"]
            self.assertTrue(openalex["citation_expansion"])
            self.assertEqual(openalex["filter"]["mode"], "native")
            self.assertEqual(len(openalex["filter"]["options"]), 26)
            self.assertEqual(openalex["filter"]["options"][6]["id"], "17")
            self.assertEqual(openalex["filter"]["options"][6]["label"], "Computer Science")

            self.assertEqual(sources["arxiv"]["capabilities"]["filter"]["mode"], "native")
            self.assertEqual(
                sources["wos"]["capabilities"]["filter"]["label"],
                "Web of Science Category",
            )
            self.assertFalse(sources["wos"]["capabilities"]["citation_expansion"])
            for source_id in ("googlescholar", "paperhub"):
                source_filter = sources[source_id]["capabilities"]["filter"]
                self.assertEqual(source_filter["mode"], "text")
                self.assertEqual(source_filter["options"], [])

            accepted = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "source-capability-submit"},
                json={
                    "question": json.loads(FIXTURE.read_text(encoding="utf-8"))["question"],
                    "source": "openalex",
                    "search_budget": "fast",
                    "field_ids": ["17", "22"],
                    "field_hint": "",
                    "citation_enabled": True,
                },
            )
            self.assertEqual(accepted.status_code, 202)
            detail = client.get(f"/api/v1/runs/{accepted.json()['id']}").json()
            self.assertEqual(detail["config_snapshot"]["field_ids"], ["17", "22"])
            self.assertEqual(detail["config_snapshot"]["field_hint"], "")

            invalid = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "source-capability-invalid"},
                json={
                    "question": "Invalid cross-source capability payload",
                    "source": "paperhub",
                    "field_ids": ["17"],
                    "field_hint": "computer systems",
                    "citation_enabled": True,
                },
            )
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(invalid.json()["error"]["code"], "unsupported_source_capability")

    def test_source_credential_never_enters_run_history_events_or_urls(self) -> None:
        secret = "source-guidance-contract-secret"
        with TestClient(
            create_app(
                self.database,
                execution_resolver=_execution,
            )
        ) as client:
            accepted = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "source-credential-redaction"},
                json={
                    "question": json.loads(FIXTURE.read_text(encoding="utf-8"))["question"],
                    "source": "openalex",
                    "search_budget": "fast",
                    "credentials": {"llm": {"api_key": secret}},
                },
            )
            self.assertEqual(accepted.status_code, 202)
            run_id = accepted.json()["id"]
            self.assertNotIn(secret, str(accepted.request.url))

            run = client.get(f"/api/v1/runs/{run_id}")
            history = client.get("/api/v1/history")
            events = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "0"})
            for response in (accepted, run, history, events):
                self.assertNotIn(secret, response.text)

    def test_embedding_and_reranker_submission_is_structured_and_credentials_are_transient(self) -> None:
        embedding_secret = "embedding-contract-secret"
        reranker_secret = "reranker-contract-secret"
        with TestClient(
            create_app(
                self.database,
                execution_resolver=_execution,
            )
        ) as client:
            schema = client.get("/openapi.json").json()
            submission_schema = schema["components"]["schemas"]["RunSubmission"]
            self.assertIn("retrieval", submission_schema["properties"])

            accepted = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "retrieval-config-submit"},
                json={
                    "question": json.loads(FIXTURE.read_text(encoding="utf-8"))["question"],
                    "source": "openalex",
                    "search_budget": "fast",
                    "retrieval": {
                        "embedding": {
                            "provider": "openai",
                            "model": "text-embedding-3-large",
                            "base_url": "https://api.openai.com/v1",
                        },
                        "reranker": {
                            "provider": "nvidia",
                            "model": "nv-rerank-qa-mistral-4b:1",
                            "base_url": "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
                        },
                    },
                    "credentials": {
                        "embedding": {"api_key": embedding_secret},
                        "reranker": {"api_key": reranker_secret},
                    },
                },
            )
            self.assertEqual(accepted.status_code, 202)
            run_id = accepted.json()["id"]
            detail = client.get(f"/api/v1/runs/{run_id}")
            self.assertEqual(
                detail.json()["config_snapshot"]["retrieval"],
                {
                    "mode": "embedding_rerank",
                    "embedding": {
                        "provider": "openai",
                        "model": "text-embedding-3-large",
                        "base_url": "https://api.openai.com/v1",
                    },
                    "reranker": {
                        "provider": "nvidia",
                        "model": "nv-rerank-qa-mistral-4b:1",
                        "base_url": "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
                    },
                },
            )
            for response in (accepted, detail, client.get("/api/v1/history")):
                self.assertNotIn(embedding_secret, response.text)
                self.assertNotIn(reranker_secret, response.text)

            invalid = client.post(
                "/api/v1/runs",
                headers={"Idempotency-Key": "retrieval-config-invalid"},
                json={
                    "question": "Unsupported retrieval provider",
                    "source": "openalex",
                    "retrieval": {"embedding": {"provider": "unknown"}},
                },
            )
            self.assertEqual(invalid.status_code, 422)

    def test_diagnostics_reuses_submission_configuration_without_network_or_persistence(self) -> None:
        secret = "diagnostics-contract-secret"
        with TestClient(create_app(self.database, execution_resolver=_execution)) as client:
            schema = client.get("/openapi.json").json()
            operation = schema["paths"]["/api/v1/diagnostics"]["post"]
            request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
            self.assertEqual(
                request_schema["$ref"],
                "#/components/schemas/DiagnosticSubmission",
            )

            warning = client.post(
                "/api/v1/diagnostics",
                json={
                    "source": "openalex",
                    "search_budget": "fast",
                    "llm": {
                        "provider": "ollama",
                        "api_type": "openai_chat",
                        "model": "qwen3:8b",
                        "base_url": "http://diagnostics.example/v1",
                    },
                    "retrieval": {
                        "embedding": {"provider": "local"},
                        "reranker": {"provider": ""},
                    },
                    "credentials": {"source": {"api_key": secret}},
                },
            )
            self.assertEqual(warning.status_code, 200)
            self.assertEqual(warning.json()["status"], "warning")
            self.assertTrue(warning.json()["ok"])
            self.assertNotIn(secret, warning.text)
            self.assertEqual(client.get("/api/v1/history").json()["items"], [])

            passed = client.post(
                "/api/v1/diagnostics",
                json={
                    "source": "arxiv",
                    "llm": {
                        "provider": "ollama",
                        "api_type": "openai_chat",
                        "model": "qwen3:8b",
                        "base_url": "http://localhost:11434/v1?token=must-not-echo",
                    },
                },
            )
            self.assertEqual(passed.status_code, 200)
            self.assertEqual(passed.json()["status"], "pass")
            self.assertTrue(passed.json()["ok"])
            self.assertNotIn("must-not-echo", passed.text)

            failed = client.post(
                "/api/v1/diagnostics",
                json={
                    "source": "wos",
                    "llm": {
                        "provider": "openai",
                        "api_type": "unsupported",
                        "base_url": "file:///tmp/model",
                    },
                    "retrieval": {
                        "embedding": {"provider": "openai"},
                        "reranker": {"provider": "nvidia"},
                    },
                },
            )
            self.assertEqual(failed.status_code, 200)
            payload = failed.json()
            self.assertEqual(payload["status"], "fail")
            self.assertFalse(payload["ok"])
            checks = {check["id"]: check for check in payload["checks"]}
            self.assertEqual(checks["source.credentials"]["status"], "fail")
            self.assertEqual(checks["llm.api_type"]["status"], "fail")
            self.assertEqual(checks["llm.api_key"]["status"], "fail")
            self.assertEqual(checks["llm.base_url"]["status"], "fail")
            self.assertEqual(checks["retrieval.embedding"]["status"], "fail")
            self.assertEqual(checks["retrieval.reranker"]["status"], "fail")

    def test_custom_loopback_route_is_shared_by_diagnostics_and_composition(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "body": json.loads(self.rfile.read(length)),
                })
                payload = json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        secret = "custom-loopback-secret"
        settings = CommunityDeploymentSettings(
            llm_api_type="openai_chat",
            llm_base_url="https://example.invalid/v1",
            llm_model="deployment-model",
            llm_api_key="",
            openalex_api_key="",
            semantic_scholar_api_key="",
            pubmed_api_key="",
            wos_api_key="",
            google_scholar_api_key="",
            contact_email="",
        )
        with TestClient(create_app(self.database, deployment_settings=settings)) as client:
            diagnostics = client.post(
                "/api/v1/diagnostics",
                json={
                    "source": "arxiv",
                    "llm": {
                        "provider": "custom",
                        "api_type": "openai_chat",
                        "model": "loopback-model",
                        "base_url": base_url,
                    },
                    "credentials": {"llm": {"api_key": secret}},
                },
            )
            self.assertEqual(diagnostics.status_code, 200)
            self.assertEqual(diagnostics.json()["status"], "pass")
            self.assertNotIn(secret, diagnostics.text)

        run = self.submission_service.create_run(
            "Custom loopback composition",
            "arxiv",
            {
                "search_budget": "fast",
                "llm": {
                    "provider": "custom",
                    "api_type": "openai_chat",
                    "model": "loopback-model",
                    "base_url": base_url,
                },
            },
        )
        credentials = EncryptedRunCredentialStore(self.database)
        credentials.save(run.id, {"llm": {"api_key": secret}})
        execution = CommunityExecutionResolver(settings, credentials)(run, NEVER_CANCELLED)
        import asyncio

        output = asyncio.run(execution.llm.chat_json([{"role": "user", "content": "Return JSON."}]))
        self.assertEqual(output, '{"ok":true}')
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(requests[0]["authorization"], f"Bearer {secret}")
        self.assertEqual(requests[0]["body"]["model"], "loopback-model")
        serialized = json.dumps(self.query_service.get_run(run.id).model_dump(mode="json"))
        self.assertNotIn(secret, serialized)
        if execution.close is not None:
            asyncio.run(execution.close())

    def test_community_composition_connects_external_retrieval_ports_without_persisting_keys(self) -> None:
        run = self.submission_service.create_run(
            "External retrieval composition",
            "openalex",
            {
                "search_budget": "fast",
                "retrieval": {
                    "embedding": {
                        "provider": "openai",
                        "model": "text-embedding-3-large",
                        "base_url": "https://embedding.example/v1",
                    },
                    "reranker": {
                        "provider": "nvidia",
                        "model": "nv-rerank-qa-mistral-4b:1",
                        "base_url": "https://reranker.example/reranking",
                    },
                },
            },
        )
        store = EncryptedRunCredentialStore(self.database)
        store.save(
            run.id,
            {
                "embedding": {"api_key": "embedding-composition-secret"},
                "reranker": {"api_key": "reranker-composition-secret"},
            },
        )
        settings = CommunityDeploymentSettings(
            llm_api_type="openai_chat",
            llm_base_url="https://example.invalid/v1",
            llm_model="fixture-model",
            llm_api_key="",
            openalex_api_key="",
            semantic_scholar_api_key="",
            pubmed_api_key="",
            wos_api_key="",
            google_scholar_api_key="",
            contact_email="",
        )

        execution = CommunityExecutionResolver(settings, store)(run, NEVER_CANCELLED)

        self.assertEqual(execution.embedding.provider, "openai")
        self.assertEqual(execution.embedding.model, "text-embedding-3-large")
        self.assertEqual(execution.reranker.provider, "nvidia")
        self.assertEqual(execution.reranker.model, "nv-rerank-qa-mistral-4b:1")
        serialized = json.dumps(self.query_service.get_run(run.id).model_dump(mode="json"))
        self.assertNotIn("embedding-composition-secret", serialized)
        self.assertNotIn("reranker-composition-secret", serialized)
        if execution.close is not None:
            import asyncio

            asyncio.run(execution.close())

    def test_structured_native_fields_reach_the_workflow_budget(self) -> None:
        run = self.submission_service.create_run(
            "Structured source fields",
            "openalex",
            {
                "search_budget": "fast",
                "field_ids": ["17", "22"],
                "field_hint": "",
                "citation_enabled": True,
            },
        )
        settings = CommunityDeploymentSettings(
            llm_api_type="openai_chat",
            llm_base_url="https://example.invalid/v1",
            llm_model="fixture-model",
            llm_api_key="",
            openalex_api_key="",
            semantic_scholar_api_key="",
            pubmed_api_key="",
            wos_api_key="",
            google_scholar_api_key="",
            contact_email="",
        )
        execution = CommunityExecutionResolver(
            settings,
            EncryptedRunCredentialStore(self.database),
        )(run, NEVER_CANCELLED)
        self.assertEqual(execution.request.query_budget.field_ids, ("17", "22"))
        self.assertIn("Discipline limit: Computer Science, Engineering", execution.request.source_context)
        if execution.close is not None:
            import asyncio

            asyncio.run(execution.close())

    def test_text_filter_reaches_query_context_without_changing_the_saved_question(self) -> None:
        run = self.submission_service.create_run(
            "Context-preserving source search",
            "paperhub",
            {
                "search_budget": "fast",
                "field_ids": [],
                "field_hint": "distributed systems",
                "citation_enabled": False,
            },
        )
        settings = CommunityDeploymentSettings(
            llm_api_type="openai_chat",
            llm_base_url="https://example.invalid/v1",
            llm_model="fixture-model",
            llm_api_key="",
            openalex_api_key="",
            semantic_scholar_api_key="",
            pubmed_api_key="",
            wos_api_key="",
            google_scholar_api_key="",
            contact_email="",
        )
        execution = CommunityExecutionResolver(
            settings,
            EncryptedRunCredentialStore(self.database),
        )(run, NEVER_CANCELLED)
        self.assertEqual(execution.request.question, "Context-preserving source search")
        self.assertIn("Research field/context hint: distributed systems", execution.request.source_context)
        if execution.close is not None:
            import asyncio

            asyncio.run(execution.close())


if __name__ == "__main__":
    unittest.main()
