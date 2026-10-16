"""LLM score and explanation requests, including structured-output repair."""

from __future__ import annotations

import re
from typing import Any

from paperseek_core.document_identity import document_identity
from paperseek_core.errors import EmptyRankingBatchError
from paperseek_core.http_retry import failure_details
from paperseek_core.models import PaperRecord
from paperseek_core.ports import LLMPort
from paperseek_core.prompts import SYSTEM_RANKING_REASONS, SYSTEM_RESULT_RANKING
from paperseek_core.ranking_output import RankingOutputCodec, describe_document


class RankingScorer:
    def __init__(self, llm: LLMPort):
        self.llm = llm
        self.codec = RankingOutputCodec()
        self.missing_reason_count = 0
        self.reason_failures: list[dict] = []

    async def score_batch(
        self,
        question: str,
        documents: list[PaperRecord],
        plan_context: str,
    ) -> list[dict[str, Any]]:
        try:
            scores = await self.request_compact_scores(
                question, documents, plan_context
            )
        except Exception as exc:
            if not self.should_retry_in_chunks(exc):
                raise
            scores = await self.request_scores_in_chunks(
                question, documents, plan_context
            )
        matched, missing = self.codec.match_scores(documents, scores)
        if missing:
            try:
                recovered = await self.request_scores_in_chunks(
                    question, missing, plan_context
                )
            except Exception:
                if not matched:
                    raise
            else:
                matched, missing = self.codec.match_scores(documents, [*matched, *recovered])
            if not matched:
                raise EmptyRankingBatchError(
                    f"Chunked score-only ranking omitted {len(missing)}/{len(documents)} candidate(s)."
                )
        return matched

    async def attach_reasons(
        self,
        question: str,
        documents: list[PaperRecord],
        scores: list[dict[str, Any]],
        plan_context: str,
    ) -> list[dict[str, Any]]:
        reason_documents = self.reason_documents(documents, scores)
        reason_by_uid: dict[str, str] = {}
        exhausted: set[str] = set()
        self.missing_reason_count = 0
        self.reason_failures = []
        # One pass in moderate batches, then one retry for whatever a batch left out in smaller ones.
        for size in (32, 12):
            pending = [
                document
                for document in reason_documents
                if document_identity(document) not in reason_by_uid and document_identity(document) not in exhausted
            ]
            for start in range(0, len(pending), size):
                batch = pending[start : start + size]
                try:
                    reason_by_uid.update(
                        await self.request_reasons(question, batch, scores, plan_context)
                    )
                except Exception as exc:
                    self.reason_failures.append(failure_details(exc))
                    if not self.should_retry_in_chunks(exc):
                        exhausted.update(document_identity(document) for document in batch)
        self.missing_reason_count = sum(
            document_identity(document) not in reason_by_uid
            for document in reason_documents
        )
        for entry in scores:
            entry["reasoning"] = reason_by_uid.get(str(entry.get("uid") or ""), "")
        return scores

    @staticmethod
    def should_retry_in_chunks(exc: Exception) -> bool:
        details = failure_details(exc)
        if str(details.get("error_type") or "").endswith("Timeout") or getattr(exc, "code", "") == "provider_timeout":
            # A slow local model is exactly what a smaller batch fixes, so completed
            # request retries must not disable the chunk split.
            return True
        if details.get("attempts"):
            return False  # Request retries are complete; splitting would multiply them.
        text = str(exc or "").casefold()
        if "429" in text or "rate_limit" in text or "too many requests" in text:
            return False
        return any(
            marker in text
            for marker in (
                "context length",
                "context_length",
                "maximum context",
                "max token",
                "too many tokens",
                "truncat",
                "json",
                "message content",
                "did not contain",
                "empty response",
                "timed out",
                "timeout",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "api error (500)",
                "api error (502)",
                "api error (503)",
                "api error (504)",
                "upstream transport failure",
                "upstream proxy failed",
                "proxy_error",
                "network error",
                "connection reset",
            )
        )

    async def request_compact_scores(
        self,
        question: str,
        documents: list[PaperRecord],
        plan_context: str,
    ) -> list[dict[str, Any]]:
        descriptions = []
        for index, document in enumerate(documents, 1):
            description = describe_document(document, index, abstract_limit=500)
            descriptions.append(
                re.sub(
                    rf"^{index}\. UID:.*$",
                    f"{index}.",
                    description,
                    count=1,
                    flags=re.MULTILINE,
                )
            )
        papers_text = "\n\n".join(descriptions)
        messages = [
            {"role": "system", "content": SYSTEM_RESULT_RANKING},
            {
                "role": "user",
                "content": (
                    f"Research question: {question}\n\n"
                    f"Stable SearchPlan:\n{plan_context}\n\n"
                    f"Papers:\n\n{papers_text}\n\n"
                    'Return only {"scores":[[index,score],...]} with one pair for every index.'
                ),
            },
        ]
        raw = await self.llm.chat_json(messages, temperature=0.0, max_tokens=4096)
        return self.codec.parse_scores(raw, documents)

    async def request_scores_in_chunks(
        self,
        question: str,
        documents: list[PaperRecord],
        plan_context: str,
        chunk_size: int = 64,
    ) -> list[dict[str, Any]]:
        scores = []
        for start in range(0, len(documents), chunk_size):
            batch = documents[start : start + chunk_size]
            try:
                batch_scores = await self.request_compact_scores(
                    question, batch, plan_context
                )
            except Exception:
                if not scores:
                    raise
                break  # Retain completed chunks without multiplying upstream failures.
            matched, _ = self.codec.match_scores(batch, batch_scores)
            scores.extend(matched)
        return scores

    def reason_documents(
        self,
        documents: list[PaperRecord],
        scores: list[dict[str, Any]],
    ) -> list[PaperRecord]:
        score_by_uid = {
            str(entry.get("uid") or ""): float(entry.get("score") or 0)
            for entry in scores
        }
        ordered = sorted(
            [document for document in documents if document_identity(document) in score_by_uid],
            key=lambda document: score_by_uid.get(document_identity(document), 0),
            reverse=True,
        )
        if len(ordered) < 50:
            return ordered
        high_scores = [
            document
            for document in ordered
            if score_by_uid.get(document_identity(document), 0) >= 5
        ]
        return high_scores if len(high_scores) > 50 else ordered[:50]

    async def request_reasons(
        self,
        question: str,
        documents: list[PaperRecord],
        scores: list[dict[str, Any]],
        plan_context: str,
    ) -> dict[str, str]:
        score_by_uid = {
            str(entry.get("uid") or ""): entry.get("score", 0) for entry in scores
        }
        descriptions = []
        identities = []
        for index, document in enumerate(documents, 1):
            identity = document_identity(document)
            identities.append(identity)
            description = describe_document(document, index, abstract_limit=700)
            descriptions.append(
                re.sub(
                    rf"^{index}\. UID:.*$",
                    f"{index}. Score: {score_by_uid.get(identity, 0)}",
                    description,
                    count=1,
                    flags=re.MULTILINE,
                )
            )
        retained_text = "\n\n".join(descriptions)
        messages = [
            {"role": "system", "content": SYSTEM_RANKING_REASONS},
            {
                "role": "user",
                "content": (
                    f"Research question: {question}\n\n"
                    f"Stable SearchPlan:\n{plan_context}\n\n"
                    f"Retained papers:\n\n{retained_text}\n\n"
                    'Return only {"reasons":[[index,"reason"],...]}.'
                ),
            },
        ]
        # A one-sentence reason in Chinese can take 150 tokens; a tight budget cuts the JSON off.
        max_tokens = min(8192, max(2048, len(documents) * 180))
        raw = await self.llm.chat_json(messages, temperature=0.0, max_tokens=max_tokens)
        return self.codec.parse_reasons(raw, identities)
