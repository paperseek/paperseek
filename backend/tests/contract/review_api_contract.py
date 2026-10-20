from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from paperseek_app.review.execution import ReviewExecution
from paperseek_community.api import create_app
from paperseek_community.persistence.sqlite_review_jobs import SQLiteReviewUnitOfWork
from fixtures.review_support import ROUTE, ReviewLLM, create_source, review_settings


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = create_source(self.directory, 3)
        self.database = self.directory / "paperseek.db"
        self.store = SQLiteReviewUnitOfWork(self.database)
        self.addCleanup(self.store.close)
        self.llm = ReviewLLM()

    def client(self, *, worker=True):
        app = create_app(self.database, deployment_settings=review_settings(),
                         execution_resolver=(lambda *_: self.fail("Unexpected search task")) if worker else None,
                         accept_submissions_without_worker=not worker,
                         review_execution_resolver=lambda *_: ReviewExecution(llm=self.llm, route=ROUTE))
        client = TestClient(app)
        self.addCleanup(client.__exit__, None, None, None)
        client.__enter__()
        return client

    def create(self, client, *, key=None, uids=None):
        return client.post("/api/v1/reviews", headers={"Idempotency-Key": key or str(uuid4())}, json={
            "source_run_id": self.source.id, "paper_uids": uids or ["fixed-001", "fixed-002", "fixed-003"],
            "topic": "Compare durable studies", "language": "en", "length": "short", "section_count": 2})

    def submit(self, client, review, action="prepare_outline", key=None):
        return client.post(f"/api/v1/reviews/{review['id']}/jobs", headers={"Idempotency-Key": key or str(uuid4())}, json={
            "action": action, "expected_version": review["version"],
            "outline_revision": review["content"]["outline_revision"], "llm": ROUTE,
            "credential": {"api_key": "ephemeral-secret"}})

    def finish(self, client, review_id):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            review = client.get(f"/api/v1/reviews/{review_id}").json()
            if not review["active_job"]:
                self.assertIn(review["latest_job"]["status"], {"completed", "completed_with_warnings"}, review)
                return review
            time.sleep(.02)
        self.fail("Review task did not finish")

    def test_nonresearch_card_is_retained_but_not_required_in_outline_or_writing(self):
        original = self.llm.chat_json

        async def metadata_only(messages, **kwargs):
            result = json.loads(await original(messages, **kwargs))
            for card in result.get('cards', []):
                if card['paper_id'] == 'p_003':
                    card['evidence'] = []
            return json.dumps(result)

        self.llm.chat_json = metadata_only
        client = self.client()
        review = self.create(client).json()
        self.assertEqual(self.submit(client, review).status_code, 202)
        review = self.finish(client, review['id'])
        self.assertEqual(review['content']['evidence_errors'], {'p_003': 'review_evidence_unusable'})
        self.assertEqual(len(review['content']['cards']), 3)
        self.assertEqual({p for s in review['content']['outline']['sections'] for p in s['paper_ids']}, {'p_001', 'p_002'})
        saved = client.put(f"/api/v1/reviews/{review['id']}/outline", json={
            'expected_version': review['version'], 'expected_revision': review['content']['outline_revision'],
            'outline': review['content']['outline']})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.submit(client, saved.json(), 'write').status_code, 202)
        written = self.finish(client, review['id'])
        self.assertEqual(len(written['document']['sections']), 2)
        self.assertEqual({r['paper_id'] for r in written['document']['references']}, {'p_001', 'p_002'})
        evidence_calls = self.llm.calls['evidence']
        self.assertEqual(self.submit(client, written).status_code, 202)
        regenerated = self.finish(client, review['id'])
        self.assertEqual(self.llm.calls['evidence'], evidence_calls)
        self.assertEqual(len(regenerated['document']['sections']), 2)
        self.assertTrue(all(s['stale'] for s in regenerated['document']['sections']))

    def test_custom_paragraph_settings_are_saved_with_the_review(self):
        client = self.client()
        response = client.post("/api/v1/reviews", headers={"Idempotency-Key": str(uuid4())}, json={
            "source_run_id": self.source.id, "paper_uids": ["fixed-001", "fixed-002"], "topic": "Compare",
            "language": "zh", "length": "standard", "section_count": 3, "paragraph_count": 4, "paragraph_length": 300})
        self.assertEqual(response.status_code, 201, response.text)
        content = client.get(f"/api/v1/reviews/{response.json()['id']}").json()["content"]
        self.assertEqual((content["paragraph_count"], content["paragraph_length"]), (4, 300))
        response = client.post("/api/v1/reviews", headers={"Idempotency-Key": str(uuid4())}, json={
            "source_run_id": self.source.id, "paper_uids": ["fixed-001", "fixed-002"], "topic": "Compare",
            "language": "en", "paragraph_length": 1200})
        self.assertEqual(response.status_code, 422)

    def settle(self, client, review_id):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            review = client.get(f"/api/v1/reviews/{review_id}").json()
            if not review["active_job"]:
                return review
            time.sleep(.02)
        self.fail("Review task did not finish")

    def test_failed_section_keeps_sibling_sections_and_retry_redoes_only_the_failure(self):
        client = self.client()
        review = self.create(client).json()
        self.assertEqual(self.submit(client, review).status_code, 202)
        review = self.finish(client, review["id"])
        original = self.llm.chat_json
        broken = {"theme_2"}

        async def sabotage(messages, **kwargs):
            result = await original(messages, **kwargs)
            content = messages[0]["content"]
            if "TASK: section\n" in content and json.loads(messages[1]["content"])["section"]["id"] in broken:
                value = json.loads(result)
                value["paragraphs"][0]["evidence_ids"] = ["invented"]
                return json.dumps(value)
            return result

        self.llm.chat_json = sabotage
        self.assertEqual(self.submit(client, review, "write").status_code, 202)
        review = self.settle(client, review["id"])
        self.assertEqual(review["latest_job"]["error_code"], "review_model_output_invalid")
        self.assertEqual(review["generation_failure"]["task"], "section")
        self.assertEqual(review["latest_request"]["action"], "write")
        self.assertEqual([s["section_id"] for s in review["content"]["sections"]], ["theme"])
        written = self.llm.calls["section"]
        broken.clear()
        self.assertEqual(self.submit(client, review, "write").status_code, 202)
        review = self.finish(client, review["id"])
        self.assertEqual(len(review["document"]["sections"]), 2)
        # The retry writes only the failed section; the saved one is reused.
        self.assertEqual(self.llm.calls["section"] - written, 1)

    def test_complete_api_flow_sse_replay_credentials_versions_exports_and_delete(self):
        client = self.client()
        key = str(uuid4())
        created = self.create(client, key=key)
        self.assertEqual(created.status_code, 201, created.text)
        review = created.json()
        self.assertEqual(self.create(client, key=key).json()["id"], review["id"])
        root = f"/api/v1/reviews/{review['id']}"
        response = self.submit(client, review)
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        review = self.finish(client, review["id"])
        self.assertEqual(review["document"]["sections"], [])
        self.assertFalse(self.store.credential_store.resolve(job["id"]))
        for suffix in ["", "/results", "/audit", "/events", "/citation-map", "/export?format=json"]:
            self.assertEqual(client.get(f"/api/v1/runs/{job['id']}{suffix}").status_code, 404, suffix)
        stream = client.get(f"{root}/jobs/{job['id']}/events")
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(events)
        self.assertNotIn("ephemeral-secret", stream.text + json.dumps(review))
        self.assertEqual(client.get(f"{root}/jobs/{job['id']}/events", headers={"Last-Event-ID": str(events[-1]["sequence"])}).text, "")
        self.assertEqual(client.get(f"{root}/jobs/{job['id']}/events", headers={"Last-Event-ID": "-1"}).status_code, 400)
        self.assertEqual(self.submit(client, review, "write").status_code, 202)
        review = self.finish(client, review["id"])
        self.assertEqual(len(review["document"]["sections"]), 2)
        self.assertFalse(review["document"]["draft"])
        harvard = client.get(f"{root}?citation_style=harvard").json()
        self.assertEqual(harvard["document"]["citation_style"], "harvard")
        self.assertEqual(harvard["version"], review["version"])
        self.assertEqual(harvard["content"], review["content"])
        self.assertIn("Researcher", harvard["document"]["sections"][0]["paragraphs"][0]["citations"][0]["label"])
        self.assertEqual(client.get(f"{root}?citation_style=unknown").status_code, 422)
        harvard_md = client.get(f"{root}/export?format=md&version={review['version']}&citation_style=harvard")
        self.assertEqual(harvard_md.status_code, 200)
        self.assertIn("Researcher, 2026", harvard_md.text)
        for fmt in ["md", "docx", "bibtex"]:
            exported = client.get(f"{root}/export?format={fmt}&version={review['version']}")
            self.assertEqual(exported.status_code, 200, exported.text[:100] if fmt != "docx" else "")
            self.assertGreater(len(exported.content), 100)
        self.assertEqual(client.get(f"{root}/export?format=md&version=1").status_code, 409)
        self.assertEqual(len(client.get(f"{root}/versions/theme").json()["items"]), 1)
        other = self.create(client).json()
        self.assertEqual(client.get(f"/api/v1/reviews/{other['id']}/jobs/{job['id']}").status_code, 404)
        self.assertEqual(client.delete(f"{root}?version={review['version']}").status_code, 204)
        self.assertEqual(client.get(root).status_code, 404)
        self.assertEqual(client.get(f"/api/v1/runs/{self.source.id}").status_code, 200)
        with self.store.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM review_revisions WHERE review_id=:id"), {"id": review["id"]}).scalar(), 0)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM run_credentials")).scalar(), 0)

    def test_invalid_selection_missing_abstract_busy_and_cancelled_queue(self):
        client = self.client(worker=False)
        self.assertEqual(self.create(client, uids=["fixed-001", "forged"]).status_code, 422)
        review = self.create(client).json()
        root = f"/api/v1/reviews/{review['id']}"
        key = str(uuid4())
        job = self.submit(client, review, key=key).json()
        self.assertEqual(self.submit(client, review, key=key).json()["id"], job["id"])
        self.assertEqual(client.delete(f"{root}?version={review['version']}").status_code, 409)
        search = client.post("/api/v1/runs", headers={"Idempotency-Key": str(uuid4())}, json={
            "question": "Test search", "source": "openalex", "llm": ROUTE, "credentials": {"llm": {"api_key": "x"}}})
        self.assertEqual(search.status_code, 409, search.text)
        self.assertEqual(search.json()["error"]["code"], "community_task_active")
        cancelled = client.post(f"{root}/jobs/{job['id']}/cancel")
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertFalse(self.store.credential_store.resolve(job["id"]))
        again = client.post(f"{root}/jobs/{job['id']}/cancel")
        self.assertIn(again.status_code, {202, 409})

    def test_paper_suggestions_cover_the_topic_without_creating_anything(self):
        source = create_source(self.directory, 6)
        client = self.client(worker=False)
        body = {"source_run_id": source.id, "topic": "Compare durable studies", "language": "en", "count": 3,
                "llm": ROUTE, "credential": {"api_key": "ephemeral-secret"}}
        original = self.llm.chat_json
        prompts = []

        async def record(messages, **kwargs):
            prompts.append(messages)
            if len(prompts) == 1:
                return json.dumps({"papers": [{"id": "c1"}, {"id": "c1"}, {"id": "c99"}]})
            return await original(messages, **kwargs)

        self.llm.chat_json = record
        with patch("paperseek_community.api_routes.reviews.HttpLLMAdapter", lambda **_: self.llm):
            response = client.post("/api/v1/reviews/paper-suggestions", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([item["uid"] for item in response.json()["items"]], ["fixed-001", "fixed-002", "fixed-003"])
            self.assertEqual(response.json()["items"][0]["reason"], "Covers a distinct setting.")
            self.assertIn("c99", prompts[-1][-1]["content"])
            self.assertTrue(all(len(item["abstract"]) <= 300 for item in json.loads(prompts[0][1]["content"])["candidates"]))
            # Asking for more papers than the search has returns every paper with an abstract, without a model call.
            calls = len(prompts)
            everything = client.post("/api/v1/reviews/paper-suggestions", json={**body, "count": 50}).json()["items"]
            self.assertEqual(len(everything), 6)
            self.assertEqual(len(prompts), calls)
            missing = client.post("/api/v1/reviews/paper-suggestions", json={**body, "credential": {}})
            self.assertEqual(missing.json()["error"]["code"], "review_credential_required")
        self.assertEqual(client.get("/api/v1/reviews").json()["items"], [])

    def test_no_key_is_reused_for_a_different_endpoint(self):
        client = self.client(worker=False)
        review = self.create(client).json()
        response = client.post(f"/api/v1/reviews/{review['id']}/jobs", headers={"Idempotency-Key": str(uuid4())},
                               json={"action": "prepare_outline", "expected_version": review["version"], "llm": ROUTE})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "review_credential_required")
