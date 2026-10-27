"""Bibliometrics uses real saved inputs and never expands model scoring."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from fixtures.review_support import create_source
from sqlalchemy import text

from paperseek_community.api import create_app
from paperseek_community.persistence.sqlite_bibliometrics import SQLiteAnalysisStore
from paperseek_core.bibliometrics.corpus import build_corpus, merge_corpus, rank_corpus
from paperseek_core.bibliometrics.models import (
    MetricCorpus,
    MetricPaper,
    NetworkRequest,
    NetworkTooDense,
)
from paperseek_core.bibliometrics.networks import network
from paperseek_core.bibliometrics.statistics import overview
from paperseek_core.journal_metrics import JournalMetricsTable, parse_cas, parse_ccf, parse_jcr
from paperseek_core.models import PaperCitation, PaperRecord, PaperSource


class BibliometricAlgorithmsTest(unittest.TestCase):
    def corpus(self):
        return MetricCorpus(
            papers=[
                MetricPaper(
                    id="a",
                    title="A",
                    year=2020,
                    keywords=["topic a", "topic b"],
                    references=["ref:1", "ref:2"],
                    references_available=True,
                    citations=0,
                    scored=True,
                ),
                MetricPaper(
                    id="b",
                    title="B",
                    year=2021,
                    keywords=["topic a"],
                    references=["ref:1", "ref:3"],
                    references_available=True,
                    citations=4,
                ),
                MetricPaper(id="c", title="C", keywords=["topic b"], references=[]),
            ]
        )

    def test_missing_citations_are_not_zero_and_unscored_papers_are_counted(self):
        result = overview(self.corpus())
        self.assertEqual(result.paper_count, 3)
        self.assertEqual(result.scored_count, 1)
        self.assertEqual(result.citation_sum, 4)
        self.assertEqual(
            next(
                item.available for item in result.coverage if item.field == "citations"
            ),
            2,
        )
        self.assertEqual([item.label for item in result.years], ["2020", "2021"])

    def test_venue_rankings_are_counted_in_rank_order_and_by_venue(self):
        table = JournalMetricsTable({
            "cas": parse_cas("Journal,ISSN/EISSN,大类,大类分区,Top\nNEUROCOMPUTING,0925-2312/1872-8286,计算机科学,2 [127/758],否\n"),
            "jcr": parse_jcr("Journal,ISSN,EISSN,IF(2025),Category_1,IF Quartile(2025)_1,IF Rank(2025)_1\n"
                             "NEUROCOMPUTING,0925-2312,1872-8286,6.7,AI,Q1,43/210\n"),
            "ccf": parse_ccf("刊物名称,Journal,CCF推荐类别（国际学术刊物/会议）,CCF推荐类型\n"
                             ",Neurocomputing,推荐国际学术刊物,C类\nAAAI,AAAI Conference on Artificial Intelligence,推荐国际学术会议,A类\n"),
        })
        corpus = MetricCorpus(papers=[
            MetricPaper(id="a", title="A", venue="Neurocomputing", issns=["0925-2312"]),
            MetricPaper(id="b", title="B", venue="Proceedings of the AAAI Conference on Artificial Intelligence"),
            MetricPaper(id="c", title="C", venue="Neurocomputing"),
            MetricPaper(id="d", title="D", venue="Unlisted Letters"),
        ])
        result = overview(rank_corpus(corpus, table))
        self.assertEqual(result.ranked_paper_count, 3)
        self.assertEqual([(item.label, item.count) for item in result.ccf_ranks], [("A", 1), ("C", 2)])
        self.assertEqual([(item.label, item.count) for item in result.cas_tiers], [("2", 2)])
        self.assertEqual([(item.label, item.count) for item in result.jcr_quartiles], [("Q1", 2)])
        top = result.ranked_venues[0]
        self.assertEqual((top.label, top.count, top.cas_tier, top.impact_factor, top.ccf_rank), ("Neurocomputing", 2, 2, 6.7, "C"))
        self.assertEqual(next(item.available for item in result.coverage if item.field == "venue_rank"), 3)

    def test_keywords_full_count_each_paper_once(self):
        corpus = self.corpus().model_copy(
            update={
                "papers": [MetricPaper(id="a", title="A", keywords=["x", "x", "y"])]
            }
        )
        result = network(corpus, NetworkRequest(kind="keywords", min_occurrences=1))
        self.assertEqual([node.count for node in result.nodes], [1, 1])
        self.assertEqual(result.edges[0].weight, 1)
        self.assertEqual(result.edges[0].similarity, 1)

    def test_coupling_uses_shared_references_and_cosine_similarity(self):
        result = network(
            self.corpus(), NetworkRequest(kind="coupling", min_occurrences=1)
        )
        self.assertEqual(result.eligible_paper_count, 2)
        self.assertEqual(len(result.edges), 1)
        self.assertEqual(result.edges[0].weight, 1)
        self.assertEqual(result.edges[0].similarity, 0.5)
        self.assertEqual(result.nodes[0].group, result.nodes[1].group)

    def test_display_limit_does_not_change_computed_totals(self):
        corpus = MetricCorpus(
            papers=[
                MetricPaper(id=str(i), title=str(i), keywords=["common", f"term{i}"])
                for i in range(20)
            ]
        )
        result = network(
            corpus,
            NetworkRequest(
                kind="keywords", min_occurrences=1, max_nodes=10, max_edges=10
            ),
        )
        self.assertEqual(result.eligible_node_count, 21)
        self.assertEqual(result.total_edge_count, 20)
        self.assertEqual(len(result.nodes), 10)
        self.assertIn("network_display_limited", result.warnings)

    def test_dense_network_fails_instead_of_returning_partial_success(self):
        with patch("paperseek_core.bibliometrics.networks.MAX_PAIR_CONTRIBUTIONS", 0):
            with self.assertRaises(NetworkTooDense):
                network(
                    self.corpus(), NetworkRequest(kind="coupling", min_occurrences=1)
                )

    def test_reference_frequency_filter_is_disclosed(self):
        corpus = MetricCorpus(
            papers=[
                MetricPaper(id=str(i), title=str(i), references=["popular"])
                for i in range(3)
            ]
        )
        result = network(
            corpus,
            NetworkRequest(
                kind="coupling", min_occurrences=1, max_reference_frequency=2
            ),
        )
        self.assertEqual(result.excluded_reference_count, 1)
        self.assertIn("frequent_references_excluded", result.warnings)

    def test_native_metadata_is_compact_and_empty_reference_list_is_known(self):
        records = [
            PaperRecord(
                uid="https://openalex.org/W1",
                title="One",
                provider="openalex",
                source=PaperSource(publish_year=2020),
                raw={
                    "cited_by_count": 0,
                    "referenced_works": [],
                    "authorships": [
                        {
                            "institutions": [
                                {"display_name": "Institute", "country_code": "SG"}
                            ]
                        }
                    ],
                    "abstract_inverted_index": {"unneeded": [0]},
                },
            )
        ]
        corpus = build_corpus(records + records)
        self.assertEqual(len(corpus.papers), 1)
        self.assertEqual(corpus.papers[0].citations, 0)
        self.assertTrue(corpus.papers[0].references_available)
        self.assertEqual(corpus.papers[0].institutions, ["Institute"])
        self.assertNotIn("abstract_inverted_index", corpus.model_dump_json())

    def test_citation_alias_does_not_double_count_a_retrieved_paper(self):
        corpus = build_corpus(
            [PaperRecord(uid="first", title="Same paper", provider="fixture")]
        )
        result = merge_corpus(
            corpus,
            [PaperRecord(uid="second", title="Same PAPER", provider="fixture")],
            scored_ids={"uid:second"},
        )
        self.assertEqual(len(result.papers), 1)
        self.assertTrue(result.papers[0].scored)

    def test_native_missing_wos_counts_and_scholar_zero_are_distinct(self):
        records = [
            PaperRecord(
                uid="wos",
                title="Missing",
                provider="wos",
                raw={"citations": [{"db": "WOS"}]},
                citations=[PaperCitation(count=0)],
            ),
            PaperRecord(
                uid="scholar",
                title="Zero",
                provider="googlescholar",
                raw={"citedBy": {"total": 0}},
            ),
            PaperRecord(
                uid="oa",
                title="References",
                provider="openalex",
                raw={
                    "referenced_works": [None, "", "https://openalex.org/W12"],
                    "reference": [{"DOI": "doi:"}],
                },
            ),
        ]
        papers = build_corpus(records).papers
        self.assertIsNone(papers[0].citations)
        self.assertEqual(papers[1].citations, 0)
        self.assertEqual(papers[2].references, ["openalex:W12"])


class BibliometricApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = create_source(self.directory, 3)
        self.database = self.directory / "paperseek.db"
        self.store = SQLiteAnalysisStore(self.database)
        self.addCleanup(self.store.close)
        self.client = TestClient(create_app(self.database))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, key=None, **overrides):
        return self.client.post(
            "/api/v1/bibliometrics",
            headers={"Idempotency-Key": key or str(uuid4())},
            json={"source_run_id": self.source.id, **overrides},
        )

    def test_create_list_export_and_source_deletion_preserve_analysis(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        analysis = response.json()
        identity = analysis["id"]
        self.assertEqual(analysis["scope"], "retrieved_candidates")
        self.assertEqual(analysis["paper_count"], 3)
        page = self.client.get("/api/v1/bibliometrics").json()
        self.assertEqual(page["total_count"], 1)
        self.assertNotIn("corpus", page["items"][0])
        export = self.client.get(f"/api/v1/bibliometrics/{identity}/export?format=json")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(len(export.json()["corpus"]["papers"]), 3)
        removed = self.client.delete(f"/api/v1/history/{self.source.id}")
        self.assertIn(removed.status_code, (200, 202, 204), removed.text)
        self.assertEqual(
            self.client.get(f"/api/v1/bibliometrics/{identity}").status_code, 200
        )

    def test_idempotency_and_request_conflict(self):
        key = str(uuid4())
        first, second = self.create(key), self.create(key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.create(key, title="Different").status_code, 409)
        self.client.delete(f"/api/v1/bibliometrics/{first.json()['id']}")
        self.assertEqual(self.create(key).status_code, 409)

    def test_search_display_name_preserves_input_and_is_searchable(self):
        original = self.client.get(f"/api/v1/runs/{self.source.id}").json()
        response = self.client.patch(
            f"/api/v1/history/{self.source.id}", json={"title": "Renamed 检索"}
        )
        self.assertEqual(response.status_code, 204, response.text)
        current = self.client.get(f"/api/v1/runs/{self.source.id}").json()
        self.assertEqual(current["question"], original["question"])
        self.assertEqual(current["updated_at"], original["updated_at"])
        self.assertEqual(
            self.client.get("/api/v1/history?query=renamed").json()["items"][0][
                "display_title"
            ],
            "Renamed 检索",
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/history/{uuid4()}", json={"title": "Unknown"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/history/{self.source.id}", json={"title": "   "}
            ).status_code,
            422,
        )

    def test_review_display_name_preserves_topic_version_and_input(self):
        response = self.client.post(
            "/api/v1/reviews",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "source_run_id": self.source.id,
                "paper_uids": ["fixed-001", "fixed-002"],
                "topic": "Original topic",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        original = response.json()
        self.assertEqual(
            self.client.patch(
                f"/api/v1/reviews/{original['id']}", json={"title": "Renamed 综述"}
            ).status_code,
            204,
        )
        current = self.client.get(f"/api/v1/reviews/{original['id']}").json()
        for key in ("content", "version", "updated_at"):
            self.assertEqual(current[key], original[key])
        page = self.client.get("/api/v1/reviews?query=renamed").json()
        self.assertEqual(page["items"][0]["display_title"], "Renamed 综述")
        self.assertEqual(
            self.client.get("/api/v1/reviews?query=no-match").json()["total_count"], 0
        )
        self.assertIsNotNone(current["content"]["citation_map"])
        self.client.delete(f"/api/v1/history/{self.source.id}")
        retained = self.client.get(f"/api/v1/reviews/{original['id']}").json()
        self.assertEqual(retained["content"], current["content"])

    def test_rename_search_and_delete_only_own_files(self):
        identity = self.create().json()["id"]
        renamed = self.client.patch(
            f"/api/v1/bibliometrics/{identity}", json={"title": "计量 Beta"}
        )
        self.assertEqual(renamed.status_code, 204)
        self.assertEqual(
            self.client.get("/api/v1/bibliometrics?q=BETA").json()["total_count"], 1
        )
        record = self.store.get_analysis(identity)
        self.assertTrue((self.directory / record.corpus.storage_key).is_file())
        self.assertEqual(
            self.client.delete(f"/api/v1/bibliometrics/{identity}").status_code, 204
        )
        self.assertFalse((self.directory / record.corpus.storage_key).exists())
        self.assertEqual(
            self.client.get(f"/api/v1/bibliometrics/{identity}").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/v1/runs/{self.source.id}").status_code, 200
        )

    def test_network_is_on_demand_and_cached(self):
        identity = self.create().json()["id"]
        self.assertEqual(self.store.get_analysis(identity).networks, {})
        url = f"/api/v1/bibliometrics/{identity}/network"
        response = self.client.post(
            url, json={"kind": "keywords", "min_occurrences": 1}
        )
        self.assertEqual(response.status_code, 200, response.text)
        with patch(
            "paperseek_app.bibliometrics.services.network",
            side_effect=AssertionError("cache was ignored"),
        ):
            self.assertEqual(
                self.client.post(
                    url, json={"kind": "keywords", "min_occurrences": 1}
                ).json(),
                response.json(),
            )

    def test_old_history_explicitly_reports_scored_subset(self):
        with self.store.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM run_artifacts WHERE run_id=:id AND kind='bibliometric_corpus'"
                ),
                {"id": self.source.id},
            )
        result = self.create()
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["scope"], "legacy_ranked_only")
        self.assertIn("legacy_ranked_only", result.json()["warnings"])

    def test_unknown_source_and_invalid_requests(self):
        self.assertEqual(self.create(source_run_id=str(uuid4())).status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/v1/bibliometrics", json={"source_run_id": self.source.id}
            ).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
