from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from fixtures.review_support import ROUTE, ReviewLLM, create_source, review_settings
from sqlalchemy import create_engine, text

from paperseek_app.review.execution import ReviewExecution
from paperseek_community.api import create_app


class WorkspaceDataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.source = create_source(directory, 3)
        self.database = directory / "paperseek.db"
        app = create_app(self.database, deployment_settings=review_settings(), execution_resolver=None,
                         accept_submissions_without_worker=True,
                         review_execution_resolver=lambda *_: ReviewExecution(llm=ReviewLLM(), route=ROUTE))
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def sql(self, statement, **values):
        engine = create_engine(f"sqlite:///{self.database}")
        try:
            with engine.begin() as connection:
                return connection.execute(text(statement), values).fetchall() if statement.lstrip().upper().startswith("SELECT") \
                    else connection.execute(text(statement), values)
        finally:
            engine.dispose()

    def create_review(self):
        response = self.client.post("/api/v1/reviews", headers={"Idempotency-Key": str(uuid4())}, json={
            "source_run_id": self.source.id, "paper_uids": ["fixed-001", "fixed-002"], "topic": "Compare",
            "language": "en", "length": "short", "section_count": 1})
        self.assertEqual(response.status_code, 201, response.text)

    def test_summary_counts_saved_records_and_running_tasks(self):
        self.create_review()
        summary = self.client.get("/api/v1/data/summary").json()
        self.assertEqual({key: summary[key] for key in ("searches", "analyses", "reviews", "active")},
                         {"searches": 1, "analyses": 0, "reviews": 1, "active": 0})
        self.assertTrue(summary["location"].endswith(Path(self.temp.name).name))

    def test_clearing_history_stops_running_tasks_first_then_deletes_everything(self):
        self.create_review()
        self.sql("UPDATE runs SET status='running' WHERE id=:id", id=self.source.id)
        stopping = self.client.post("/api/v1/data/history/clear").json()
        self.assertEqual((stopping["status"], stopping["active"]), ("stopping", 1))
        # Nothing is deleted while a task is still stopping.
        self.assertEqual(self.client.get("/api/v1/reviews").json()["total_count"], 1)
        self.assertTrue(self.sql("SELECT cancel_requested_at FROM runs WHERE id=:id", id=self.source.id)[0][0])
        self.sql("UPDATE runs SET status='cancelled' WHERE id=:id", id=self.source.id)
        cleared = self.client.post("/api/v1/data/history/clear").json()
        self.assertEqual(cleared, {"status": "cleared", "active": 0, "searches": 1, "analyses": 0, "reviews": 1})
        summary = self.client.get("/api/v1/data/summary").json()
        self.assertEqual([summary[key] for key in ("searches", "analyses", "reviews", "active")], [0, 0, 0, 0])
        self.assertEqual(self.client.get("/api/v1/history").json()["items"], [])

    def test_clearing_secrets_removes_connections_and_source_keys_only(self):
        created = self.client.post("/api/v1/settings/connections", json={
            "kind": "llm", "provider": "deepseek", "api_type": "openai_chat", "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash", "api_key": "sk-workspace-test"})
        self.assertEqual(created.status_code, 201, created.text)
        saved = self.client.put("/api/v1/settings/sources/openalex", json={"api_key": "openalex-key", "options": {"openalex_email": "me@example.edu"}})
        self.assertEqual(saved.status_code, 204, saved.text)
        result = self.client.delete("/api/v1/settings/secrets")
        self.assertEqual(result.json(), {"connections": 1, "source_keys": 1, "proxies": 0})
        settings = self.client.get("/api/v1/settings").json()
        self.assertEqual(settings["connections"], [])
        openalex = next(item for item in settings["sources"] if item["id"] == "openalex")
        self.assertFalse(openalex["has_key"])
        self.assertEqual(openalex["options"]["openalex_email"], "me@example.edu")
        self.assertEqual(settings["defaults"]["search_llm"], "")
        # Search history is untouched.
        self.assertEqual(self.client.get("/api/v1/data/summary").json()["searches"], 1)


if __name__ == "__main__":
    unittest.main()
