"""ShowJCR tables label papers: CAS and JCR by ISSN then journal title, CCF by venue name or abbreviation."""

from __future__ import annotations

import asyncio
import tempfile
import unittest

import httpx

from paperseek_community.journal_tables import JournalTableError, JournalTableStore
from paperseek_core.enrichment import ResultEnricher
from paperseek_core.journal_metrics import showjcr_url
from paperseek_core.models import PaperIdentifiers, PaperRecord, PaperSource

CAS = ("Journal,年份,ISSN/EISSN,Review,OA Journal Index（OAJ）,大类,大类分区,Top\n"
       "IEEE TRANSACTIONS ON PATTERN ANALYSIS AND MACHINE INTELLIGENCE,2025,0162-8828/1939-3539,否,否,计算机科学,1 [10/758],是\n"
       "NEUROCOMPUTING,2025,0925-2312/1872-8286,否,否,计算机科学,2 [127/758],否\n")
JCR = ("Journal,ISSN,EISSN,Web of Science,IF(2025),Category_1,IF Quartile(2025)_1,IF Rank(2025)_1,Category_2,IF Quartile(2025)_2,IF Rank(2025)_2\n"
       'IEEE TRANSACTIONS ON PATTERN ANALYSIS AND MACHINE INTELLIGENCE,0162-8828,1939-3539,SCIE,20.4,"COMPUTER SCIENCE, ARTIFICIAL INTELLIGENCE",Q1,4/210,"ENGINEERING, ELECTRICAL & ELECTRONIC",Q1,6/366\n'
       'NEUROCOMPUTING,0925-2312,1872-8286,SCIE,6.7,"COMPUTER SCIENCE, ARTIFICIAL INTELLIGENCE",Q2,43/210,,,\n')
CCF = ("刊物名称,Journal,年份,出版社,网址,领域,CCF推荐类别（国际学术刊物/会议）,CCF推荐类型\n"
       "TPAMI,IEEE Transactions on Pattern Analysis and Machine Intelligence,2026,IEEE,,人工智能,推荐国际学术刊物,A类\n"
       "NeurIPS,Conference on Neural Information Processing Systems,2026,MIT Press,,人工智能,推荐国际学术会议,A类\n"
       "ICLR,International Conference on Learning Representations,2026,OpenReview,,人工智能,推荐国际学术会议,A类\n")


class JournalMetricsContractTest(unittest.TestCase):
    def test_showjcr_tables_label_journals_and_conferences(self) -> None:
        bodies = {showjcr_url("cas"): CAS, showjcr_url("jcr"): JCR, showjcr_url("ccf"): CCF}
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=bodies[str(request.url)].encode("utf-8"))))
        papers = [
            PaperRecord(uid="tpami", title="A", source=PaperSource(source_title="IEEE TPAMI"),
                        identifiers=PaperIdentifiers(issn="19393539")),
            PaperRecord(uid="neuro", title="B", source=PaperSource(source_title="Neurocomputing")),
            PaperRecord(uid="neurips", title="C", source=PaperSource(source_title="Advances in Neural Information Processing Systems 37")),
            PaperRecord(uid="iclr", title="D", provider="paperhub", raw={"conference": "ICLR", "year": 2025, "presentation": "oral"}),
            PaperRecord(uid="workshop", title="E", source=PaperSource(source_title="NeurIPS 2024 Workshop on Efficient ML")),
        ]
        with tempfile.TemporaryDirectory() as folder:
            store = JournalTableStore(folder)
            labels = asyncio.run(ResultEnricher(journals=lambda: store.ensure(client)).annotate(papers))
            self.assertEqual([(row["platform"], row["journals"], row["year"], row["source"]) for row in store.status()],
                             [("cas", 2, 2025, "showjcr"), ("jcr", 2, 2025, "showjcr"), ("ccf", 3, 2026, "showjcr")])
            with self.assertRaises(JournalTableError):
                store.import_table("jcr", b"Journal,Category\nNature,Science\n", filename="bad.csv")

        tpami = labels["tpami"]["journal_metrics"]
        self.assertEqual((tpami["matched_by"], tpami["cas"]["tier"], tpami["cas"]["top"]), ("issn", 1, True))
        self.assertEqual((tpami["jcr"]["quartile"], tpami["jcr"]["impact_factor"], tpami["ccf"]["rank"]), ("Q1", 20.4, "A"))
        neuro = labels["neuro"]["journal_metrics"]
        self.assertEqual((neuro["matched_by"], neuro["cas"]["tier"], neuro["jcr"]["quartile"], "ccf" in neuro), ("title", 2, "Q2", False))
        self.assertEqual(labels["neurips"]["journal_metrics"]["ccf"]["abbreviation"], "NeurIPS")
        self.assertEqual(labels["iclr"]["journal_metrics"]["ccf"]["rank"], "A")
        self.assertEqual(labels["iclr"]["acceptance"]["label"], "ICLR 2025 Oral")
        self.assertNotIn("journal_metrics", labels.get("workshop", {}))


if __name__ == "__main__":
    unittest.main()
