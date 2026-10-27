from __future__ import annotations

import base64
import binascii
from datetime import datetime
import hashlib
import json
from typing import Annotated

from pydantic import Field, TypeAdapter

from paperseek_app.errors import RunNotFound, ArtifactNotFound, InvalidCursor, InvalidResultQuery
from paperseek_app.models import (
    ArtifactContent,
    ArtifactDescriptor,
    ArtifactRole,
    ArtifactVisibility,
    HistoryItem,
    HistoryPage,
    PersistedEvent,
    PersistedResult,
    ResultPage,
    RunAudit,
    RunDetail,
    StoredArtifact,
)
from paperseek_app.ports import ArtifactStore, RunUnitOfWork
from paperseek_app.derived_artifacts import EXPORT_KINDS, derive_exports, export_id
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.acceptance import ACCEPTANCE_FILTERS
from paperseek_core.journal_metrics import JOURNAL_FILTERS
from paperseek_core.reference_export import to_bibtex, to_ris


class RunQueryService:
    _MAX_PAGE_SIZE = 200
    _OFFSET = TypeAdapter(Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)])

    def __init__(self, uow: RunUnitOfWork, artifact_store: ArtifactStore) -> None:
        self.uow = uow
        self.artifact_store = artifact_store

    def get_run(self, run_id: str) -> RunDetail:
        run = self.uow.get_run(run_id)
        if run.kind != "research":
            raise RunNotFound(f"Run {run_id} was not found.")
        return run

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[PersistedEvent]:
        self.get_run(run_id)
        return self.uow.list_events(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def poll_events(self, run_id: str, *, after_sequence: int, limit: int = 200):
        """Read status once per replay batch, including terminal/deletion checks."""
        run = self.get_run(run_id)
        return run, self.uow.list_events(run_id, after_sequence=after_sequence, limit=limit)

    def get_run_log(self, run_id: str) -> str:
        lines = []
        for event in self.list_events(run_id):
            payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            lines.append(f"{event.sequence:06d} {event.occurred_at.isoformat()} {event.event_type} {payload}")
        return "\n".join(lines) + ("\n" if lines else "")

    def get_results(self, run_id: str) -> list[PersistedResult]:
        self.get_run(run_id)
        return self.uow.get_results(run_id)

    def list_artifacts(self, run_id: str) -> list[StoredArtifact]:
        self.get_run(run_id)
        return self.uow.list_artifacts(run_id)

    _RESULT_SORTS = frozenset({"rank", "score", "citations", "year", "impact"})
    _RESULT_METADATA_FILTERS = frozenset({"abstract", "doi", "pdf"})
    _EXPORT_FORMATS = {"json": "export_json", "csv": "export_csv", "bibtex": "export_bibtex", "bib": "export_bibtex",
                       "ris": "export_ris"}

    def get_result_page(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 50,
        query: str | None = None,
        min_score: float | None = None,
        metadata: str | None = None,
        sort: str = "rank",
        journal: str | None = None,
        acceptance: str | None = None,
    ) -> ResultPage:
        self._validate_page_size(page_size)
        if journal and journal not in JOURNAL_FILTERS:
            raise InvalidResultQuery("The journal filter is unsupported.")
        if acceptance and acceptance not in ACCEPTANCE_FILTERS:
            raise InvalidResultQuery("The acceptance filter is unsupported.")
        if sort not in self._RESULT_SORTS:
            raise InvalidResultQuery("The result sort is unsupported.")
        if metadata is not None and metadata not in self._RESULT_METADATA_FILTERS:
            raise InvalidResultQuery("The metadata filter is unsupported.")
        if min_score is not None and not 0 <= min_score <= 10:
            raise InvalidResultQuery("The minimum score is out of range.")
        self.get_run(run_id)
        fingerprint = self._result_fingerprint(query, min_score, metadata, sort, journal, acceptance)
        offset = 0
        if cursor:
            payload = self._decode_cursor(cursor, "results")
            if payload.get("run_id") != run_id or payload.get("filter") != fingerprint:
                raise InvalidCursor("The result cursor belongs to another run or filter.")
            try:
                offset = self._OFFSET.validate_python(payload["offset"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor("The result cursor is invalid.") from exc
            if offset < 1:
                raise InvalidCursor("The result cursor is invalid.")
        # Only passed when set, so stores written before these filters keep working.
        venue_filters = {key: value for key, value in (("journal", journal), ("acceptance", acceptance)) if value}
        page = self.uow.query_results(
            run_id, offset=offset, limit=page_size, query=query,
            min_score=min_score, metadata=metadata, sort=sort, **venue_filters,
        )
        next_cursor = None
        if page.matched_count > offset + page_size:
            next_cursor = self._encode_cursor(
                {
                    "scope": "results",
                    "run_id": run_id,
                    "filter": fingerprint,
                    "offset": offset + page_size,
                }
            )
        return page.model_copy(update={"next_cursor": next_cursor})

    @staticmethod
    def _result_fingerprint(
        query: str | None,
        min_score: float | None,
        metadata: str | None,
        sort: str,
        journal: str | None = None,
        acceptance: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "query": (query or "").strip(),
                "min_score": min_score,
                "metadata": metadata or "",
                "sort": sort,
                **({"journal": journal} if journal else {}),
                **({"acceptance": acceptance} if acceptance else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


    def get_history_page(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 50,
        query: str | None = None,
    ) -> HistoryPage:
        self._validate_page_size(page_size)
        if query is not None and query.strip():
            return self._search_history_page(cursor=cursor, page_size=page_size, query=query)
        before_created_at = None
        before_id = None
        if cursor:
            payload = self._decode_cursor(cursor, "history")
            try:
                before_created_at = datetime.fromisoformat(str(payload["created_at"]))
                before_id = str(payload["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor("The history cursor is invalid.") from exc
            if not before_id or before_created_at.tzinfo is None:
                raise InvalidCursor("The history cursor is invalid.")
        rows = self.uow.list_history(
            before_created_at=before_created_at,
            before_id=before_id,
            limit=page_size + 1,
        )
        visible = rows[:page_size]
        items = [self._history_item(item) for item in visible]
        next_cursor = None
        if len(rows) > page_size:
            last = visible[-1]
            next_cursor = self._encode_cursor(
                {
                    "scope": "history",
                    "created_at": last.created_at.isoformat(),
                    "id": last.id,
                }
            )
        return HistoryPage(items=items, next_cursor=next_cursor)

    def _search_history_page(
        self,
        *,
        cursor: str | None,
        page_size: int,
        query: str,
    ) -> HistoryPage:
        needle = query.strip().casefold()
        offset = 0
        if cursor:
            payload = self._decode_cursor(cursor, "history-search")
            if payload.get("query") != needle:
                raise InvalidCursor("The history cursor belongs to another search.")
            try:
                offset = self._OFFSET.validate_python(payload["offset"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor("The history cursor is invalid.") from exc
            if offset < 1:
                raise InvalidCursor("The history cursor is invalid.")
        page = self.uow.search_history(query=needle, offset=offset, limit=page_size)
        next_cursor = None
        if page.matched_count > offset + page_size:
            next_cursor = self._encode_cursor(
                {"scope": "history-search", "query": needle, "offset": offset + page_size}
            )
        return page.model_copy(update={"next_cursor": next_cursor})

    @staticmethod
    def _history_item(item) -> HistoryItem:
        return HistoryItem(
            display_title=item.display_title,
            id=item.id,
            question=item.question,
            source=item.source,
            status=item.status,
            result_count=item.result_count,
            error_code=item.error_code,
            warnings=item.warnings,
            created_at=item.created_at,
            finished_at=item.finished_at,
        )

    def list_user_artifacts(self, run_id: str) -> list[ArtifactDescriptor]:
        self.get_run(run_id)
        stored = self.uow.list_artifacts(run_id)
        descriptors = [
            self._artifact_descriptor(item)
            for item in stored
            if item.visibility is ArtifactVisibility.USER
            and item.role in {ArtifactRole.COMPLETION_REQUIRED, ArtifactRole.AUDIT}
        ]
        missing = EXPORT_KINDS - {item.kind for item in descriptors}
        primary = next((item for item in stored if item.kind == "final_result" and item.visibility is ArtifactVisibility.USER), None)
        if missing and primary:
            try:
                generated = derive_exports(primary, self.artifact_store.read(primary))
            except ArtifactNotFound:
                return descriptors
            descriptors.extend(descriptor for kind, (descriptor, _) in generated.items() if kind in missing)
        return descriptors

    def read_user_artifact(self, run_id: str, artifact_id: str) -> ArtifactContent:
        self.get_run(run_id)
        stored = self.uow.list_artifacts(run_id)
        artifact = next(
            (
                item
                for item in stored
                if item.id == artifact_id
                and item.visibility is ArtifactVisibility.USER
                and item.role in {ArtifactRole.COMPLETION_REQUIRED, ArtifactRole.AUDIT}
            ),
            None,
        )
        if artifact is None:
            primary = next((item for item in stored if item.kind == "final_result" and item.visibility is ArtifactVisibility.USER), None)
            kind = next((kind for kind in EXPORT_KINDS if primary and export_id(primary, kind) == artifact_id), None)
            if kind:
                return derive_exports(primary, self.artifact_store.read(primary))[kind][1]
            raise ArtifactNotFound("The user artifact was not found.")
        return self._artifact_content(artifact)

    def get_export(self, run_id: str, export_format: str) -> ArtifactContent:
        kind = self._EXPORT_FORMATS.get(export_format.casefold())
        if kind is None:
            raise ValueError("The export format must be json, csv, bibtex or ris.")
        return self._read_user_artifact_by_kind(run_id, kind)

    def get_selected_export(self, run_id: str, export_format: str, uids: list[str]) -> ArtifactContent:
        wanted = {str(uid) for uid in uids if str(uid).strip()}
        if not wanted:
            raise InvalidResultQuery("No papers were selected for export.")
        run = self.get_run(run_id)
        records = [
            item.record
            for item in self.uow.get_results(run_id, uids=sorted(wanted))
        ]
        if not records:
            raise InvalidResultQuery("The selected papers do not belong to this run.")
        if export_format.casefold() == "csv":
            content = FinalResultAssembler._csv(records).encode("utf-8")
            filename = "export-selected.csv"
            media_type = "text/csv"
        elif export_format.casefold() in {"bibtex", "bib"}:
            content = to_bibtex(records).encode("utf-8")
            filename = "export-selected.bib"
            media_type = "application/x-bibtex"
        elif export_format.casefold() == "ris":
            content = to_ris(records).encode("utf-8")
            filename = "export-selected.ris"
            media_type = "application/x-research-info-systems"
        elif export_format.casefold() == "json":
            content = (
                json.dumps(
                    {
                        "question": run.question,
                        "ranked": [record.model_dump(mode="json") for record in records],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
            filename = "export-selected.json"
            media_type = "application/json"
        else:
            raise InvalidResultQuery("The export format must be json, csv, bibtex or ris.")
        return ArtifactContent(
            id="selected-export",
            filename=filename,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest().upper(),
            size=len(content),
            content=content,
        )

    def get_citation_map(self, run_id: str) -> ArtifactContent:
        return self._read_user_artifact_by_kind(run_id, "citation_map")

    def get_bibliometric_corpus(self, run_id: str) -> ArtifactContent:
        return self._read_user_artifact_by_kind(run_id, "bibliometric_corpus")

    def get_run_audit(self, run_id: str) -> RunAudit:
        content = self._read_user_artifact_by_kind(run_id, "final_result")
        payload = json.loads(content.content)
        search_state = dict(payload.get("search_state") or {})
        queue = dict(search_state.get("candidate_queue") or {})
        ledger = dict(queue.get("ledger") or {})
        summary = dict(queue.get("ledger_summary") or {})
        lifecycle = dict(summary.get("lifecycle_candidate_counts") or {})
        ranking_steps = list(payload.get("ranking_steps") or [])
        selected_for_llm = int(lifecycle.get("selected_for_llm") or 0)
        if not selected_for_llm:
            reranking = next(
                (item for item in reversed(ranking_steps) if item.get("id") == "llm_reranking"),
                {},
            )
            selected_for_llm = int(reranking.get("candidate_count") or 0)
        ranked = list(payload.get("ranked") or [])
        ledger_candidates = int(summary.get("total_candidates") or len(ledger) or len(queue.get("keys") or []))
        ledger_retained = int(summary.get("serialized_candidates") or len(ledger))

        recovery = {key: 0 for key in ("query", "retrieval", "citation", "selection", "ranking")}
        recovery_event = next(
            (event for event in reversed(self.list_events(run_id)) if event.event_type == "workflow.recovery"),
            None,
        )
        if recovery_event is not None:
            for key in recovery:
                recovery[key] = int(recovery_event.payload.get(key) or 0)
        else:
            recovery["query"] = sum(
                event.event_type == "query.resumed" for event in self.list_events(run_id)
            )
        recovery["total"] = sum(recovery.values())

        history = list(payload.get("history") or [])
        return RunAudit(
            query_portfolio=list(payload.get("query_portfolio") or []),
            loop_decisions=[item for item in history if item.get("action") != "portfolio"],
            candidate_queue=queue,
            candidate_lifecycle={
                "ledger_candidates": ledger_candidates,
                "selected_for_llm": selected_for_llm,
                "final_output": int(lifecycle.get("final_output") or len(ranked)),
                "ledger_retained": ledger_retained,
            },
            recovery=recovery,
        )

    def _read_user_artifact_by_kind(self, run_id: str, kind: str) -> ArtifactContent:
        self.get_run(run_id)
        stored = self.uow.list_artifacts(run_id)
        artifact = next(
            (
                item
                for item in stored
                if item.kind == kind
                and item.visibility is ArtifactVisibility.USER
                and item.role in {ArtifactRole.COMPLETION_REQUIRED, ArtifactRole.AUDIT}
            ),
            None,
        )
        if artifact is None:
            primary = next((item for item in stored if item.kind == "final_result" and item.visibility is ArtifactVisibility.USER), None)
            if primary and kind in EXPORT_KINDS:
                return derive_exports(primary, self.artifact_store.read(primary))[kind][1]
            raise ArtifactNotFound("The requested artifact was not found.")
        return self._artifact_content(artifact)

    def _artifact_content(self, artifact: StoredArtifact) -> ArtifactContent:
        return ArtifactContent(
            id=artifact.id,
            filename=artifact.filename,
            media_type=artifact.media_type,
            sha256=artifact.sha256,
            size=artifact.size,
            content=self.artifact_store.read(artifact),
        )

    @staticmethod
    def _artifact_descriptor(artifact: StoredArtifact) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            id=artifact.id,
            kind=artifact.kind,
            filename=artifact.filename,
            media_type=artifact.media_type,
            sha256=artifact.sha256,
            size=artifact.size,
            schema_version=artifact.schema_version,
            created_at=artifact.created_at,
            download_url=f"/api/v1/runs/{artifact.run_id}/artifacts/{artifact.id}",
        )

    @classmethod
    def _validate_page_size(cls, page_size: int) -> None:
        if page_size < 1 or page_size > cls._MAX_PAGE_SIZE:
            raise ValueError("Page size must be between 1 and 200.")

    @staticmethod
    def _encode_cursor(payload: dict) -> str:
        content = json.dumps(
            {"version": 1, **payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, scope: str) -> dict:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.b64decode(cursor + padding, altchars=b"-_", validate=True))
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise InvalidCursor("The cursor is invalid.") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("scope") != scope:
            raise InvalidCursor("The cursor is invalid for this resource.")
        return payload
