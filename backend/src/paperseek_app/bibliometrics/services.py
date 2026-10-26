"""Analysis lifecycle; files stay independent of deletable source searches."""

from collections.abc import Callable
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import io
import json
from uuid import uuid4

from paperseek_app.bibliometrics.models import (
    AnalysisCreate,
    AnalysisDetail,
    AnalysisError,
    AnalysisRecord,
)
from paperseek_app.bibliometrics.ports import AnalysisStore
from paperseek_app.errors import ArtifactNotFound
from paperseek_app.models import (
    ArtifactRole,
    ArtifactVisibility,
    ArtifactWrite,
    RunStatus,
)
from paperseek_app.ports import ArtifactStore
from paperseek_app.services.run_queries import RunQueryService
from paperseek_core.bibliometrics.models import (
    MetricCorpus,
    MetricNetwork,
    MetricOverview,
    MetricPaper,
    NetworkRequest,
    NetworkTooDense,
)
from paperseek_core.bibliometrics.corpus import rank_corpus
from paperseek_core.bibliometrics.networks import network
from paperseek_core.bibliometrics.statistics import overview
from paperseek_core.journal_metrics import JournalMetricsTable


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class AnalysisService:
    def __init__(
        self,
        store: AnalysisStore,
        files: ArtifactStore,
        runs: RunQueryService | None = None,
        rankings: Callable[[], JournalMetricsTable | None] | None = None,
    ):
        self.store, self.files, self.runs = store, files, runs
        self.rankings = rankings

    def create(self, request: AnalysisCreate, key: str) -> AnalysisDetail:
        signature = digest(request.model_dump())
        existing = self.store.find_analysis(key)
        if existing:
            if existing.request_digest != signature:
                raise AnalysisError("analysis_idempotency_conflict")
            return self.get(existing.id)
        assert self.runs is not None
        source = self.runs.get_run(request.source_run_id)
        if (
            source.status
            not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS}
            or source.kind != "research"
        ):
            raise AnalysisError("analysis_source_not_ready")
        try:
            content = self.runs.get_bibliometric_corpus(source.id)
            corpus = MetricCorpus.model_validate_json(content.content)
        except ArtifactNotFound:
            # Old saved searches never contained complete metadata. Do not label
            # a scored subset as the full retrieval pool, or fetch more data.
            records = self.runs.uow.get_results(source.id)
            corpus = MetricCorpus(
                scope="legacy_ranked_only",
                warnings=["legacy_ranked_only"],
                papers=[
                    MetricPaper(
                        id=item.record.uid or str(item.rank),
                        title=item.record.title,
                        doi=item.record.doi,
                        year=item.record.year,
                        venue=item.record.venue,
                        provider=item.record.provider,
                        authors=item.record.authors,
                        document_types=item.record.document_types,
                        keywords=[
                            term.strip().casefold()
                            for term in item.record.keywords.split(";")
                            if term.strip()
                        ],
                        keyword_source="legacy:unverified",
                        scored=item.record.score is not None,
                        issns=[issn for issn in (item.record.issn, item.record.eissn) if issn],
                    )
                    for item in records
                ],
            )
        if not corpus.papers:
            raise AnalysisError("analysis_empty_corpus", 422)
        if len(corpus.papers) > 50000:
            raise AnalysisError("analysis_corpus_too_large", 422)
        if self.rankings is not None:
            corpus = rank_corpus(corpus, self.rankings())
        identity, written = str(uuid4()), []
        try:
            corpus_file = self._put(identity, "corpus", corpus.model_dump_json())
            written.append(corpus_file)
            overview_file = self._put(
                identity, "overview", overview(corpus).model_dump_json()
            )
            written.append(overview_file)
            record = self.store.create_analysis(
                AnalysisRecord(
                    id=identity,
                    source_run_id=source.id,
                    title=request.title.strip() or source.question[:200],
                    paper_count=len(corpus.papers),
                    scope=corpus.scope,
                    created_at=datetime.now(timezone.utc),
                    source_reported_total=corpus.source_reported_total,
                    retrieval_pool_limit=corpus.retrieval_pool_limit,
                    scoring_limit=corpus.scoring_limit,
                    warnings=corpus.warnings,
                    corpus=corpus_file,
                    overview=overview_file,
                    request_digest=signature,
                ),
                key,
            )
        except Exception:
            for artifact in written:
                self.files.delete(artifact)
            raise
        if record.id != identity:
            for artifact in written:
                self.files.delete(artifact)
            if record.request_digest != signature:
                raise AnalysisError("analysis_idempotency_conflict")
        return self.get(record.id)

    def _put(self, identity: str, name: str, content: str):
        return self.files.put(
            ArtifactWrite(
                run_id=identity,
                kind="bibliometrics",
                filename=name + ".json.gz",
                media_type="application/gzip",
                schema_version=1,
                role=ArtifactRole.AUDIT,
                visibility=ArtifactVisibility.INTERNAL,
                content=gzip.compress(content.encode("utf-8"), mtime=0),
            )
        )

    def _corpus(self, record: AnalysisRecord) -> MetricCorpus:
        return MetricCorpus.model_validate_json(
            gzip.decompress(self.files.read(record.corpus))
        )

    def get(self, identity: str) -> AnalysisDetail:
        record = self.store.get_analysis(identity)
        return AnalysisDetail(
            **record.model_dump(
                exclude={"corpus", "overview", "networks", "request_digest"}
            ),
            statistics=MetricOverview.model_validate_json(
                gzip.decompress(self.files.read(record.overview))
            ),
        )

    def network(self, identity: str, request: NetworkRequest) -> MetricNetwork:
        record = self.store.get_analysis(identity)
        key = digest(request.model_dump())
        if key in record.networks:
            return MetricNetwork.model_validate_json(
                gzip.decompress(self.files.read(record.networks[key]))
            )
        if len(record.networks) >= 20:
            raise AnalysisError("analysis_network_cache_full", 422)
        try:
            result = network(self._corpus(record), request)
        except NetworkTooDense as exc:
            raise AnalysisError("network_too_dense", 422) from exc
        artifact = self._put(
            identity, key + "-" + str(uuid4()), result.model_dump_json()
        )
        try:
            saved = self.store.cache_network(identity, key, artifact)
        except Exception:
            self.files.delete(artifact)
            raise
        if saved.id != artifact.id:
            self.files.delete(artifact)
        return result

    def export(self, identity: str, format: str) -> bytes:
        record = self.store.get_analysis(identity)
        corpus = self._corpus(record)
        if format == "json":
            return json.dumps(
                {
                    "analysis": self.get(identity).model_dump(mode="json"),
                    "corpus": corpus.model_dump(mode="json"),
                },
                ensure_ascii=False,
            ).encode("utf-8")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        fields = (
            "id",
            "title",
            "year",
            "venue",
            "provider",
            "doi",
            "authors",
            "keywords",
            "citations",
            "scored",
            "cas_tier",
            "cas_top",
            "jcr_quartile",
            "impact_factor",
            "ccf_rank",
        )
        writer.writerow(fields)
        for paper in corpus.papers:
            row = []
            for field in fields:
                value = getattr(paper, field)
                value = (
                    "; ".join(value)
                    if isinstance(value, list)
                    else ""
                    if value is None
                    else str(value)
                )
                row.append(
                    "'" + value
                    if value.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else value
                )
            writer.writerow(row)
        return output.getvalue().encode("utf-8-sig")

    def delete(self, identity: str) -> None:
        self._cleanup(self.store.mark_analysis_deleted(identity))

    def cleanup_deleted(self) -> None:
        for record in self.store.pending_analysis_deletions():
            self._cleanup(record)

    def _cleanup(self, record: AnalysisRecord) -> None:
        for artifact in (record.corpus, record.overview, *record.networks.values()):
            self.files.delete(artifact)
        self.store.purge_analysis(record.id)
