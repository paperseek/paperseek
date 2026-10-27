"""Canonical final-result and artifact-draft assembly."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from pydantic import BaseModel

from paperseek_core.reference_export import to_bibtex, to_ris
from paperseek_core.models import (
    ArtifactDraft,
    FinalPaperResult,
    RankedPaper,
    ResearchAssemblyRequest,
    ResearchResult,
)


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class FinalResultAssembler:
    def assemble(self, request: ResearchAssemblyRequest) -> ResearchResult:
        ranked = [self._paper(index, item, request.annotations_by_uid.get(item.document.uid) or {})
                  for index, item in enumerate(request.items, 1)]
        provenance = {
            item.document.uid: dict(item.provenance)
            for item in request.items
            if item.document.uid
        }
        result = ResearchResult(
            bibliometric_corpus=request.bibliometric_corpus,
            question=request.question,
            search_intent=request.search_plan.audit_summary(),
            search_plan=request.search_plan.model_dump(mode="json"),
            query_view_selection=[_dump(item) for item in request.query_view_selection],
            query_portfolio=[_dump(item) for item in request.query_portfolio],
            query_iteration_memory=[_dump(item) for item in request.query_iteration_memory],
            source=request.source,
            final_query=request.final_query,
            db=request.source.upper(),
            total=request.total,
            iterations=request.iterations,
            history=[_dump(item) for item in request.history],
            search_state=request.search_state,
            citation_map=self._citation_map(request.citation_map, ranked),
            ranking_steps=request.ranking_steps,
            ranked=ranked,
            warnings=list(dict.fromkeys(request.warnings)),
            provenance_by_identity=provenance,
        )
        return result.model_copy(update={"artifacts": self.artifacts_for(result)})

    @staticmethod
    def _citation_map(citation_map: dict[str, Any], ranked: list[FinalPaperResult]) -> dict[str, Any]:
        if not citation_map:
            return {}
        nodes = {node["id"]: {**node, "roles": list(node.get("roles") or [])} for node in citation_map.get("nodes", []) if node.get("id")}
        for paper in ranked:
            node_id = paper.uid or paper.id or paper.title
            if not node_id:
                continue
            node = nodes.setdefault(node_id, {
                "id": node_id, "title": paper.title or "(no title)", "year": paper.year,
                "source": paper.venue, "citations": paper.citation_count, "roles": [], "seed_uids": [],
            })
            if "result" not in node["roles"]:
                node["roles"].append("result")
            node.update(rank=paper.rank, score=paper.score, reasoning=paper.reasoning)
        return {**citation_map, "nodes": list(nodes.values())}

    @staticmethod
    def _paper(rank: int, item: RankedPaper, annotations: dict[str, Any] | None = None) -> FinalPaperResult:
        document = item.document
        identifiers = document.identifiers
        annotations = annotations or {}
        source = document.source
        links_model = document.links
        links = links_model.model_dump(mode="json") if links_model else {}
        authors = [
            author.display_name or author.wos_standard
            for author in (document.names.authors if document.names else [])
            if author.display_name or author.wos_standard
        ]
        keywords = list(document.keywords.author_keywords if document.keywords else [])
        citation_count = sum(int(citation.count or 0) for citation in document.citations)
        publication_type = document.types[0] if document.types else ""
        venue = source.source_title if source else ""
        year = source.publish_year if source else None
        doi = document.identifiers.doi if document.identifiers else ""
        url = links.get("record") or links.get("landing_page") or document.uid
        provenance = item.provenance
        return FinalPaperResult(
            rank=rank,
            source=venue,
            id=url,
            title=document.title,
            authors=authors,
            year=year,
            venue=venue,
            publication_type=publication_type,
            doi=doi,
            url=url,
            pdf_url=links.get("pdf") or "",
            abstract=document.abstract,
            keywords="; ".join(keywords),
            citation_count=citation_count,
            relevance_score=item.score,
            relevance_reason=item.reasoning,
            source_rank=int(provenance.get("source_rank") or rank),
            retrieval_score=float(provenance.get("retrieval_score") or 0),
            retrieval_lanes=list(provenance.get("retrieval_lanes") or []),
            source_raw_id=document.uid,
            links=links,
            keywords_text="; ".join(keywords),
            authors_text="; ".join(authors),
            score=item.score,
            provider=document.provider,
            uid=document.uid,
            publish_year=year,
            document_types=list(document.types),
            citations=str(citation_count),
            reasoning=item.reasoning,
            volume=str(source.volume or "") if source else "",
            issue=str(source.issue or "") if source else "",
            pages=FinalResultAssembler._pages(source.pages) if source else "",
            issn=identifiers.issn if identifiers else "",
            eissn=identifiers.eissn if identifiers else "",
            arxiv_id=identifiers.arxiv if identifiers else "",
            journal_metrics=dict(annotations.get("journal_metrics") or {}),
            acceptance=dict(annotations.get("acceptance") or {}),
        )

    @staticmethod
    def _pages(value: Any) -> str:
        if isinstance(value, dict):
            begin, end = str(value.get("begin") or value.get("first_page") or ""), str(value.get("end") or value.get("last_page") or "")
            return f"{begin}-{end}" if begin and end and begin != end else begin
        return str(value or "").strip()

    def artifacts_for(self, result: ResearchResult) -> list[ArtifactDraft]:
        """Serialize a final result, including imported history, without rerunning research."""
        # Retain export inputs in the saved result without changing the public response.
        public = result.public_payload() | {
            "query_iteration_memory": result.query_iteration_memory,
            "warnings": result.warnings,
            "provenance_by_identity": result.provenance_by_identity,
        }
        export = {
            "question": result.question,
            "search_intent": result.search_intent,
            "search_plan": result.search_plan,
            "query_view_selection": result.query_view_selection,
            "query_portfolio": result.query_portfolio,
            "query_iteration_memory": result.query_iteration_memory,
            "source": result.source,
            "query": result.final_query,
            "database": result.db,
            "final_query": result.final_query,
            "db": result.db,
            "field": result.field or None,
            "total_results": result.total,
            "total": result.total,
            "iterations": result.iterations,
            "history": result.history,
            "search_state": result.search_state,
            "citation_map": result.citation_map,
            "ranking_steps": result.ranking_steps,
            "ranked": [item.model_dump(mode="json") for item in result.ranked],
        }
        if result.warnings:
            export["warnings"] = list(result.warnings)
        artifacts = [
            ArtifactDraft(
                kind="final_result",
                filename="final_result.json",
                media_type="application/json",
                content=json.dumps(public, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
            ArtifactDraft(
                kind="export_json",
                filename="export.json",
                media_type="application/json",
                content=json.dumps(export, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
            ArtifactDraft(
                kind="export_csv",
                filename="export.csv",
                media_type="text/csv",
                content=self._csv(result.ranked),
            ),
            ArtifactDraft(
                kind="export_bibtex",
                filename="export.bib",
                media_type="application/x-bibtex",
                content=to_bibtex(result.ranked),
            ),
            ArtifactDraft(
                kind="export_ris",
                filename="export.ris",
                media_type="application/x-research-info-systems",
                content=to_ris(result.ranked),
            ),
            ArtifactDraft(
                kind="candidate_pool",
                filename="candidate_pool.jsonl",
                media_type="application/x-ndjson",
                content=self._candidate_pool(result),
            ),
            ArtifactDraft(
                kind="citation_map",
                filename="citation_map.json",
                media_type="application/json",
                content=json.dumps(result.citation_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
        ]
        if result.bibliometric_corpus is not None:
            artifacts.append(ArtifactDraft(
                kind="bibliometric_corpus", filename="bibliometric_corpus.json",
                media_type="application/json", content=result.bibliometric_corpus.model_dump_json(),
            ))
        return artifacts

    @staticmethod
    def _csv(items: list[FinalPaperResult]) -> str:
        fields = [
            "rank",
            "score",
            "title",
            "authors",
            "publish_year",
            "source",
            "provider",
            "document_types",
            "citations",
            "doi",
            "keywords",
            "abstract",
            "reasoning",
            "record_url",
            "pdf_url",
        ]
        output = io.StringIO(newline="")
        output.write(",".join(fields) + "\n")
        writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\n")
        for item in items:
            writer.writerow(
                [
                    item.rank,
                    FinalResultAssembler._number(item.score),
                    item.title,
                    item.authors_text,
                    item.publish_year or "",
                    item.source,
                    item.provider,
                    "; ".join(item.document_types),
                    item.citations,
                    item.doi,
                    item.keywords_text,
                    item.abstract,
                    item.reasoning,
                    item.url,
                    item.pdf_url,
                ]
            )
        return output.getvalue()

    @staticmethod
    def _number(value: float | None) -> str:
        if value is None:
            return ""
        return str(int(value)) if float(value).is_integer() else str(value)

    @staticmethod
    def _candidate_pool(result: ResearchResult) -> str:
        queue = result.search_state.get("candidate_queue") or {}
        keys = list(queue.get("keys") or [])
        ledger = dict(queue.get("ledger") or {})
        if keys and ledger:
            rows = [
                {
                    "record_type": "summary",
                    "keys": keys,
                    "ledger_summary": queue.get("ledger_summary") or {},
                    "origin_counts": queue.get("origin_counts") or {},
                    "duplicate_count": int(queue.get("duplicate_count") or 0),
                },
                *[
                    {"record_type": "candidate", **ledger[key]}
                    for key in keys
                    if key in ledger
                ],
            ]
        else:
            rows = [
                {
                    "record_type": "summary",
                    "keys": [item.uid for item in result.ranked],
                    "ledger_summary": {
                        "serialized_candidates": len(result.ranked),
                        "total_candidates": len(result.ranked),
                    },
                    "origin_counts": {},
                    "duplicate_count": 0,
                },
                *[
                    {
                        "record_type": "candidate",
                        "candidate_id": item.uid,
                        "title": item.title,
                        "doi": item.doi,
                        "year": item.publish_year,
                        "origins": result.provenance_by_identity.get(item.uid, {}).get("origins", []),
                        "retrieval_lanes": item.retrieval_lanes,
                        "retrieval_score": item.retrieval_score,
                        "llm_score": item.score,
                    }
                    for item in result.ranked
                ],
            ]
        return "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )


__all__ = ["FinalResultAssembler"]
