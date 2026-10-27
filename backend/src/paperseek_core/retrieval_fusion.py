"""Pure, deterministic candidate identity, local scores and RRF fusion."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from hashlib import blake2b
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from paperseek_core.document_identity import document_alias_keys, document_key
from paperseek_core.models import PaperRecord


class RetrievalLane:
    RELEVANCE = "relevance"
    SEMANTIC = "semantic"
    IMPACT = "impact"
    RECENT = "recent"
    LOCAL_QUALITY = "local_quality"
    CITATION = "citation_expansion"



LANE_WEIGHTS = {
    RetrievalLane.RELEVANCE: 1.0,
    RetrievalLane.SEMANTIC: 1.0,
    RetrievalLane.IMPACT: 0.9,
    RetrievalLane.RECENT: 0.8,
    RetrievalLane.LOCAL_QUALITY: 0.9,
    RetrievalLane.CITATION: 1.0,
}



RRF_COMPONENT_WEIGHT = 0.50



PRIMARY_LANE_PRIOR_WEIGHT = 0.15



EMBEDDING_COMPONENT_WEIGHT = 0.1225



BM25_COMPONENT_WEIGHT = 0.14



TERM_COVERAGE_WEIGHT = 0.0875



_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+\-.]{1,}", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")



_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "paper",
    "papers",
    "research",
    "study",
    "studies",
    "the",
    "to",
    "with",
}



@dataclass
class FusedRetrievalResult:
    documents: list[PaperRecord]
    metadata_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    lane_counts: dict[str, int] = field(default_factory=dict)
    total_candidates: int = 0



def document_text(document: PaperRecord) -> str:
    parts = [value for value in (document.title, document.abstract) if value]
    if document.source:
        parts.extend(
            str(value)
            for value in (document.source.source_title, document.source.publish_year)
            if value
        )
    if document.keywords:
        parts.extend(value for value in document.keywords.author_keywords if value)
    if document.names:
        for author in document.names.authors:
            name = author.display_name or author.wos_standard
            if name:
                parts.append(name)
    parts.extend(value for value in document.types if value)
    return " ".join(str(value) for value in parts)



def dedupe_documents(documents: Sequence[PaperRecord]) -> list[PaperRecord]:
    output = []
    aliases_seen: set[str] = set()
    for document in documents:
        aliases = set(document_alias_keys(document))
        if not aliases or aliases & aliases_seen:
            continue
        aliases_seen.update(aliases)
        output.append(document)
    return output



def fuse_candidates_rrf(
    query: str,
    lane_results: Mapping[str, Sequence[PaperRecord]],
    *,
    pool_max: int = 3000,
    rrf_k: int = 60,
    embedding_scores_by_key: Mapping[str, float] | None = None,
) -> FusedRetrievalResult:
    pool_max = max(1, int(pool_max or 3000))
    rrf_k = max(1, int(rrf_k or 60))
    key_to_doc: dict[str, PaperRecord] = {}
    alias_to_key: dict[str, str] = {}
    lane_ranks: dict[str, dict[str, int]] = {}

    for lane, documents in lane_results.items():
        seen_in_lane = set()
        ranks: dict[str, int] = {}
        for document in documents or []:
            aliases = document_alias_keys(document)
            key = next((alias_to_key[alias] for alias in aliases if alias in alias_to_key), "")
            if not key:
                key = document_key(document)
            if not key:
                continue
            for alias in aliases:
                alias_to_key.setdefault(alias, key)
            if key in seen_in_lane:
                continue
            seen_in_lane.add(key)
            key_to_doc.setdefault(key, document)
            ranks[key] = len(ranks) + 1
        lane_ranks[lane] = ranks

    if not key_to_doc:
        return FusedRetrievalResult(
            documents=[],
            lane_counts={lane: 0 for lane in lane_results},
        )

    keys = list(key_to_doc)
    documents = [key_to_doc[key] for key in keys]
    texts = [document_text(document) for document in documents]
    bm25 = _bm25_scores(query, texts)
    query_vector = _hashed_vector(query)
    if embedding_scores_by_key:
        cosine = [
            max(0.0, min(1.0, float(embedding_scores_by_key.get(key, 0.0) or 0.0)))
            for key in keys
        ]
    else:
        cosine = [_cosine(query_vector, _hashed_vector(text)) for text in texts]
    coverage = [_term_coverage(query, text) for text in texts]
    bm25_norm = _normalize_scores(bm25)

    raw_rrf_scores = []
    source_ranks = []
    retrieval_lanes = []
    for key in keys:
        rrf_score = 0.0
        lanes = []
        source_rank = None
        for lane, ranks in lane_ranks.items():
            rank = ranks.get(key)
            if rank is None:
                continue
            lanes.append(lane)
            source_rank = rank if source_rank is None else min(source_rank, rank)
            rrf_score += LANE_WEIGHTS.get(lane, 0.8) / (rrf_k + rank)
        raw_rrf_scores.append(rrf_score)
        source_ranks.append(source_rank)
        retrieval_lanes.append(lanes)

    normalized_rrf_scores = _normalize_scores(raw_rrf_scores)
    scored = []
    metadata_by_key: dict[str, dict[str, Any]] = {}
    for index, key in enumerate(keys):
        relevance_rank = lane_ranks.get(RetrievalLane.RELEVANCE, {}).get(key)
        primary_prior = 1.0 / math.log2(relevance_rank + 1.0) if relevance_rank else 0.0
        final_score = (
            RRF_COMPONENT_WEIGHT * normalized_rrf_scores[index]
            + PRIMARY_LANE_PRIOR_WEIGHT * primary_prior
            + EMBEDDING_COMPONENT_WEIGHT * cosine[index]
            + BM25_COMPONENT_WEIGHT * bm25_norm[index]
            + TERM_COVERAGE_WEIGHT * coverage[index]
        )
        metadata_by_key[key] = {
            "source_rank": source_ranks[index] or index + 1,
            "retrieval_source_ranks": {
                lane: ranks[key]
                for lane, ranks in lane_ranks.items()
                if key in ranks
            },
            "retrieval_score": round(final_score, 6),
            "retrieval_lanes": retrieval_lanes[index],
            "retrieval_rrf_score": round(raw_rrf_scores[index], 6),
            "retrieval_rrf_normalized": round(normalized_rrf_scores[index], 6),
            "retrieval_primary_prior": round(primary_prior, 6),
            "retrieval_embedding_score": round(cosine[index], 6),
            "retrieval_similarity_method": "external_embedding" if embedding_scores_by_key else "local_hash",
            "retrieval_bm25_score": round(bm25_norm[index], 6),
            "retrieval_term_coverage": round(coverage[index], 6),
        }
        scored.append((final_score, -(source_ranks[index] or index + 1), key))

    scored.sort(reverse=True)
    selected_keys = [key for _, _, key in scored[:pool_max]]
    return FusedRetrievalResult(
        documents=[key_to_doc[key] for key in selected_keys],
        metadata_by_key={key: metadata_by_key[key] for key in selected_keys},
        lane_counts={lane: len(ranks) for lane, ranks in lane_ranks.items()},
        total_candidates=len(key_to_doc),
    )



def fuse_citation_candidates(
    question: str,
    candidates: Sequence[PaperRecord],
    provenance: Mapping[str, dict[str, Any]],
    *,
    pool_max: int,
    rrf_k: int = 60,
    embedding_scores_by_key: Mapping[str, float] | None = None,
) -> FusedRetrievalResult:
    documents = dedupe_documents(candidates)
    prior = {
        document_key(document): dict(provenance.get(document_key(document)) or provenance.get(document.uid) or {})
        for document in documents
    }
    citation_documents = [
        document for document in documents
        if "citation_expansion" in prior[document_key(document)].get("origins", [])
    ]
    if not citation_documents:
        return FusedRetrievalResult(documents=documents, metadata_by_key=prior)

    ranked_lanes: dict[str, list[tuple[int, str, PaperRecord]]] = {}
    for document in documents:
        key = document_key(document)
        for lane, rank in prior[key].get("retrieval_source_ranks", {}).items():
            ranked_lanes.setdefault(lane, []).append((int(rank), key, document))
    lane_results = {
        lane: [document for _, _, document in sorted(values, key=lambda item: (item[0], item[1]))]
        for lane, values in ranked_lanes.items()
    }
    lane_results[RetrievalLane.LOCAL_QUALITY] = [
        document for document in documents
        if not prior[document_key(document)].get("origins")
        or any(origin != "citation_expansion" for origin in prior[document_key(document)]["origins"])
    ]
    lane_results[RetrievalLane.CITATION] = citation_documents
    fused = fuse_candidates_rrf(
        question, lane_results, pool_max=pool_max, rrf_k=rrf_k,
        embedding_scores_by_key=embedding_scores_by_key,
    )
    fused.metadata_by_key = {
        key: {**prior[key], **metadata}
        for key, metadata in fused.metadata_by_key.items()
    }
    return fused



def _tokens(text: str) -> list[str]:
    words = [
        token.lower()
        for token in _TOKEN_RE.findall(text or "")
        if token.lower() not in _STOPWORDS
    ]
    # Chinese has no spaces between words; overlapping character pairs stand in for words.
    pairs = [run[index:index + 2] for run in _CJK_RE.findall(text or "") for index in range(max(1, len(run) - 1))]
    return words + pairs



def _hashed_vector(text: str, dimensions: int = 512) -> dict[int, float]:
    tokens = _tokens(text)
    features: Iterable[str] = tokens
    char_features = []
    compact = re.sub(r"\s+", " ", (text or "").lower())
    if len(compact) >= 3:
        char_features = [
            compact[index:index + 3]
            for index in range(0, min(len(compact) - 2, 600))
        ]
    vector: dict[int, float] = {}
    for feature, count in Counter([*features, *char_features]).items():
        digest = blake2b(feature.encode("utf-8", errors="ignore"), digest_size=8).digest()
        slot = int.from_bytes(digest[:4], "little") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[slot] = vector.get(slot, 0.0) + sign * count
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm <= 0:
        return {}
    return {key: value / norm for key, value in vector.items()}



def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    score = sum(value * right.get(key, 0.0) for key, value in left.items())
    return max(0.0, min(1.0, score))



def _bm25_scores(query: str, documents: Sequence[str]) -> list[float]:
    query_terms = _tokens(query)
    if not query_terms or not documents:
        return [0.0 for _ in documents]
    doc_tokens = [_tokens(document) for document in documents]
    doc_count = len(doc_tokens)
    avg_len = sum(len(tokens) for tokens in doc_tokens) / max(1, doc_count)
    doc_freq: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    scores = []
    k1 = 1.5
    b = 0.75
    for tokens in doc_tokens:
        term_freq: dict[str, int] = {}
        for token in tokens:
            term_freq[token] = term_freq.get(token, 0) + 1
        doc_len = len(tokens)
        score = 0.0
        for term in query_terms:
            tf = term_freq.get(term, 0)
            if tf <= 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + ((doc_count - df + 0.5) / (df + 0.5)))
            denom = tf + k1 * (1 - b + b * doc_len / max(avg_len, 1.0))
            score += idf * ((tf * (k1 + 1)) / max(denom, 1e-9))
        scores.append(score)
    return scores



def _term_coverage(query: str, document: str) -> float:
    query_terms = set(_tokens(query))
    if not query_terms:
        return 0.0
    document_terms = set(_tokens(document))
    return len(query_terms & document_terms) / max(1, len(query_terms))



def _normalize_scores(scores: Sequence[float]) -> list[float]:
    if not scores:
        return []
    high = max(scores)
    low = min(scores)
    if high <= low:
        return [1.0 if high > 0 else 0.0 for _ in scores]
    return [(score - low) / (high - low) for score in scores]

