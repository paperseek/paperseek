from __future__ import annotations

import json
import unittest
from pathlib import Path

from paperseek_core import query_compiler, query_parsing
from paperseek_core.query_parsing import parse_search_plan

PARSING_QUESTION = "Compare transparent AI systems without robotics."


class QueryParsingContract(unittest.TestCase):
    def test_search_plan_accepts_fences_embedded_braces_and_filters_machine_fields(self) -> None:
        payload = {
            "intent": "Transparent {AI} comparison",
            "question_type": "unknown-machine-value",
            "unknown_plan_field": "discarded",
            "query_views": [
                {
                    "name": "Direct View",
                    "purpose": 'Compare "audit {evidence}"',
                    "concept_groups": [
                        {
                            "label": "Transparency",
                            "terms": ["transparent AI"],
                            "required": True,
                            "unknown_group_field": "discarded",
                        }
                    ],
                    "exclusions": ["robotics", "healthcare"],
                    "unknown_view_field": "discarded",
                },
                "discard this non-object",
            ],
        }
        encoded = json.dumps(payload)
        raw = f"preface\n{encoded}\ntrailing prose"
        fenced = f"```json\n{encoded}\n```"

        plan = query_parsing.parse_search_plan(raw, PARSING_QUESTION)
        fenced_plan = query_parsing.parse_search_plan(fenced, PARSING_QUESTION)

        self.assertIsNotNone(plan)
        self.assertIsNotNone(fenced_plan)
        assert plan is not None
        assert fenced_plan is not None
        self.assertEqual(fenced_plan, plan)
        self.assertEqual(plan.intent, "Transparent {AI} comparison")
        self.assertEqual(plan.question_type, "comparison")
        self.assertEqual(len(plan.query_views), 1)
        self.assertEqual(plan.query_views[0].view_id, "direct_view")
        self.assertEqual(plan.query_views[0].exclusions, ["robotics"])
        self.assertEqual(plan.query_views[0].concept_groups[0].terms, ["transparent AI"])

    def test_invalid_search_plan_shapes_remain_none(self) -> None:
        for raw in ("", "[]", "prefix {not-json} suffix", 'prefix {"intent": "open"'):
            with self.subTest(raw=raw):
                self.assertIsNone(query_parsing.parse_search_plan(raw, PARSING_QUESTION))

    def test_query_proposal_aliases_nested_values_and_rationale_order_are_stable(self) -> None:
        raw = (
            "model note before "
            + json.dumps(
                {
                    "search_query": {"query": 'all:"audit {evidence}"'},
                    "rationale": ["first", "second"],
                    "adjustment": "tighten scope",
                }
            )
            + " model note after"
        )

        self.assertEqual(
            query_parsing.parse_query_proposal(raw),
            ('all:"audit {evidence}"', "first; second\ntighten scope"),
        )
        for invalid in ("", "[]", "```json\n{broken}\n```"):
            with self.subTest(invalid=invalid):
                self.assertEqual(query_parsing.parse_query_proposal(invalid), ("", ""))


ROOT = Path(__file__).resolve().parents[3]


QUESTION = "How do transparent AI decision systems affect accountability in public agencies?"


class QueryValidationCompilerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = parse_search_plan(
            (ROOT / "tests" / "fixtures" / "llm" / "intent_search_plan.json").read_text(
                encoding="utf-8"
            ),
            QUESTION,
        )
        assert cls.plan is not None

    def test_eight_source_compilers_remain_exact(self) -> None:
        expected = {
            "arxiv": '(all:"AI decision systems" OR all:"automated decision systems") AND (all:accountability OR all:"algorithmic accountability")',
            "crossref": "AI decision systems automated decision systems accountability algorithmic accountability",
            "googlescholar": '("AI decision systems" OR "automated decision systems") (accountability OR "algorithmic accountability")',
            "openalex": '("AI decision systems" OR "automated decision systems") AND (accountability OR "algorithmic accountability")',
            "paperhub": '"AI decision systems" OR "automated decision systems" accountability OR "algorithmic accountability"',
            "pubmed": '("AI decision systems"[Title/Abstract] OR "automated decision systems"[Title/Abstract]) AND (accountability[Title/Abstract] OR "algorithmic accountability"[Title/Abstract])',
            "semanticscholar": "AI decision systems automated decision systems accountability algorithmic accountability",
            "wos": 'TS=(("AI decision systems" OR "automated decision systems") AND (accountability OR "algorithmic accountability"))',
        }

        for source, query in expected.items():
            with self.subTest(source=source):
                compiled = query_compiler.compile_query_view(source, self.plan.query_views[0])
                self.assertEqual(compiled.query, query)
                self.assertTrue(query_compiler.validate_source_query(source, query).ok)

    def test_validation_failure_codes_and_openalex_length_boundary_remain_exact(self) -> None:
        cases = (
            ("openalex", "", "missing query field in JSON response"),
            ("unknown", "query", "unsupported data source: unknown"),
            (
                "openalex",
                '"AI governance" AND (accountability OR',
                "unbalanced quotation marks, parentheses, or trailing Boolean operator",
            ),
            (
                "arxiv",
                "all:test\nall:second",
                "query contains control characters or multiple lines",
            ),
            (
                "openalex",
                "search=query&page=1",
                "query must contain only the source query value, not a URL or API parameter",
            ),
            (
                "crossref",
                "governance AND accountability",
                "crossref relevance search requires plain text, not Boolean operators",
            ),
            (
                "googlescholar",
                "governance AND accountability",
                "googlescholar uses implicit AND with OR groups and minus exclusions",
            ),
            (
                "openalex",
                "governance AND accountability OR transparency",
                "OpenAlex mixed top-level AND/OR requires each OR synonym group to be parenthesized",
            ),
            (
                "openalex",
                "governance*",
                "OpenAlex search= does not accept wildcard syntax; use complete terms",
            ),
            (
                "arxiv",
                "title:governance",
                "arXiv field prefixes must use all:, ti:, abs:, au:, cat:, id:, jr:, co:, or rn:",
            ),
            (
                "pubmed",
                "all:governance",
                "PubMed fields must use bracketed tags such as [Title/Abstract]",
            ),
        )
        for source, query, issue in cases:
            with self.subTest(source=source, issue=issue):
                result = query_compiler.validate_source_query(source, query)
                self.assertFalse(result.ok)
                self.assertEqual(result.issue, issue)

        long_query = " ".join(f"term{index}" for index in range(600))
        result = query_compiler.validate_source_query("openalex", long_query)
        self.assertFalse(result.ok)
        self.assertEqual(
            result.issue,
            "OpenAlex search query is too long for a safe request URL; use fewer established terms or split the search into query views",
        )

    def test_source_safe_normalization_remains_exact(self) -> None:
        self.assertEqual(
            query_compiler.source_safe_query("semanticscholar", "cross-modal retrieval"),
            "cross modal retrieval",
        )
        self.assertEqual(
            query_compiler.source_safe_query(
                "openalex",
                "search = (AI* AND accountability) AND (AI* AND accountability) filter=x",
            ),
            "(AI AND accountability)",
        )
        self.assertEqual(query_compiler.source_safe_query("arxiv", " all:test "), "all:test")
