"""Structured LLM output parsing for query planning."""

from __future__ import annotations

import json
import re
from typing import Any

from paperseek_core.models import ConceptGroup, QueryView, SearchPlan


QUERY_FIELD_ALIASES = (
    "query",
    "search_query",
    "query_string",
    "source_query",
    "term",
    "wos_query",
)
RATIONALE_FIELD_ALIASES = (
    "rationale",
    "adjustment",
    "adjustment_direction",
    "reason",
    "diagnostic_note",
)


def _strip_llm_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw


def _extract_json_object_text(text: str) -> str:
    raw = _strip_llm_fence(text)
    if not raw:
        return ""
    if raw.startswith("{") and raw.endswith("}"):
        return raw
    start = raw.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(raw[start:], start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return ""


def normalize_question_type(value: Any, question: str) -> str:
    normalized = str(value or "").strip().lower()
    allowed = {
        "broad_landscape",
        "specific_relation",
        "method_application",
        "mechanism",
        "comparison",
        "seminal_work",
        "recent_progress",
        "systematic_search",
    }
    if normalized in allowed:
        return normalized
    question_lower = str(question or "").lower()
    if any(
        marker in question_lower
        for marker in (
            "field of",
            "works related",
            "papers about",
            "research on",
            "literature on",
        )
    ):
        return "broad_landscape"
    if any(marker in question_lower for marker in ("latest", "recent", "state of the art")):
        return "recent_progress"
    if any(marker in question_lower for marker in ("compare", "comparison", "versus", " vs ")):
        return "comparison"
    if any(marker in question_lower for marker in ("method", "use of", "application", "applied")):
        return "method_application"
    return "specific_relation"


def _explicit_user_exclusion(question: str, exclusion: str) -> bool:
    question_text = re.sub(r"\s+", " ", str(question or "")).strip().casefold()
    exclusion_text = re.sub(r"\s+", " ", str(exclusion or "")).strip(
        " \t\r\n,;."
    ).casefold()
    if not question_text or not exclusion_text:
        return False
    negative_segments = re.findall(
        r"(?:\bexclude(?:d|s|ing)?\b|\bwithout\b|\bexcept(?: for)?\b|\bnot\b|不要|排除|不包括|不含)"
        r"\s*(?:about\s+|including\s+|include\s+)?([^?.;；。！？]+)",
        question_text,
    )
    return any(exclusion_text in segment for segment in negative_segments)


def parse_search_plan(text: str, question: str) -> SearchPlan | None:
    object_text = _extract_json_object_text(text)
    if not object_text:
        return None
    try:
        payload = json.loads(object_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    allowed_plan = set(SearchPlan.model_fields)
    normalized = {key: value for key, value in payload.items() if key in allowed_plan}
    normalized["intent"] = str(
        normalized.get("intent") or question or "Literature search"
    ).strip()
    normalized["question_type"] = normalize_question_type(
        normalized.get("question_type"), question
    )
    raw_views = normalized.get("query_views")
    views = []
    if isinstance(raw_views, list):
        for index, item in enumerate(raw_views):
            if not isinstance(item, dict):
                continue
            view = dict(item)
            view["view_id"] = (
                view.get("view_id")
                or view.get("id")
                or view.get("name")
                or f"view_{index + 1}"
            )
            view["concept_groups"] = view.get("concept_groups") or view.get("groups") or []
            exclusions = view.get("exclusions")
            if isinstance(exclusions, str):
                exclusions = [exclusions]
            view["exclusions"] = [
                str(value)
                for value in exclusions or []
                if _explicit_user_exclusion(question, str(value))
            ]
            allowed_view = set(QueryView.model_fields) | {"id", "name"}
            view = {key: value for key, value in view.items() if key in allowed_view}
            groups = []
            for group in view.get("concept_groups") or []:
                if isinstance(group, dict):
                    groups.append(
                        {
                            key: value
                            for key, value in group.items()
                            if key in ConceptGroup.model_fields
                        }
                    )
            view["concept_groups"] = groups
            views.append(view)
    normalized["query_views"] = views
    try:
        return SearchPlan.model_validate(normalized)
    except (TypeError, ValueError):
        return None


def parse_query_proposal(text: str) -> tuple[str, str]:
    object_text = _extract_json_object_text(text)
    if not object_text:
        return "", ""
    try:
        payload = json.loads(object_text)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    query = ""
    for key in QUERY_FIELD_ALIASES:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            query = value.strip()
            break
        if isinstance(value, dict):
            nested = value.get("value") or value.get("query")
            if isinstance(nested, str) and nested.strip():
                query = nested.strip()
                break
    rationale_parts = []
    for key in RATIONALE_FIELD_ALIASES:
        value = payload.get(key)
        if isinstance(value, list):
            value = "; ".join(str(item).strip() for item in value if str(item).strip())
        value = str(value or "").strip()
        if value:
            rationale_parts.append(value)
    return query, "\n".join(rationale_parts).strip()


__all__ = ["normalize_question_type", "parse_query_proposal", "parse_search_plan"]
