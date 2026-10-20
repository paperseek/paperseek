from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

MCP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_ROOT / "src"))


class M4MCPApplicationContract(unittest.TestCase):

    def test_source_capabilities_and_input_validation_use_community_contract(self):
        from paperseek_mcp.application import CommunityMCPApplication

        with tempfile.TemporaryDirectory() as directory:
            application = CommunityMCPApplication(Path(directory) / "paperseek.db")

            sources = application.list_sources()["sources"]
            self.assertEqual(application.get_source_capabilities(source="openalex"),
                             next(source for source in sources if source["id"] == "openalex"))
            with self.assertRaisesRegex(ValueError, "not supported"):
                application.get_source_capabilities(source="missing")

    def test_pagination_arguments_are_validated_before_storage_access(self):
        from paperseek_mcp.application import CommunityMCPApplication

        with tempfile.TemporaryDirectory() as directory:
            application = CommunityMCPApplication(Path(directory) / "paperseek.db")

            with self.assertRaisesRegex(ValueError, "event cursor"):
                application.get_search_events(
                    run_id="missing",
                    after_sequence=-1,
                    limit=20,
                )
            with self.assertRaisesRegex(ValueError, "Event limit"):
                application.get_search_events(
                    run_id="missing",
                    after_sequence=0,
                    limit=201,
                )



def submission_digest(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT request_digest FROM run_submissions").fetchone()[0]


class SubmissionAdapterTests(unittest.TestCase):
    def test_api_and_mcp_submit_the_same_run_with_default_or_explicit_options(self):
        from paperseek_app.source_catalog import SOURCE_BY_ID
        from paperseek_community.api import create_app
        from paperseek_mcp.application import CommunityMCPApplication

        field = SOURCE_BY_ID["openalex"].descriptor()["capabilities"]["filter"]["options"][0]["id"]
        for options in ({}, {"source": "openalex", "search_budget": "high", "field_ids": [field, field],
                            "field_hint": "", "citation_enabled": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                api_db, mcp_db = (Path(directory) / name for name in ("api.db", "mcp.db"))
                values = {"question": "  Durable submission equivalence  ", **options}
                with TestClient(create_app(api_db, execution_resolver=lambda *_args: None)) as client:
                    response = client.post("/api/v1/runs", headers={"Idempotency-Key": "same"}, json=values)
                    self.assertEqual(response.status_code, 202, response.text)
                application = CommunityMCPApplication(mcp_db)
                started = application.start_search(**values, idempotency_key="same")
                run_id = started["run_id"]
                detail = application.get_search_status(run_id=run_id)
                for field in ("question", "source", "config_snapshot", "status"):
                    self.assertEqual(detail[field], response.json()[field])
                self.assertEqual(submission_digest(mcp_db), submission_digest(api_db))
                events = application.get_search_events(run_id=run_id, after_sequence=0, limit=20)
                self.assertEqual(events["events"][0]["event_type"], "run.created")
                replay = application.get_search_events(run_id=run_id, after_sequence=events["next_sequence"], limit=20)
                self.assertEqual(replay["events"], [])
                self.assertEqual(application.cancel_search(run_id=run_id)["status"], "cancelled")
                self.assertEqual(application.get_search_status(run_id=run_id)["status"], "cancelled")

    def test_api_and_mcp_preserve_the_shared_validation_error(self):
        from paperseek_app.submission import SubmissionValidationError
        from paperseek_community.api import create_app
        from paperseek_mcp.application import CommunityMCPApplication

        with tempfile.TemporaryDirectory() as directory:
            application = CommunityMCPApplication(Path(directory) / "mcp.db")
            values = {"question": "x", "source": "arxiv", "citation_enabled": True}
            with TestClient(create_app(Path(directory) / "api.db", execution_resolver=lambda *_args: None)) as client:
                response = client.post("/api/v1/runs", headers={"Idempotency-Key": "invalid"}, json=values)
            self.assertEqual(response.status_code, 422)
            with self.assertRaises(SubmissionValidationError) as caught:
                application.start_search(**values, idempotency_key="invalid")
            self.assertEqual(caught.exception.code, "unsupported_source_capability")
            self.assertEqual(response.json()["error"]["code"], caught.exception.code)
