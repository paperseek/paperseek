"""Paper prompt rendering and validation of ranking responses."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Callable

from paperseek_core.document_identity import document_identity, document_key
from paperseek_core.json_repair import loads_closing_brackets
from paperseek_core.models import PaperRecord, RankingScore

_REASON_PAIR = re.compile(r'\[\s*"?(\d+)"?\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]')


def describe_document(
    document: PaperRecord, index: int, *, abstract_limit: int = 1200
) -> str:
    parts = [f"{index}. UID: {document.uid or document_key(document)}"]
    if document.provider:
        parts.append(f"   Provider: {document.provider}")
    if document.title:
        parts.append(f"   Title: {document.title}")
    if document.types:
        parts.append(f"   Document types: {'; '.join(document.types[:5])}")
    if document.source:
        source_parts = []
        if document.source.source_title:
            source_parts.append(document.source.source_title)
        if document.source.publish_year:
            source_parts.append(str(document.source.publish_year))
        if document.source.volume:
            source_parts.append(f"V{document.source.volume}")
        if document.source.issue:
            source_parts.append(f"I{document.source.issue}")
        if source_parts:
            parts.append(f"   Source: {', '.join(source_parts)}")
    if document.names and document.names.authors:
        authors = [
            author.display_name or author.wos_standard or ""
            for author in document.names.authors[:5]
        ]
        parts.append(f"   Authors: {', '.join(author for author in authors if author)}")
    if document.keywords and document.keywords.author_keywords:
        parts.append(f"   Keywords: {'; '.join(document.keywords.author_keywords[:8])}")
    if document.abstract:
        parts.append(
            f"   Abstract: {document.abstract[: max(200, int(abstract_limit or 1200))]}"
        )
    return "\n".join(parts)


@dataclass(frozen=True)
class RankingOutputCodec:
    identity: Callable[[PaperRecord], str] = document_identity

    @staticmethod
    def json_object(raw: str) -> dict:
        text = str(raw or "").strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            value = loads_closing_brackets(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return {}
            try:
                value = loads_closing_brackets(match.group(0))
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                return {}

    def parse_scores(
        self, raw: str, documents: list[PaperRecord]
    ) -> list[dict[str, Any]]:
        pairs = self.json_object(raw).get("scores")
        if not isinstance(pairs, list):
            return []
        output = []
        seen = set()
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                index = int(pair[0])
                score = float(pair[1])
            except (TypeError, ValueError):
                continue
            if (
                index < 1
                or index > len(documents)
                or index in seen
                or not math.isfinite(score)
            ):
                continue
            seen.add(index)
            output.append(
                {
                    "uid": self.identity(documents[index - 1]),
                    "score": score,
                    "reasoning": "",
                }
            )
        return output

    @staticmethod
    def parse_reasons(raw: str, identities: list[str]) -> dict[str, str]:
        pairs = RankingOutputCodec.json_object(raw).get("reasons")
        if not isinstance(pairs, list):
            # A reply cut off mid-list still holds complete [index, "reason"] pairs before the break.
            pairs = []
            for match in _REASON_PAIR.finditer(str(raw or "")):
                try:
                    pairs.append([match.group(1), json.loads(f'"{match.group(2)}"')])
                except json.JSONDecodeError:
                    continue
        output = {}
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                index = int(pair[0])
            except (TypeError, ValueError):
                continue
            reason = re.sub(r"\s+", " ", str(pair[1] or "")).strip()
            if 1 <= index <= len(identities) and reason:
                output[identities[index - 1]] = reason
        return output

    def match_scores(
        self,
        documents: list[PaperRecord],
        scores: Any,
    ) -> tuple[list[dict[str, Any]], list[PaperRecord]]:
        identities = [self.identity(document) for document in documents]
        exact = {
            identity.casefold(): index
            for index, identity in enumerate(identities)
            if identity
        }
        matched_indexes = set()
        matched_scores = []
        for entry in self.validated_scores(scores):
            uid = str(entry.get("uid") or "").strip()
            if not uid:
                continue
            index = exact.get(uid.casefold())
            if index is None:
                candidates = [
                    candidate_index
                    for candidate_index, identity in enumerate(identities)
                    if identity
                    and (
                        uid.casefold() in identity.casefold()
                        or identity.casefold() in uid.casefold()
                    )
                ]
                if len(candidates) == 1:
                    index = candidates[0]
            if index is None or index in matched_indexes:
                continue
            normalized = dict(entry)
            normalized["uid"] = identities[index]
            matched_scores.append(normalized)
            matched_indexes.add(index)
        missing = [
            document
            for index, document in enumerate(documents)
            if index not in matched_indexes
        ]
        return matched_scores, missing

    @staticmethod
    def validated_scores(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        output = []
        for item in value:
            try:
                output.append(RankingScore.model_validate(item).model_dump())
            except (TypeError, ValueError):
                continue
        return output
