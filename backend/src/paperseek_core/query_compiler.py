"""Source query validation, normalization, and structured-view compilation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlencode

from paperseek_core.models import ConceptGroup, QueryValidation, QueryView


SUPPORTED_QUERY_SOURCES = {
    "openalex",
    "arxiv",
    "crossref",
    "semanticscholar",
    "pubmed",
    "googlescholar",
    "paperhub",
    "wos",
    "wanfang",
    "nssd",
    "yiigle",
}
# Chinese databases: planning and queries use Chinese terminology and Chinese prompts.
CHINESE_QUERY_SOURCES = {"wanfang", "nssd", "yiigle"}
PLAIN_TEXT_QUERY_SOURCES = {"crossref", "semanticscholar"}
MINIMAL_BOOLEAN_QUERY_SOURCES = {"googlescholar", "paperhub"}
OPENALEX_MAX_ENCODED_SEARCH_BYTES = 3200


def _parentheses_balanced(query: str) -> bool:
    depth = 0
    for char in query or "":
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _looks_malformed(query: str) -> bool:
    value = (query or "").strip()
    return bool(
        not value
        or value.count('"') % 2
        or not _parentheses_balanced(value)
        or re.search(r"\b(?:AND|OR|NOT)\s*$", value, flags=re.IGNORECASE)
        or re.search(r"\(\s*$", value)
    )


def _top_level_boolean_matches(query: str) -> list[tuple[int, int, str]]:
    matches = []
    depth = 0
    in_quote = False
    escape = False
    index = 0
    while index < len(query):
        char = query[index]
        if escape:
            escape = False
            index += 1
            continue
        if char == "\\" and in_quote:
            escape = True
            index += 1
            continue
        if char == '"':
            in_quote = not in_quote
            index += 1
            continue
        if in_quote:
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and char.isalpha():
            end = index + 1
            while end < len(query) and query[end].isalpha():
                end += 1
            word = query[index:end].upper()
            if word in {"AND", "OR", "NOT"}:
                matches.append((index, end, word))
            index = end
            continue
        index += 1
    return matches


def validate_source_query(source: str, query: str) -> QueryValidation:
    source_id = (source or "").strip().lower()
    value = (query or "").strip()
    if not value:
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue="missing query field in JSON response",
        )
    if source_id not in SUPPORTED_QUERY_SOURCES:
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue=f"unsupported data source: {source_id or '(missing)'}",
        )
    if source_id not in PLAIN_TEXT_QUERY_SOURCES and _looks_malformed(value):
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue="unbalanced quotation marks, parentheses, or trailing Boolean operator",
        )
    if re.search(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue="query contains control characters or multiple lines",
        )
    api_parameter = re.search(
        r"(?:^|[?&\s])(?:query(?:\.[a-z_]+)?|search(?:\.[a-z_]+)?|filter|page|limit)=",
        value,
        flags=re.IGNORECASE,
    )
    if api_parameter or re.search(r"https?://", value, flags=re.IGNORECASE):
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue="query must contain only the source query value, not a URL or API parameter",
        )
    foreign_field = re.search(
        r"(?:^|[\s(])(?:TS|TI|AB|AU)\s*=|(?:^|[\s(])(?:all|ti|abs|cat|title|abstract|topic):|\[[A-Za-z][^\]]*\]",
        value,
        flags=re.IGNORECASE,
    )
    if source_id in PLAIN_TEXT_QUERY_SOURCES:
        if re.search(r"\s+(?:AND|OR|NOT|ANDNOT)\s+", value, flags=re.IGNORECASE):
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue=f"{source_id} relevance search requires plain text, not Boolean operators",
            )
        if foreign_field:
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue=f"{source_id} relevance search does not accept database field syntax",
            )
    if source_id in MINIMAL_BOOLEAN_QUERY_SOURCES:
        if re.search(r"\s+(?:AND|NOT|ANDNOT)\s+", value, flags=re.IGNORECASE):
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue=f"{source_id} uses implicit AND with OR groups and minus exclusions",
            )
        if foreign_field:
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue=f"{source_id} does not accept database field syntax",
            )
    if source_id == "openalex":
        if len(urlencode({"search": value}).encode("ascii")) > OPENALEX_MAX_ENCODED_SEARCH_BYTES:
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue="OpenAlex search query is too long for a safe request URL; use fewer established terms or split the search into query views",
            )
        operators = {operator for _, _, operator in _top_level_boolean_matches(value)}
        if "AND" in operators and "OR" in operators:
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue="OpenAlex mixed top-level AND/OR requires each OR synonym group to be parenthesized",
            )
        if re.search(r"[*?]", value):
            return QueryValidation(
                source=source_id,
                query=value,
                ok=False,
                issue="OpenAlex search= does not accept wildcard syntax; use complete terms",
            )
    if source_id == "arxiv" and re.search(
        r"(?:^|[\s(])(?:title|abstract|author|topic):", value, flags=re.IGNORECASE
    ):
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue="arXiv field prefixes must use all:, ti:, abs:, au:, cat:, id:, jr:, co:, or rn:",
        )
    if source_id in CHINESE_QUERY_SOURCES and not re.search(r"[\u4e00-\u9fff]", value):
        return QueryValidation(source=source_id, query=value, ok=False,
                               issue=f"{source_id} queries must use Chinese search terms")
    if source_id == "nssd" and re.search(r"[A-Za-z_]+\s*=", value):
        return QueryValidation(source=source_id, query=value, ok=False,
                               issue="NSSD queries use plain terms and Boolean operators; PaperSeek adds the field codes")
    if source_id == "yiigle" and (re.search(r"\b(?:AND|OR|NOT)\b|[()]", value) or ":" in value):
        return QueryValidation(source=source_id, query=value, ok=False,
                               issue="Yiigle accepts only space-separated terms, without Boolean operators or fields")
    if source_id == "pubmed" and re.search(
        r"(?:^|[\s(])(?:all|ti|abs|cat):", value, flags=re.IGNORECASE
    ):
        return QueryValidation(
            source=source_id,
            query=value,
            ok=False,
            issue="PubMed fields must use bracketed tags such as [Title/Abstract]",
        )
    return QueryValidation(source=source_id, query=value, ok=True)


def _dedupe_top_level_and(query: str) -> str:
    matches = _top_level_boolean_matches(query)
    if not matches or any(operator != "AND" for _, _, operator in matches):
        return query
    clauses = []
    start = 0
    for match_start, match_end, _ in matches:
        clauses.append(query[start:match_start].strip())
        start = match_end
    clauses.append(query[start:].strip())
    if any(not clause for clause in clauses):
        return query
    output = []
    seen = set()
    for clause in clauses:
        normalized = re.sub(r"\s+", " ", clause).strip().casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(clause)
    return " AND ".join(output)


def source_safe_query(source: str, query: str) -> str:
    value = str(query or "").strip()
    if source == "semanticscholar":
        return re.sub(
            r"\s+", " ", re.sub(r"(?<=\w)[\-‐-―](?=\w)", " ", value)
        ).strip()
    if source != "openalex":
        return value
    original = value
    cleaned = (
        original.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    )
    cleaned = re.sub(
        r"^\s*(?:search|q)\s*=\s*", "", cleaned, flags=re.IGNORECASE
    ).strip().strip("`")
    cleaned = re.sub(
        r"\b(?:filter|per-page|page|sort|select|fields)\s*=[^\s]+",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"([A-Za-z0-9])[*?]+\b", r"\1", cleaned).replace("*", "").replace("?", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([)])", r"\1", cleaned)
    cleaned = re.sub(r"([(])\s+", r"\1", cleaned)
    cleaned = re.sub(r"^\s*(?:AND|OR|NOT)\b\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\b(?:AND|OR|NOT)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\(\s*(?:AND|OR)\s+", "(", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:AND|OR)\s*\)", ")", cleaned, flags=re.IGNORECASE)
    return _dedupe_top_level_and(cleaned.strip()) or original


def strict_query_output_instruction(output_label: str) -> str:
    normalized_label = str(output_label or "").casefold()
    atomic = ""
    if "broadened" in normalized_label:
        atomic = (
            "- Make exactly one atomic broadening edit to the current query. Change one concept group only: "
            "remove one non-essential restrictive group, replace one group's wording with broader alternatives, "
            "or broaden the alternatives inside that one group. Copy every other concept group and filter unchanged. "
            "Do not remove one group and broaden another in the same response.\n"
            "- In rationale, name the one changed concept group and describe its before/after meaning.\n"
        )
    elif "narrowed" in normalized_label:
        atomic = (
            "- Make exactly one atomic narrowing edit to the current query. Change one concept group only: "
            "add one missing must-have group, replace one drift-causing alternative, or tighten the alternatives "
            "inside one existing group. Copy every other concept group and filter unchanged. Do not add two missing "
            "facets or tighten multiple groups in the same response.\n"
            "- In rationale, name the one changed concept group and describe its before/after meaning.\n"
        )
    return (
        "\nStructured output contract:\n"
        "- Return exactly one valid JSON object and no text outside JSON.\n"
        '- Use this schema: {"query":"...", "rationale":"..."}.\n'
        f"- The query field must contain exactly one complete {output_label}; it is the only value PaperSeek will send to the source API.\n"
        f"{atomic}"
        "- The rationale field may briefly explain the adjustment, direction, or title-intent diagnostic for audit.\n"
        "- Do not put the rationale, labels, markdown, bullets, or comments inside the query field.\n"
        "- If you compare returned titles with the intent, put the on-intent/off-intent conclusion in rationale and keep query clean.\n"
        "- If you cannot improve the query, repeat the current valid query in query and explain why in rationale.\n"
    )


@dataclass(frozen=True)
class CompiledQuery:
    source: str
    view_id: str
    query: str
    purpose: str = ""


def _selected_groups(view: QueryView) -> list[ConceptGroup]:
    required = [group for group in view.concept_groups if group.required]
    return required or list(view.concept_groups[:1])


def _clean_term(value: str, *, strip_wildcards: bool = True) -> str:
    term = (
        re.sub(r"\s+", " ", str(value or ""))
        .strip()
        .replace("\\", " ")
        .replace('"', " ")
    )
    if strip_wildcards:
        term = term.replace("*", "").replace("?", "")
    return re.sub(r"\s+", " ", term).strip(" ()[]{}")


def _quoted_term(value: str) -> str:
    term = _clean_term(value)
    return f'"{term}"' if term and re.search(r"\s", term) else term


def _unique_terms(
    groups: list[ConceptGroup], *, per_group: int = 4, total: int = 12
) -> list[str]:
    output = []
    seen = set()
    for group in groups:
        for value in group.terms[:per_group]:
            term = _clean_term(value)
            key = term.casefold()
            if not term or key in seen:
                continue
            seen.add(key)
            output.append(term)
            if len(output) >= total:
                return output
    return output


def compile_query_view(source: str, view: QueryView) -> CompiledQuery:
    source_id = str(source or "").strip().lower()
    if source_id not in SUPPORTED_QUERY_SOURCES:
        raise ValueError(f"No structured query compiler is available for source={source_id}")
    groups = _selected_groups(view)
    if source_id in {"crossref", "semanticscholar"}:
        terms = _unique_terms(
            groups,
            per_group=3 if source_id == "semanticscholar" else 4,
            total=10 if source_id == "semanticscholar" else 12,
        )
        query = " ".join(terms)
        if source_id == "semanticscholar":
            query = re.sub(
                r"\s+", " ", re.sub(r"(?<=\w)[\-‐-―](?=\w)", " ", query)
            ).strip()
    elif source_id == "arxiv":
        required = []
        optional = []
        fields = {"all": "all", "title": "ti", "abstract": "abs"}
        for group in view.concept_groups:
            terms = []
            for value in group.terms:
                term = (
                    re.sub(r"\s+", " ", str(value or ""))
                    .strip()
                    .replace("\\", " ")
                    .replace('"', " ")
                    .strip(" ()")
                )
                if term:
                    terms.append(
                        f'{fields.get(group.field_hint, "all")}:"{term}"'
                        if re.search(r"\s", term)
                        else f'{fields.get(group.field_hint, "all")}:{term}'
                    )
            clause = (
                terms[0]
                if len(terms) == 1
                else f"({' OR '.join(terms)})" if terms else ""
            )
            if clause:
                (required if group.required else optional).append(clause)
        clauses = required or (
            [optional[0] if len(optional) == 1 else f"({' OR '.join(optional)})"]
            if optional
            else []
        )
        query = " AND ".join(clauses)
        for exclusion in view.exclusions:
            term = re.sub(r"\s+", " ", exclusion).strip().replace('"', "").strip(" ()")
            if term:
                query += (
                    f' ANDNOT all:"{term}"'
                    if re.search(r"\s", term)
                    else f" ANDNOT all:{term}"
                )
    elif source_id == "openalex":
        required = []
        optional = []
        for group in view.concept_groups:
            terms = [_quoted_term(value) for value in group.terms]
            terms = [term for term in terms if term]
            clause = (
                terms[0]
                if len(terms) == 1
                else f"({' OR '.join(terms)})" if terms else ""
            )
            if clause:
                (required if group.required else optional).append(clause)
        clauses = required or (
            [optional[0] if len(optional) == 1 else f"({' OR '.join(optional)})"]
            if optional
            else []
        )
        query = " AND ".join(clauses)
        for exclusion in view.exclusions:
            term = _quoted_term(exclusion)
            if term:
                query += f" NOT {term}"
    elif source_id == "pubmed":
        fields = {"all": "Title/Abstract", "title": "Title", "abstract": "Title/Abstract"}
        clauses = []
        for group in groups:
            terms = [
                f"{_quoted_term(value)}[{fields.get(group.field_hint, 'Title/Abstract')}]"
                for value in group.terms
                if _quoted_term(value)
            ]
            if terms:
                clauses.append(
                    terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"
                )
        query = " AND ".join(clauses)
        exclusions = [
            f"{_quoted_term(value)}[Title/Abstract]"
            for value in view.exclusions
            if _quoted_term(value)
        ]
        if exclusions:
            query += f" NOT {exclusions[0] if len(exclusions) == 1 else f'({' OR '.join(exclusions)})'}"
    elif source_id in {"wanfang", "nssd"}:
        fields = {"title": "标题", "abstract": "摘要"}
        clauses = []
        for group in groups:
            terms = [_quoted_term(value) for value in group.terms]
            terms = [term for term in terms if term]
            if terms:
                clause = terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"
                field = fields.get(group.field_hint) if source_id == "wanfang" else None
                clauses.append(f"{field}:{clause if clause.startswith('(') else f'({clause})'}" if field else clause)
        query = " AND ".join(clauses)
        for exclusion in view.exclusions:
            term = _quoted_term(exclusion)
            if term:
                query += f" NOT {term}"
    elif source_id == "yiigle":
        # Every word is ANDed, so one term per required group.
        query = " ".join(_clean_term(group.terms[0]) for group in groups if group.terms and _clean_term(group.terms[0]))
    elif source_id in {"googlescholar", "paperhub"}:
        clauses = []
        for group in groups:
            terms = [_quoted_term(value) for value in group.terms]
            terms = [term for term in terms if term]
            if terms:
                if source_id == "googlescholar":
                    clauses.append(
                        terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"
                    )
                else:
                    clauses.append(" OR ".join(terms))
        query = " ".join(clauses)
        for exclusion in view.exclusions:
            term = _quoted_term(exclusion)
            if term:
                query += f" -{term}"
    else:
        clauses = []
        for group in groups:
            terms = [_quoted_term(value) for value in group.terms]
            terms = [term for term in terms if term]
            if terms:
                clauses.append(
                    terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"
                )
        topic = " AND ".join(clauses)
        for exclusion in view.exclusions:
            term = _quoted_term(exclusion)
            if term:
                topic += f" NOT {term}"
        query = f"TS=({topic})"
    if not query:
        raise ValueError(f"{source_id} query view did not contain compilable terms")
    validation = validate_source_query(source_id, query)
    if not validation.ok:
        raise ValueError(validation.issue)
    return CompiledQuery(
        source=source_id,
        view_id=view.view_id,
        query=query,
        purpose=view.purpose,
    )


__all__ = [
    "CHINESE_QUERY_SOURCES",
    "CompiledQuery",
    "SUPPORTED_QUERY_SOURCES",
    "compile_query_view",
    "source_safe_query",
    "strict_query_output_instruction",
    "validate_source_query",
]
