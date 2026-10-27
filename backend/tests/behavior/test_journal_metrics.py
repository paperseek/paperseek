"""Ranking tables label papers: CAS and JCR by ISSN then journal title, CCF by venue name or abbreviation,
北大核心 and CSSCI by Chinese title."""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile

import httpx

from paperseek_community.journal_tables import JournalTableError, JournalTableStore
from paperseek_core.enrichment import ResultEnricher
from paperseek_core.journal_metrics import CN_CORE_URL, matches_filter, showjcr_url
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
# 刊名, 收录, 曾用名 without a header row, as shared strings like the published sheet.
CN_CORE_ROWS = [("安徽大学学报（哲学社会科学版）", "CSSCI来源期刊（2025-2026）；北大中文核心（2023版）", ""),
                ("阿拉伯世界研究", "CSSCI扩展版（2025-2026）", ""),
                ("上海体育大学学报", "北大中文核心（2023版）", "上海体育学院学报")]
STALE_ROWS = [("安徽大学学报（哲学社会科学版）", "北大中文核心（2020版）", "")]


def _xlsx(first: list[tuple[str, ...]], later: list[tuple[str, ...]]) -> bytes:
    """A workbook whose first sheet is stored as sheet2.xml, as after moving a new sheet to the front."""
    strings = sorted({value for row in (*first, *later) for value in row if value})
    namespace = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    relationships = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

    def sheet(rows: list[tuple[str, ...]]) -> str:
        cells = "".join(f'<row r="{line}">' + "".join(f'<c r="{"ABC"[column]}{line}" t="s"><v>{strings.index(value)}</v></c>'
                                                      for column, value in enumerate(row) if value) + "</row>"
                        for line, row in enumerate(rows, 1))
        return f"<worksheet {namespace}><sheetData>{cells}</sheetData></worksheet>"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("xl/workbook.xml", f'<workbook {namespace} {relationships}><sheets><sheet name="新" sheetId="2" r:id="rId2"/>'
                                         '<sheet name="旧" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                      '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/sheet2.xml"/>'
                      '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                      "</Relationships>")
        book.writestr("xl/sharedStrings.xml", f"<sst {namespace}>" + "".join(f"<si><t>{value}</t></si>" for value in strings) + "</sst>")
        book.writestr("xl/worksheets/sheet1.xml", sheet(later))
        book.writestr("xl/worksheets/sheet2.xml", sheet(first))
    return buffer.getvalue()


class JournalMetricsContractTest(unittest.TestCase):
    def test_showjcr_tables_label_journals_and_conferences(self) -> None:
        bodies = {showjcr_url("cas"): CAS.encode(), showjcr_url("jcr"): JCR.encode(), showjcr_url("ccf"): CCF.encode(),
                  CN_CORE_URL: _xlsx(CN_CORE_ROWS, STALE_ROWS)}
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=bodies[str(request.url)])))
        papers = [
            PaperRecord(uid="tpami", title="A", source=PaperSource(source_title="IEEE TPAMI"),
                        identifiers=PaperIdentifiers(issn="19393539")),
            PaperRecord(uid="neuro", title="B", source=PaperSource(source_title="Neurocomputing")),
            PaperRecord(uid="neurips", title="C", source=PaperSource(source_title="Advances in Neural Information Processing Systems 37")),
            PaperRecord(uid="iclr", title="D", provider="paperhub", raw={"conference": "ICLR", "year": 2025, "presentation": "oral"}),
            PaperRecord(uid="workshop", title="E", source=PaperSource(source_title="NeurIPS 2024 Workshop on Efficient ML")),
            PaperRecord(uid="anhui", title="F", source=PaperSource(source_title="安徽大学学报(哲学社会科学版)")),
            PaperRecord(uid="arab", title="G", source=PaperSource(source_title="阿拉伯世界研究")),
            PaperRecord(uid="renamed", title="H", source=PaperSource(source_title="上海体育学院学报")),
        ]
        with tempfile.TemporaryDirectory() as folder:
            store = JournalTableStore(folder)
            labels = asyncio.run(ResultEnricher(journals=lambda: store.ensure(client)).annotate(papers))
            self.assertEqual([(row["platform"], row["journals"], row["year"], row["source"]) for row in store.status()],
                             [("cas", 2, 2025, "showjcr"), ("jcr", 2, 2025, "showjcr"), ("ccf", 3, 2026, "showjcr"),
                              ("cn_core", 3, None, "paperseek")])
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
        # Full-width and half-width brackets name the same journal; 扩展版 is not a CSSCI source journal.
        anhui = labels["anhui"]["journal_metrics"]["cn_core"]
        self.assertEqual((anhui["pku_edition"], anhui["cssci_edition"]), ("2023", "2025-2026"))
        self.assertEqual([name for name in ("pku", "cssci", "cssci_c", "cssci_e") if matches_filter(labels["anhui"]["journal_metrics"], name)],
                         ["pku", "cssci"])
        self.assertEqual([name for name in ("pku", "cssci", "cssci_e") if matches_filter(labels["arab"]["journal_metrics"], name)],
                         ["cssci_e"])
        self.assertTrue(labels["renamed"]["journal_metrics"]["cn_core"]["pku"])


if __name__ == "__main__":
    unittest.main()
