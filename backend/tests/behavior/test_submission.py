from __future__ import annotations

import asyncio
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from fixtures.submission import settings
from pydantic import TypeAdapter, ValidationError

from paperseek_app.configuration import RunConfigurationSnapshot
from paperseek_app.services import RunSubmissionService
from paperseek_app.submission import (
    LLMRouteInput,
    RunSubmission,
    SubmissionValidationError,
    prepare_run_submission,
)
from paperseek_community.api import create_app
from paperseek_community.cli import _parser
from paperseek_community.composition import CommunityExecutionResolver
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.diagnostics import run_configuration_diagnostics
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.disciplines import WOS_CATEGORIES
from paperseek_core.execution import NEVER_CANCELLED
from paperseek_core.models import CoreProgressEvent
from paperseek_core.sources.contracts import SourceSearchRequest
from paperseek_core.sources.wos import WosSource


class SharedSubmissionTests(unittest.TestCase):
    def setUp(self):
        # Hosted supplies deployment defaults, not a Community environment reader or credentials.
        self.defaults = SimpleNamespace(llm_api_type="openai_responses", llm_base_url="https://api.openai.com/v1",
                                        llm_model="gpt-5.4-mini", wos_db="WOS")

    def test_equivalent_inputs_share_effective_config_digest_and_exclude_credentials(self):
        first = prepare_run_submission(RunSubmission(question="  evidence  ", source=" OpenAlex ",
                                                      credentials={"llm": {"api_key": "private-test-key"}}), self.defaults)
        second = prepare_run_submission(RunSubmission(question="evidence", source="openalex"), self.defaults)
        self.assertEqual(first, second)
        self.assertEqual(first.config_snapshot["llm"], {"provider": "openai", "api_type": "openai_responses",
                                                       "base_url": "https://api.openai.com/v1", "model": "gpt-5.4-mini"})
        self.assertEqual(first.config_snapshot["wos_db"], "WOS")
        self.assertNotIn("private-test-key", str(first))

    def test_shared_validation_retains_public_error_codes_and_source_capabilities(self):
        for values, code in (({"question": " "}, "invalid_question"),
                             ({"source": "missing"}, "unsupported_source"),
                             ({"source": "arxiv", "citation_enabled": True}, "unsupported_source_capability")):
            with self.subTest(code=code), self.assertRaises(SubmissionValidationError) as caught:
                prepare_run_submission(RunSubmission.model_validate({"question": "evidence"} | values), self.defaults)
            self.assertEqual(caught.exception.code, code)

    def test_service_choice_is_explicit_and_part_of_the_same_request_digest(self):
        connection_id = str(uuid4())
        choices = ({"mode": "paperseek"}, {"mode": "byok"},
                   {"mode": "third_party", "connection_id": connection_id})
        default = prepare_run_submission(RunSubmission(question="evidence"), self.defaults)
        null = prepare_run_submission(RunSubmission(question="evidence", service=None), self.defaults)
        self.assertEqual(default, null)
        self.assertNotIn("service_mode", default.config_snapshot)
        digests = {default.request_digest}
        for choice in choices:
            with self.subTest(choice=choice):
                prepared = prepare_run_submission(RunSubmission(question="evidence", service=choice,
                    credentials={"llm": {"api_key": "not-in-snapshot"}}), self.defaults)
                self.assertEqual(prepared.config_snapshot["service_mode"], choice["mode"])
                if choice["mode"] == "third_party":
                    self.assertEqual(prepared.config_snapshot["connection_id"], connection_id)
                else:
                    self.assertNotIn("connection_id", prepared.config_snapshot)
                self.assertNotIn("not-in-snapshot", str(prepared))
                self.assertNotIn(prepared.request_digest, digests)
                digests.add(prepared.request_digest)
        another = prepare_run_submission(RunSubmission(question="evidence",
            service={"mode": "third_party", "connection_id": str(uuid4())}), self.defaults)
        self.assertNotIn(another.request_digest, digests)

    def test_service_choice_rejects_ambiguous_or_invalid_connection_selection(self):
        for choice in ({"mode": "auto"}, {"mode": "third_party"},
                       {"mode": "third_party", "connection_id": "not-a-uuid"},
                       {"mode": "paperseek", "connection_id": str(uuid4())},
                       {"mode": "byok", "connection_id": str(uuid4())}):
            with self.subTest(choice=choice), self.assertRaises(ValidationError):
                RunSubmission(question="evidence", service=choice)


