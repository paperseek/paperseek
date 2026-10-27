"""Contracts for full-set result filtering/sorting/summary and history search.

The results endpoint must filter, sort and summarize over the run's complete
committed result set; pagination is only a transport concern. History search
must cover the full history, not just the loaded first page.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from paperseek_app.errors import InvalidCursor, InvalidResultQuery
from paperseek_app.models import (
    ArtifactRole,
    ArtifactVisibility,
    ArtifactWrite,
    PendingEvent,
)
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_community.api import create_app
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.persistence import SQLiteRunUnitOfWork, migrate_database
from paperseek_core.models import CheckpointDraft, FinalPaperResult, ResearchResult


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += timedelta(seconds=seconds)


def _record(rank: int, **overrides) -> FinalPaperResult:
    values = {
        "rank": rank,
        "title": f"Paper {rank}",
        "uid": f"paper-{rank}",
        "source": "Journal",
        "provider": "openalex",
        "authors_text": f"Author {rank}",
        "keywords_text": "",
        "abstract": "",
        "doi": "",
        "pdf_url": "",
        "citation_count": 0,
    }
    values.update(overrides)
    return FinalPaperResult(**values)


class M4ResultQueryContractTest(unittest.TestCase):
    def test_idle_event_stream_bounds_reads_and_backs_off(self):
        run = self.submission_service.create_run("Idle event subscriber", "openalex")
        app = create_app(self.database, accept_submissions_without_worker=True)
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/v1/runs/{run_id}/events")
        statements, delays = [], []

        def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().startswith("SELECT"):
                statements.append(statement)

        class Request:
            polls = 0
            async def is_disconnected(self):
                self.polls += 1
                return self.polls >= 20

        async def sleep(delay):
            delays.append(delay)

        async def consume():
            response = await endpoint(Request(), run.id, last_event_id=None)
            statements.clear()
            with patch("paperseek_community.api_routes.event_stream.asyncio.sleep", sleep):
                return [frame async for frame in response.body_iterator]

        event.listen(Engine, "before_cursor_execute", observe)
        try:
            frames = asyncio.run(consume())
        finally:
            event.remove(Engine, "before_cursor_execute", observe)
        self.assertEqual(len(frames), 1)
        self.assertLessEqual(len(statements), 40)
        self.assertGreater(delays[-1], delays[0])
        self.assertLessEqual(max(delays), 1.0)

    def test_cursor_offsets_reject_non_integer_and_out_of_storage_range(self):
        run = self._seed_results()
        with TestClient(create_app(self.database)) as client:
            for offset in (True, 1.9, "2", float("inf"), float("nan"), 0, -1, 10**100):
                for scope in ("results", "history-search"):
                    with self.subTest(offset=offset, scope=scope):
                        fields = {"run_id": run.id, "filter": self.query_service._result_fingerprint(None, None, None, "rank")} if scope == "results" else {"query": "query"}
                        token = self.query_service._encode_cursor({"scope": scope, "offset": offset, **fields})
                        endpoint = f"/api/v1/runs/{run.id}/results" if scope == "results" else "/api/v1/history"
                        parameters = {"cursor": token} if scope == "results" else {"cursor": token, "query": "query"}
                        response = client.get(endpoint, params=parameters)
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertEqual(response.json()["error"]["code"], "invalid_cursor")

    def test_selected_export_only_materializes_selected_records(self):
        run = self._complete([_record(i) for i in range(1, 501)])
        with patch.object(FinalPaperResult, "model_validate", wraps=FinalPaperResult.model_validate) as materialize:
            export = self.query_service.get_selected_export(run.id, "json", ["paper-499", "paper-2"])
        self.assertEqual(materialize.call_count, 2)
        self.assertEqual([paper["uid"] for paper in json.loads(export.content)["ranked"]], ["paper-2", "paper-499"])

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

    def _complete(self, records: list[FinalPaperResult], *, question: str | None = None):
        run = self.submission_service.create_run(
            question or f"Question {self.clock.value.isoformat()}", "openalex"
        )
        lease = self.uow.claim_run(run.id, "contract-worker", timedelta(seconds=60))
        self.execution_service.append_checkpoint(
            lease,
            [PendingEvent(event_type="workflow.progress", stage="workflow", payload={})],
            CheckpointDraft(
                step_key="assemble_final_result",
                idempotency_key="final-before-complete",
                state={"final": True, "step_workflow_version": "m2.research-workflow.v2"},
                workflow_version=run.versions.workflow_version,
                schema_version=1,
            ),
        )
        payload = {"ranked": [record.model_dump(mode="json") for record in records]}
        artifacts = [
            self.store.put(
                ArtifactWrite(
                    run_id=run.id,
                    kind="final_result",
                    role=ArtifactRole.COMPLETION_REQUIRED,
                    visibility=ArtifactVisibility.USER,
                    filename="final_result.json",
                    media_type="application/json",
                    schema_version=1,
                    content=(json.dumps(payload) + "\n").encode(),
                )
            ),
            self.store.put(
                ArtifactWrite(
                    run_id=run.id,
                    kind="export_json",
                    role=ArtifactRole.COMPLETION_REQUIRED,
                    visibility=ArtifactVisibility.USER,
                    filename="export.json",
                    media_type="application/json",
                    schema_version=1,
                    content=(json.dumps(payload, sort_keys=True) + "\n").encode(),
                )
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
        )
        completed = self.uow.complete_run(lease, [], result, artifacts)
        self.clock.advance()
        return completed

    def _seed_results(self):
        records = [
            _record(1, title="Alpha study", score=9, abstract="has abstract", doi="10.1/a", citation_count=30, publish_year=2020),
            _record(2, title="Beta study", score=8, doi="10.1/b", citation_count=10, publish_year=2022),
            _record(3, title="Gamma study", score=None, abstract="has abstract", citation_count=99, publish_year=2019),
            _record(4, title="Delta study", score=7.5, pdf_url="https://pdf.example/d", citation_count=5, publish_year=2024),
            _record(5, title="Epsilon study", score=0, doi="10.1/e", citation_count=1, publish_year=2021),
            _record(6, title="Transformer survey", score=None, keywords_text="transformer; attention", citation_count=50, publish_year=2023),
        ]
        return self._complete(records, question="Result query seed")

    def test_summary_counts_cover_the_complete_set(self) -> None:
        run = self._seed_results()
        page = self.query_service.get_result_page(run.id, page_size=2)
        self.assertEqual(page.total_count, 6)
        self.assertEqual(page.matched_count, 6)
        self.assertEqual(page.scored_count, 4)
        self.assertEqual(page.max_score, 9)
        self.assertEqual([item.rank for item in page.results], [1, 2])
        self.assertIsNotNone(page.next_cursor)

    def test_text_search_matches_beyond_the_first_page(self) -> None:
        run = self._seed_results()
        page = self.query_service.get_result_page(run.id, page_size=2, query="transformer")
        self.assertEqual(page.matched_count, 1)
        self.assertEqual([item.record.title for item in page.results], ["Transformer survey"])
        self.assertIsNone(page.next_cursor)

    def test_min_score_excludes_unscored_but_keeps_legal_zero(self) -> None:
        run = self._seed_results()
        page = self.query_service.get_result_page(run.id, min_score=0)
        self.assertEqual([item.record.title for item in page.results],
                         ["Alpha study", "Beta study", "Delta study", "Epsilon study"])
        page = self.query_service.get_result_page(run.id, min_score=8)
        self.assertEqual([item.record.title for item in page.results], ["Alpha study", "Beta study"])

    def test_metadata_filters(self) -> None:
        run = self._seed_results()
        self.assertEqual(
            [item.record.title for item in self.query_service.get_result_page(run.id, metadata="abstract").results],
            ["Alpha study", "Gamma study"],
        )
        self.assertEqual(
            [item.record.title for item in self.query_service.get_result_page(run.id, metadata="doi").results],
            ["Alpha study", "Beta study", "Epsilon study"],
        )
        self.assertEqual(
            [item.record.title for item in self.query_service.get_result_page(run.id, metadata="pdf").results],
            ["Delta study"],
        )

    def test_sort_orders_with_unscored_last_and_stable_ties(self) -> None:
        run = self._seed_results()
        by_score = self.query_service.get_result_page(run.id, sort="score")
        self.assertEqual(
            [(item.record.title, item.record.score) for item in by_score.results],
            [
                ("Alpha study", 9),
                ("Beta study", 8),
                ("Delta study", 7.5),
                ("Epsilon study", 0),
                ("Gamma study", None),
                ("Transformer survey", None),
            ],
        )
        by_citations = self.query_service.get_result_page(run.id, sort="citations")
        self.assertEqual([item.record.title for item in by_citations.results][:2],
                         ["Gamma study", "Transformer survey"])
        by_year = self.query_service.get_result_page(run.id, sort="year")
        self.assertEqual([item.record.title for item in by_year.results][:2],
                         ["Delta study", "Transformer survey"])

    def test_cursor_roundtrip_and_filter_mismatch_rejection(self) -> None:
        run = self._seed_results()
        first = self.query_service.get_result_page(run.id, page_size=2, sort="score")
        second = self.query_service.get_result_page(run.id, cursor=first.next_cursor, page_size=2, sort="score")
        self.assertEqual([item.record.title for item in second.results], ["Delta study", "Epsilon study"])
        with self.assertRaises(InvalidCursor):
            self.query_service.get_result_page(run.id, cursor=first.next_cursor, page_size=2, sort="citations")

    def test_invalid_query_parameters_are_rejected(self) -> None:
        run = self._seed_results()
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_result_page(run.id, sort="unknown")
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_result_page(run.id, metadata="unknown")
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_result_page(run.id, min_score=11)

    def test_api_exposes_filters_summary_and_validation(self) -> None:
        run = self._seed_results()
        client = TestClient(create_app(self.database))
        response = client.get(f"/api/v1/runs/{run.id}/results", params={"min_score": 8})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["matched_count"], 2)
        self.assertEqual(payload["total_count"], 6)
        self.assertEqual(payload["scored_count"], 4)
        self.assertEqual(payload["max_score"], 9)
        bad = client.get(f"/api/v1/runs/{run.id}/results", params={"sort": "unknown"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "invalid_result_query")

    def test_history_search_covers_all_pages(self) -> None:
        for index in range(5):
            self._complete([_record(1)], question=f"Common topic run {index}")
        self._complete([_record(1)], question="Needle in the history haystack")
        page = self.query_service.get_history_page(page_size=2, query="needle")
        self.assertEqual(page.matched_count, 1)
        self.assertEqual([item.question for item in page.items], ["Needle in the history haystack"])
        common = self.query_service.get_history_page(page_size=2, query="common")
        self.assertEqual(common.matched_count, 5)
        self.assertIsNotNone(common.next_cursor)
        rest = self.query_service.get_history_page(cursor=common.next_cursor, page_size=2, query="common")
        self.assertEqual(len(rest.items), 2)
        with self.assertRaises(InvalidCursor):
            self.query_service.get_history_page(cursor=common.next_cursor, page_size=2, query="other")
        unfiltered = self.query_service.get_history_page(page_size=10)
        self.assertEqual(len(unfiltered.items), 6)
        self.assertEqual(unfiltered.matched_count, 0)

    def test_history_api_search(self) -> None:
        self._complete([_record(1)], question="Visible through the API")
        client = TestClient(create_app(self.database))
        response = client.get("/api/v1/history", params={"query": "visible"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["matched_count"], 1)
        self.assertEqual(payload["items"][0]["question"], "Visible through the API")

    def test_pagination_only_materializes_requested_result_models(self) -> None:
        run = self._complete([_record(index, score=0 if index % 2 else None) for index in range(1, 257)])
        with patch.object(FinalPaperResult, "model_validate", wraps=FinalPaperResult.model_validate) as decode:
            first = self.query_service.get_result_page(run.id, page_size=7, sort="score")
            second = self.query_service.get_result_page(run.id, cursor=first.next_cursor, page_size=7, sort="score")
        self.assertEqual(decode.call_count, 14)
        self.assertEqual(first.total_count, 256)
        self.assertEqual(first.scored_count, 128)
        self.assertEqual(first.max_score, 0)
        self.assertEqual(first.high_score_count, 0)
        self.assertEqual([item.rank for item in second.results], list(range(15, 29, 2)))

    def test_database_filters_preserve_unicode_and_literal_search(self) -> None:
        run = self._complete([
            _record(1, title="Straße 研究 100%_", abstract="\u2003", score=0),
            _record(2, title="STRASSE", abstract="Evidence", score=None),
            _record(3, title="Strasse", abstract="Evidence", score=8),
        ])
        page = self.query_service.get_result_page(run.id, query=" STRASSE ", page_size=1, sort="score")
        self.assertEqual(page.matched_count, 3)
        self.assertEqual([item.rank for item in page.results], [3])
        literal = self.query_service.get_result_page(run.id, query="100%_")
        self.assertEqual([item.rank for item in literal.results], [1])
        filtered = self.query_service.get_result_page(run.id, query="strasse", metadata="abstract", min_score=0)
        self.assertEqual([item.rank for item in filtered.results], [3])
        self.assertEqual(filtered.total_count, 3)
        self.assertEqual(filtered.scored_count, 2)

    def test_history_database_search_is_unicode_literal_and_excludes_deleted(self) -> None:
        first = self._complete([_record(1)], question="Straße 研究 100%_")
        self._complete([_record(1)], question="STRASSE research")
        page = self.query_service.get_history_page(query="strasse", page_size=1)
        self.assertEqual(page.matched_count, 2)
        second = self.query_service.get_history_page(query="strasse", page_size=1, cursor=page.next_cursor)
        self.assertEqual(second.items[0].id, first.id)
        literal = self.query_service.get_history_page(query="100%_")
        self.assertEqual(literal.matched_count, 1)
        self.uow.stage_run_deletion(first.id)
        self.assertEqual(self.query_service.get_history_page(query="研究").matched_count, 0)

    def test_batched_store_reader_matches_database_queries(self) -> None:
        from paperseek_app.read_queries import query_results, search_history

        run = self._complete([
            _record(index, title=f"Straße {index}", score=None if index % 3 else index % 11,
                    citation_count=index % 7, publish_year=2020 + index % 4)
            for index in range(1, 257)
        ], question="Straße 研究")
        for sort in ("rank", "score", "citations", "year"):
            options = dict(offset=7, limit=9, query="STRASSE", min_score=0, metadata=None, sort=sort)
            with self.subTest(sort=sort), patch.object(self.uow, "get_results", wraps=self.uow.get_results) as reads:
                portable = query_results(self.uow, run.id, **options)
                native = self.uow.query_results(run.id, **options)
                self.assertEqual(portable, native)
                self.assertEqual([call.kwargs["limit"] for call in reads.call_args_list], [200, 200])
        options = dict(query="strasse", offset=0, limit=1)
        self.assertEqual(search_history(self.uow, **options), self.uow.search_history(**options))

    def test_high_score_count_uses_full_set_threshold(self) -> None:
        run = self._seed_results()
        page = self.query_service.get_result_page(run.id, page_size=1)
        self.assertEqual(page.high_score_count, 3)
        client = TestClient(create_app(self.database))
        payload = client.get(f"/api/v1/runs/{run.id}/results").json()
        self.assertEqual(payload["high_score_count"], 3)

    def test_selected_export_csv_and_json_use_full_records(self) -> None:
        run = self._seed_results()
        csv_content = self.query_service.get_selected_export(run.id, "csv", ["paper-1", "paper-5"])
        text = csv_content.content.decode("utf-8")
        self.assertIn("Alpha study", text)
        self.assertIn("Epsilon study", text)
        self.assertNotIn("Beta study", text)
        self.assertEqual(csv_content.filename, "export-selected.csv")
        json_content = self.query_service.get_selected_export(run.id, "json", ["paper-6"])
        payload = json.loads(json_content.content.decode("utf-8"))
        self.assertEqual([item["title"] for item in payload["ranked"]], ["Transformer survey"])
        self.assertIsNone(payload["ranked"][0]["score"])

    def test_journal_and_acceptance_filters_agree_in_database_and_batched_reader(self) -> None:
        from paperseek_app.read_queries import query_results

        run = self._complete([
            _record(1, title="Top journal", journal_metrics={"cas": {"tier": 1, "top": True}, "jcr": {"quartile_number": 1, "impact_factor": 40.0}}),
            _record(2, title="Mid journal", journal_metrics={"cas": {"tier": 3, "top": False}, "jcr": {"quartile_number": 2, "impact_factor": 3.1}}),
            _record(3, title="Oral paper", acceptance={"venue": "ICLR", "year": 2025, "tier": "Oral"},
                    journal_metrics={"ccf": {"rank": "A", "rank_number": 1}}),
            _record(4, title="Poster paper", acceptance={"venue": "NeurIPS", "year": 2024, "tier": "Poster"}),
            _record(5, title="Unlabelled"),
        ], question="Venue filters")
        cases = {
            (("journal", "castop"),): ["Top journal"], (("journal", "jcr2"),): ["Top journal", "Mid journal"],
            (("journal", "cas2"),): ["Top journal"], (("journal", "ccfa"),): ["Oral paper"],
            (("journal", "ranked"),): ["Top journal", "Mid journal", "Oral paper"],
            (("acceptance", "oral"),): ["Oral paper"], (("acceptance", "accepted"),): ["Oral paper", "Poster paper"],
            (("sort", "impact"),): ["Top journal", "Mid journal", "Oral paper", "Poster paper", "Unlabelled"],
        }
        for filters, titles in cases.items():
            options = dict(offset=0, limit=10, query=None, min_score=None, metadata=None, sort="rank") | dict(filters)
            with self.subTest(filters=filters):
                native = self.uow.query_results(run.id, **options)
                self.assertEqual([item.record.title for item in native.results], titles)
                self.assertEqual(query_results(self.uow, run.id, **options), native)
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_result_page(run.id, journal="cas9")

    def test_selected_export_bibtex_and_ris_for_reference_managers(self) -> None:
        run = self._complete([
            _record(1, title="Graph {Learning} & Retrieval", authors=["Ada Lovelace", "Grace Hopper"], year=2024,
                    venue="ICLR", document_types=["conference-paper"], doi="10.1/x_y", pages="1-9",
                    acceptance={"venue": "ICLR", "year": 2024, "tier": "Oral", "label": "ICLR 2024 Oral"}),
            _record(2, title="Journal study", authors=["Ada Lovelace"], year=2024, venue="Nature",
                    document_types=["journal-article"]),
        ], question="Reference exports")
        bibtex = self.query_service.get_selected_export(run.id, "bibtex", ["paper-1", "paper-2"])
        self.assertEqual(bibtex.filename, "export-selected.bib")
        self.assertEqual(bibtex.content.decode(), (
            "@inproceedings{lovelace2024graph,\n  title = {Graph \\{Learning\\} \\& Retrieval},\n"
            "  author = {Ada Lovelace and Grace Hopper},\n  year = {2024},\n  booktitle = {ICLR},\n  pages = {1--9},\n"
            "  doi = {10.1/x_y},\n  note = {ICLR 2024 Oral}\n}\n\n"
            "@article{lovelace2024journal,\n  title = {Journal study},\n  author = {Ada Lovelace},\n  year = {2024},\n"
            "  journal = {Nature}\n}\n"
        ))
        ris = self.query_service.get_selected_export(run.id, "ris", ["paper-2"]).content.decode()
        self.assertEqual(ris, "TY  - JOUR\r\nTI  - Journal study\r\nAU  - Ada Lovelace\r\nPY  - 2024\r\nT2  - Nature\r\nER  - \r\n")

    def test_selected_export_rejects_empty_or_foreign_selection(self) -> None:
        run = self._seed_results()
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_selected_export(run.id, "csv", [])
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_selected_export(run.id, "csv", ["not-in-this-run"])
        with self.assertRaises(InvalidResultQuery):
            self.query_service.get_selected_export(run.id, "xml", ["paper-1"])
        client = TestClient(create_app(self.database))
        response = client.post(
            f"/api/v1/runs/{run.id}/export-selected",
            json={"format": "csv", "uids": ["paper-1"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        bad = client.post(
            f"/api/v1/runs/{run.id}/export-selected",
            json={"format": "csv", "uids": []},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "invalid_result_query")


if __name__ == "__main__":
    unittest.main()
