from __future__ import annotations

import unittest

from paperseek_core import ranking, retrieval
from paperseek_core.document_identity import document_alias_keys
from paperseek_core.models import PaperIdentifiers, PaperRecord


def record(
    *,
    uid: str = "",
    title: str = "",
    doi: str = "",
    openalex: str = "",
    arxiv: str = "",
    semanticscholar: str = "",
    pmid: str = "",
) -> PaperRecord:
    return PaperRecord(
        uid=uid,
        title=title,
        identifiers=PaperIdentifiers(
            doi=doi,
            openalex=openalex,
            arxiv=arxiv,
            semanticscholar=semanticscholar,
            pmid=pmid,
        ),
    )


class DocumentIdentityContract(unittest.TestCase):
    def test_identifier_priority_and_frozen_normalization_are_shared(self) -> None:
        cases = (
            (
                record(
                    uid="ignored",
                    doi=" HTTPS://DOI.ORG/10.1234/Shared ",
                    openalex="W-ignored",
                ),
                "doi:10.1234/shared",
            ),
            (record(uid="ignored", openalex=" W 123 "), "openalex:w123"),
            (record(uid="ignored", arxiv=" 2401.01234V2 "), "arxiv:2401.01234v2"),
            (
                record(uid="ignored", semanticscholar=" Corpus ID: 42 "),
                "semanticscholar:corpusid:42",
            ),
            (record(uid="ignored", pmid=" 123 456 "), "pmid:123456"),
            (record(uid=" Provider:ABC "), "uid:provider:abc"),
            (record(title="  A   Shared\tTitle  "), "title:a shared title"),
            (record(), ""),
        )

        for document, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(retrieval.document_key(document), expected)
                self.assertEqual(ranking.document_key(document), expected)

    def test_alias_and_ranking_identity_semantics_remain_distinct_and_stable(self) -> None:
        document = record(
            uid="  Provider:Visible UID  ",
            title="  Shared   Title ",
            doi="doi:10.1234/SHARED",
        )

        self.assertEqual(
            document_alias_keys(document),
            ("doi:10.1234/shared", "title:shared title"),
        )
        self.assertEqual(ranking.document_identity(document), "Provider:Visible UID")


if __name__ == "__main__":
    unittest.main()