class SubmissionConfigurationTests(unittest.TestCase):
    def test_deployment_settings_reject_invalid_routes_without_exposing_keys(self) -> None:
        values = {**settings().model_dump(), "llm_api_key": "private-fixture-key"}
        for field, value in (("llm_api_type", "unsupported"), ("llm_base_url", "missing-host"), ("llm_model", "")):
            with self.subTest(field=field), self.assertRaises(ValidationError) as caught:
                CommunityDeploymentSettings(**{**values, field: value})
            self.assertNotIn("private-fixture-key", str(caught.exception))
        self.assertNotIn("private-fixture-key", repr(CommunityDeploymentSettings(**values)))

    def test_larger_budgets_reach_execution_without_extending_the_run_deadline(self) -> None:
        async def check():
            resolver = CommunityExecutionResolver(settings(), SimpleNamespace(resolve=lambda _id: {}))
            previous = (0, 0, 0)
            for name in ("fast", "medium", "high", "max"):
                with self.subTest(budget=name):
                    prepared = prepare_run_submission(RunSubmission(question="limits", search_budget=name), settings())
                    run = SimpleNamespace(id="controlled", source=prepared.source, question=prepared.question,
                                          config_snapshot=prepared.config_snapshot,
                                          deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10))
                    execution = resolver(run, NEVER_CANCELLED)
                    try:
                        request, query = execution.request, execution.request.query_budget
                        self.assertGreater(execution.deadline_at, time.monotonic())
                        self.assertLessEqual(execution.deadline_at - time.monotonic(), 600)
                        current = (query.max_iterations, query.ranking_candidate_limit, request.citation_max_records)
                        self.assertTrue(all(new > old for old, new in zip(previous, current)))
                        previous = current
                    finally:
                        await execution.close()

        asyncio.run(check())

    def test_omitted_citation_option_follows_source_capability(self) -> None:
        arxiv = prepare_run_submission(
            RunSubmission(question="evidence synthesis", source="arxiv"),
            settings(),
        )
        disabled = prepare_run_submission(
            RunSubmission(
                question="evidence synthesis",
                source="openalex",
                citation_enabled=False,
            ),
            settings(),
        )

        self.assertFalse(arxiv.config_snapshot["citation_enabled"])
        self.assertFalse(disabled.config_snapshot["citation_enabled"])
        with self.assertRaises(SubmissionValidationError) as raised:
            prepare_run_submission(
                RunSubmission(
                    question="evidence synthesis",
                    source="arxiv",
                    citation_enabled=True,
                ),
                settings(),
            )
        self.assertEqual(raised.exception.code, "unsupported_source_capability")


    def test_public_values_are_normalized_and_blank_question_is_rejected(self) -> None:
        prepared = prepare_run_submission(
            RunSubmission(
                question="  evidence synthesis  ",
                source=" OpenAlex ",
                search_budget=" FAST ",
                llm=LLMRouteInput(provider=" ANTHROPIC "),
            ),
            settings(),
        )

        self.assertEqual(prepared.source, "openalex")
        self.assertEqual(prepared.config_snapshot["search_budget"], "fast")
        self.assertEqual(prepared.config_snapshot["llm"]["provider"], "anthropic")
        self.assertEqual(prepared.config_snapshot["llm"]["api_type"], "anthropic_messages")
        with self.assertRaises(SubmissionValidationError) as raised:
            prepare_run_submission(RunSubmission(question="   "), settings())
        self.assertEqual(raised.exception.code, "invalid_question")

    def test_unknown_llm_provider_is_rejected_by_submission_contract(self) -> None:
        with self.assertRaises(SubmissionValidationError) as raised:
            prepare_run_submission(
                RunSubmission(
                    question="evidence synthesis",
                    llm=LLMRouteInput(provider="not-a-provider"),
                ),
                settings(),
            )
        self.assertEqual(raised.exception.code, "unsupported_llm_provider")


