from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import Any

from paperseek_core.acceptance import from_paperhub, merge, title_key
from paperseek_core.models import PaperAuthor, PaperIdentifiers, PaperKeywords, PaperLinks, PaperNames, PaperRecord, PaperSource
from paperseek_core.sources.contracts import SourceCapabilities, SourceError, SourceSearchMetadata, SourceSearchRequest, SourceSearchResult
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openreview import OpenReviewClient


@dataclass
class PaperHubQuery:
    groups: list[list[str]] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    conference: str | None = None
    year: int | None = None
    after: int | None = None
    before: int | None = None


class PaperHubSource:
    name = "paperhub"
    MANIFEST_URL = "https://raw.githubusercontent.com/Yupu-Wang/paper-hub/main/data/manifest.json"
    RAW_BASE_URL = "https://raw.githubusercontent.com/Yupu-Wang/paper-hub/main/data"
    CACHE_TTL_SECONDS = 3600
    _lock = asyncio.Lock()
    _paper_cache: list[dict[str, Any]] | None = None
    _paper_cache_loaded_at = 0.0
    _paper_cache_manifest_version = ""
    _title_index: dict[str, dict[str, Any]] | None = None
    # OpenReview rows per query, so later pages of one search see the same union.
    _openreview_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def __init__(self, *, transport: AsyncHttpTransport, openreview: OpenReviewClient | None = None) -> None:
        self.transport = transport
        self.openreview = openreview
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance", "recent", "local_quality"),
            metadata_fields=("title", "names", "source", "abstract", "keywords"),
            supports_abstracts=True,
            api_key="not_required",
            max_page_size=100,
            backoff_policy="atomic concurrent shard refresh with one-hour complete-snapshot cache",
        )
        self.last_response_info: dict[str, Any] = {}

    @classmethod
    def reset_cache(cls) -> None:
        cls._paper_cache = None
        cls._paper_cache_loaded_at = 0.0
        cls._paper_cache_manifest_version = ""
        cls._title_index = None
        cls._openreview_cache = {}

    @classmethod
    def cached_acceptance(cls, title: str) -> dict[str, Any]:
        """Acceptance from an already loaded snapshot; never downloads one."""
        if cls._paper_cache is None:
            return {}
        if cls._title_index is None:
            cls._title_index = {title_key(paper.get("title")): paper for paper in cls._paper_cache if paper.get("title")}
        paper = cls._title_index.get(title_key(title))
        return from_paperhub(paper) if paper else {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "Paper Hub search query is empty.")
        papers = await self._load_papers(query)
        papers = self._union(papers, await self._openreview_rows(query))
        parsed = self._parse_query(query)
        scored = [(score, paper) for paper in papers if (score := self._match_score(paper, parsed)) is not None]
        if request.lane == "recent":
            scored.sort(key=lambda item: (int(item[1].get("year") or 0), item[0], item[1].get("conference") or ""), reverse=True)
        elif request.lane == "local_quality":
            scored.sort(key=lambda item: (item[0], self._conference_weight(item[1]), int(item[1].get("year") or 0)), reverse=True)
        else:
            scored.sort(key=lambda item: (item[0], int(item[1].get("year") or 0), item[1].get("conference") or ""), reverse=True)
        page_size = max(1, min(int(request.limit or 10), 100))
        offset = max(0, (int(request.page or 1) - 1) * page_size)
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=len(scored), page=max(1, request.page), limit=page_size),
            hits=[self._to_record(paper) for _, paper in scored[offset : offset + page_size]],
        )

    async def _openreview_rows(self, query: str) -> list[dict[str, Any]]:
        if self.openreview is None:
            return []
        cls = self.__class__
        cached = cls._openreview_cache.get(query)
        if cached and time.monotonic() - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1]
        try:
            rows = await self.openreview.search(query)
        except SourceError as exc:
            # OpenReview only widens Paper Hub; a block or outage leaves the snapshot results.
            self.last_response_info = {**self.last_response_info, "openreview_error": str(exc)}
            return []
        if len(cls._openreview_cache) >= 64:
            cls._openreview_cache.pop(next(iter(cls._openreview_cache)))
        cls._openreview_cache[query] = (time.monotonic(), rows)
        return rows

    @staticmethod
    def _union(papers: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Paper Hub plus OpenReview papers it lacks; a shared paper keeps Paper Hub's row and both sources."""
        if not extra:
            return papers
        by_title = {title_key(row["title"]): row for row in extra}
        merged = []
        for paper in papers:
            row = by_title.pop(title_key(paper.get("title")), None)
            merged.append({**paper, "acceptance": merge(from_paperhub(paper), row["acceptance"])} if row else paper)
        return merged + list(by_title.values())

    async def _load_papers(self, query: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        cls = self.__class__
        if cls._paper_cache is not None and now - cls._paper_cache_loaded_at < self.CACHE_TTL_SECONDS:
            return cls._paper_cache
        async with cls._lock:
            now = time.monotonic()
            if cls._paper_cache is not None and now - cls._paper_cache_loaded_at < self.CACHE_TTL_SECONDS:
                return cls._paper_cache
            stale = cls._paper_cache
            try:
                response, info = await self.transport.request(
                    self.name,
                    "GET",
                    self.MANIFEST_URL,
                    headers={"Accept": "application/json"},
                    timeout=45,
                    query=query,
                )
                if response.status_code >= 400:
                    raise SourceError(self.name, f"Paper Hub manifest returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
                try:
                    manifest = response.json()
                except ValueError as exc:
                    raise SourceError(self.name, "Paper Hub manifest returned non-JSON content.", body=response.text[:1000], query=query) from exc
                shards = self._manifest_shards(manifest, query)
                version = self._manifest_version(manifest, shards)
                if stale is not None and version == cls._paper_cache_manifest_version:
                    cls._paper_cache_loaded_at = now
                    self.last_response_info = {**info, "manifest_version": version, "shards": len(shards), "papers": len(stale), "cache_revalidated": True}
                    return stale
                semaphore = asyncio.Semaphore(8)

                async def download(shard: dict[str, Any]) -> list[dict[str, Any]]:
                    async with semaphore:
                        return await self._download_shard(shard, query)

                tasks: list[asyncio.Task[list[dict[str, Any]]]] = []
                async with asyncio.TaskGroup() as group:
                    for shard in shards:
                        tasks.append(group.create_task(download(shard)))
                chunks = [task.result() for task in tasks]
                papers = [paper for chunk in chunks for paper in chunk]
                expected = sum(int(shard.get("count") or 0) for shard in shards)
                if expected and len(papers) != expected:
                    raise SourceError(self.name, f"Paper Hub snapshot count mismatch: expected {expected}, received {len(papers)}.", query=query)
            except Exception as exc:
                if stale is not None:
                    cls._paper_cache_loaded_at = now
                    self.last_response_info = {"method": "GET", "url": self.MANIFEST_URL, "status": "stale_cache", "papers": len(stale), "refresh_error": str(exc)}
                    return stale
                if isinstance(exc, SourceError):
                    raise
                raise SourceError(self.name, f"Paper Hub snapshot loading failed: {exc}", query=query) from exc
            cls._paper_cache = papers
            cls._title_index = None
            cls._paper_cache_loaded_at = now
            cls._paper_cache_manifest_version = version
            self.last_response_info = {**info, "manifest_version": version, "shards": len(shards), "papers": len(papers), "download_workers": min(8, len(shards))}
            return papers

    async def _download_shard(self, shard: dict[str, Any], query: str) -> list[dict[str, Any]]:
        file_name = str(shard["file"])
        url = f"{self.RAW_BASE_URL}/{file_name}"
        response, _ = await self.transport.request(self.name, "GET", url, headers={"Accept": "application/json"}, timeout=90, query=query)
        if response.status_code >= 400:
            raise SourceError(self.name, f"Paper Hub shard {file_name} returned HTTP {response.status_code}.", status=response.status_code, retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, f"Paper Hub shard {file_name} returned non-JSON content.", body=response.text[:1000], query=query) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("papers"), list):
            raise SourceError(self.name, f"Paper Hub shard {file_name} did not contain a paper list.", query=query)
        papers = [paper for paper in payload["papers"] if isinstance(paper, dict)]
        expected = int(shard.get("count") or 0)
        if expected and len(papers) != expected:
            raise SourceError(self.name, f"Paper Hub shard {file_name} count mismatch: expected {expected}, received {len(papers)}.", query=query)
        return papers

    @staticmethod
    def _manifest_shards(manifest: Any, query: str) -> list[dict[str, Any]]:
        if not isinstance(manifest, dict) or not isinstance(manifest.get("shards"), list):
            raise SourceError("paperhub", "Paper Hub manifest did not contain a shard list.", query=query)
        shards = []
        for shard in manifest["shards"]:
            if not isinstance(shard, dict):
                raise SourceError("paperhub", "Paper Hub manifest contained an invalid shard entry.", query=query)
            file_name = str(shard.get("file") or "").strip().replace("\\", "/")
            if not file_name or file_name.startswith("/") or ".." in file_name.split("/"):
                raise SourceError("paperhub", "Paper Hub manifest contained an unsafe shard path.", query=query)
            shards.append({**shard, "file": file_name})
        if not shards:
            raise SourceError("paperhub", "Paper Hub manifest did not list any data shards.", query=query)
        return shards

    @staticmethod
    def _manifest_version(manifest: dict[str, Any], shards: list[dict[str, Any]]) -> str:
        return f"{str(manifest.get('built_at') or '').strip()}|{'|'.join(str(shard.get('file') or '') for shard in shards)}"

    @classmethod
    def _parse_query(cls, query: str) -> PaperHubQuery:
        parsed = PaperHubQuery()
        append = False
        stop = {"and", "the", "with", "for", "from", "paper", "papers", "study", "research"}
        for match in re.compile(r'(-?)"([^"]+)"|(\S+)').finditer(query or ""):
            quoted = bool(match.group(2))
            value = cls._normalize_token(match.group(2) if quoted else match.group(3))
            excluded = bool(match.group(1))
            if not value:
                continue
            if value.upper() == "OR":
                append = True
                continue
            if not quoted and value.startswith("-"):
                excluded, value = True, cls._normalize_token(value[1:])
            if value.startswith("+"):
                value = cls._normalize_token(value[1:])
            field_match = re.match(r"^(conference|conf|venue|year|after|before):(.+)$", value, flags=re.I)
            if field_match and not excluded:
                field_name = field_match.group(1).lower()
                field_value = cls._normalize_token(field_match.group(2)).strip('"')
                if field_name in {"conference", "conf", "venue"}:
                    parsed.conference = field_value.upper()
                elif year_match := re.search(r"(19|20)\d{2}", field_value):
                    setattr(parsed, field_name, int(year_match.group(0)))
                append = False
                continue
            term = value.lower()
            if not quoted and term in stop:
                append = False
            elif excluded:
                parsed.excludes.append(term)
                append = False
            elif append and parsed.groups:
                parsed.groups[-1].append(term)
                append = False
            else:
                parsed.groups.append([term])
        return parsed

    @staticmethod
    def _normalize_token(value: str) -> str:
        return re.sub(r"^[()]+|[()]+$", "", str(value or "").strip()).strip()

    def _match_score(self, paper: dict[str, Any], query: PaperHubQuery) -> int | None:
        conference = str(paper.get("conference") or "").upper()
        try:
            year = int(paper.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if query.conference and conference != query.conference or query.year and year != query.year or query.after and year <= query.after or query.before and year >= query.before:
            return None
        text = " ".join((str(paper.get("title") or ""), str(paper.get("abstract") or ""), " ".join(map(str, paper.get("keywords") or [])), " ".join(map(str, paper.get("authors") or [])), conference, str(year), str(paper.get("presentation") or ""))).lower()
        if any(term and term in text for term in query.excludes):
            return None
        score = 0
        for group in query.groups:
            best = max((self._score_term(paper, term) for term in group if term and term in text), default=0)
            if best <= 0:
                return None
            score += best
        return max(score, 1)

    @staticmethod
    def _score_term(paper: dict[str, Any], term: str) -> int:
        fields = (str(paper.get("title") or "").lower(), " ".join(map(str, paper.get("keywords") or [])).lower(), str(paper.get("abstract") or "").lower(), " ".join(map(str, paper.get("authors") or [])).lower(), f"{paper.get('conference') or ''} {paper.get('year') or ''} {paper.get('presentation') or ''}".lower())
        return sum(weight for value, weight in zip(fields, (8, 5, 3, 2, 4), strict=True) if term in value)

    @staticmethod
    def _conference_weight(paper: dict[str, Any]) -> int:
        return {"NEURIPS": 6, "ICML": 5, "ICLR": 5, "AAAI": 4, "NDSS": 4}.get(str(paper.get("conference") or "").upper(), 1)

    @staticmethod
    def _to_record(paper: dict[str, Any]) -> PaperRecord:
        paper_id = str(paper.get("id") or paper.get("url") or paper.get("title") or "")
        conference = str(paper.get("conference") or "")
        presentation = str(paper.get("presentation") or "")
        keywords = [str(value) for value in paper.get("keywords") or [] if value]
        for value in (conference, presentation):
            if value and value not in keywords:
                keywords.append(value)
        url = str(paper.get("url") or "")
        acceptance = paper.get("acceptance") or from_paperhub(paper)
        return PaperRecord(
            uid=paper_id or url,
            title=str(paper.get("title") or paper_id),
            types=["conference-paper"],
            source_types=[presentation] if presentation else ["conference-paper"],
            source=PaperSource(source_title=conference, publish_year=paper.get("year")),
            names=PaperNames(authors=[PaperAuthor(display_name=str(name)) for name in paper.get("authors") or [] if name]),
            links=PaperLinks(record=url, landing_page=url, pdf=str(paper.get("pdf") or "")),
            identifiers=PaperIdentifiers(),
            keywords=PaperKeywords(author_keywords=keywords),
            abstract=str(paper.get("abstract") or ""),
            provider="paperhub",
            raw={**paper, "acceptance": acceptance},
        )