class PublicBoundaryContract(unittest.TestCase):
    def test_unknown_options_are_rejected_at_public_submission_boundaries(self):
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory) / "test.db", accept_submissions_without_worker=True)) as client:
            for endpoint, values in (("runs", {"question": "Native abstracts"}), ("diagnostics", {})):
                for unknown in ({"unknown_option": True}, {"retrieval": {"unknown_option": True}}):
                    with self.subTest(endpoint=endpoint, fields=unknown):
                        response = client.post(f"/api/v1/{endpoint}", json={**values, **unknown})
                        self.assertEqual(response.status_code, 422, response.text)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args(["search", "Native abstracts", "--unknown-option"])

    def test_recorded_config_keeps_missing_fields_missing_and_validates_known_values(self):
        adapter = TypeAdapter(RunConfigurationSnapshot)
        old = {"source": "openalex", "legacy_id": "old-1"}
        self.assertEqual(adapter.validate_python(old), old)
        with self.assertRaises(ValidationError):
            adapter.validate_python({"retrieval": {"embedding": {"model": 42}}})
        snapshot = {"retrieval": {"embedding": {"model": "recorded-model"}}}
        self.assertEqual(adapter.validate_python(snapshot), snapshot)

    def test_event_metrics_are_checked_but_audit_extensions_survive(self):
        event = CoreProgressEvent(event_type="ranking.completed", stage="ranking", payload={"candidate_count": 3, "audit_extension": [1, 2]})
        self.assertEqual(event.payload["audit_extension"], [1, 2])
        with self.assertRaises(ValidationError):
            CoreProgressEvent(event_type="ranking.completed", stage="ranking", payload={"candidate_count": "three"})


CONTACTS = {"openalex_email": "openalex@example.test", "crossref_email": "crossref@example.test",
            "pubmed_email": "pubmed@example.test", "pubmed_tool": "research-team"}


class SourceContactsContract(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"PAPERSEEK_CONTACT_EMAIL": "deployment@example.test"}, clear=True):
            self.settings = CommunityDeploymentSettings.from_environment()

    def test_contacts_are_normalized_in_snapshot_and_digest(self):
        default = prepare_run_submission(RunSubmission(question="source metadata"), self.settings)
        selected = prepare_run_submission(RunSubmission(question="source metadata",
            source_contacts={key: f" {value} " for key, value in CONTACTS.items()}), self.settings)
        self.assertEqual(selected.config_snapshot["source_contacts"], CONTACTS)
        self.assertNotEqual(default.request_digest, selected.request_digest)
        self.assertEqual(default.config_snapshot["source_contacts"],
            {"openalex_email": "", "crossref_email": "", "pubmed_email": "", "pubmed_tool": "paperseek"})

    def test_composition_sends_separate_source_contacts_and_keeps_crossref_independent(self):
        requests = []

        async def handle(request):
            requests.append(request)
            if request.url.host == "api.openalex.org":
                return httpx.Response(200, json={"meta": {"count": 0}, "results": []})
            if request.url.host == "api.crossref.org":
                return httpx.Response(200, json={"message": {"items": [], "total-results": 0}})
            return httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})

        real_client = httpx.AsyncClient
        def client(*args, **kwargs):
            return real_client(*args, **kwargs, transport=httpx.MockTransport(handle))

        async def exercise():
            for source in ("openalex", "crossref", "pubmed"):
                prepared = prepare_run_submission(RunSubmission(question="source metadata", source=source,
                    source_contacts=CONTACTS), self.settings)
                run = SimpleNamespace(id=source, source=source, question=prepared.question,
                                      config_snapshot=prepared.config_snapshot,
                                      deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10))
                with patch("paperseek_community.model_transport.httpx.AsyncClient", client):
                    execution = CommunityExecutionResolver(self.settings, Mock(resolve=Mock(return_value={})))(run, NEVER_CANCELLED)
                try:
                    await execution.source.search(SourceSearchRequest(query="governance", limit=1, lane="relevance"))
                finally:
                    await execution.close()

        asyncio.run(exercise())
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0].url.params["mailto"], CONTACTS["openalex_email"])
        self.assertEqual(requests[1].url.params["mailto"], CONTACTS["crossref_email"])
        self.assertEqual(requests[2].url.params["email"], CONTACTS["pubmed_email"])
        self.assertEqual(requests[2].url.params["tool"], CONTACTS["pubmed_tool"])


def _settings(*, wos_db: str = "WOS") -> CommunityDeploymentSettings:
    return CommunityDeploymentSettings(
        llm_api_type="openai_chat",
        llm_base_url="https://llm.example.invalid/v1",
        llm_model="controlled-model",
        llm_api_key="controlled-llm-key",
        openalex_api_key="",
        semantic_scholar_api_key="",
        pubmed_api_key="",
        wos_api_key="controlled-wos-key",
        google_scholar_api_key="",
        contact_email="paperseek-test@example.invalid",
        wos_db=wos_db,
    )


class M4WosDatabaseSelectorContract(unittest.TestCase):
    def test_submission_normalizes_database_separately_from_category_and_digest(self) -> None:
        category = WOS_CATEGORIES[0]
        default = prepare_run_submission(
            RunSubmission(
                question="default WoS database",
                source="wos",
                field_ids=[category],
            ),
            _settings(),
        )
        selected = prepare_run_submission(
            RunSubmission(
                question="default WoS database",
                source="wos",
                field_ids=[category],
                wos_db="  WOK  ",
            ),
            _settings(),
        )

        self.assertEqual(default.config_snapshot["wos_db"], "WOS")
        self.assertEqual(selected.config_snapshot["wos_db"], "WOK")
        self.assertEqual(selected.config_snapshot["field_ids"], [category])
        self.assertNotEqual(default.request_digest, selected.request_digest)

    def test_environment_and_cli_expose_the_new_configuration_name(self) -> None:
        with patch.dict(os.environ, {"PAPERSEEK_WOS_DB": "WOK"}, clear=True):
            self.assertEqual(CommunityDeploymentSettings.from_environment().wos_db, "WOK")

        search = _parser().parse_args(
            ["search", "WoS database contract", "--source", "wos", "--wos-db", "WOK"]
        )
        doctor = _parser().parse_args(
            ["doctor", "--source", "wos", "--wos-db", "WOK", "--json"]
        )
        self.assertEqual(search.wos_db, "WOK")
        self.assertEqual(doctor.wos_db, "WOK")

    def test_doctor_reports_the_effective_wos_database_without_network(self) -> None:
        report = run_configuration_diagnostics(
            {
                "source": "wos",
                "wos_db": "WOK",
                "credentials": {
                    "source": {"api_key": "controlled-wos-key"},
                    "llm": {"api_key": "controlled-llm-key"},
                },
            },
            _settings(),
        )
        database = next(
            check for check in report["checks"] if check["id"] == "source.wos_database"
        )

        self.assertEqual(database["status"], "pass")
        self.assertEqual(database["details"], {"database": "WOK"})
        self.assertNotIn("category", database["details"])


class M4WosDatabaseResolverContract(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_sends_selected_database_and_keeps_category_in_query(self) -> None:
        requests: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"metadata": {"total": 0, "page": 1, "limit": 5}, "hits": []},
                request=request,
            )

        controlled = httpx.MockTransport(handle)
        real_client = httpx.AsyncClient

        class ControlledAsyncClient(real_client):
            def __init__(self, *args, **kwargs) -> None:
                kwargs["transport"] = controlled
                super().__init__(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "paperseek.db"
            migrate_database(database)
            uow = SQLiteRunUnitOfWork(database)
            service = RunSubmissionService(uow)
            credentials = EncryptedRunCredentialStore(database)
            category = WOS_CATEGORIES[0]
            prepared = prepare_run_submission(
                RunSubmission(
                    question="WoS database composition",
                    source="wos",
                    field_ids=[category],
                    wos_db="WOK",
                ),
                _settings(),
            )
            run = service.submit_run(
                subject="wos-database-contract",
                idempotency_key=str(uuid4()),
                request_digest=prepared.request_digest,
                question=prepared.question,
                source=prepared.source,
                config_snapshot=prepared.config_snapshot,
            )
            resolver = CommunityExecutionResolver(_settings(), credentials)
            try:
                with patch(
                    "paperseek_community.model_transport.httpx.AsyncClient",
                    ControlledAsyncClient,
                ):
                    execution = resolver(run, NEVER_CANCELLED)
                    self.assertIsInstance(execution.source, WosSource)
                    query = f'TS=(governance) AND WC=("{category}")'
                    await execution.source.search(
                        SourceSearchRequest(query=query, limit=5, lane="relevance")
                    )
                    await execution.close()
            finally:
                uow.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["db"], "WOK")
        self.assertEqual(requests[0].url.params["q"], query)
