from __future__ import annotations

import asyncio
from html import unescape
import math
import re
from typing import Any

from paperseek_core.models import (
    CitationEdge,
    CitationSourceRequest,
    CitationSourceResult,
    PaperAuthor,
    PaperCitation,
    PaperIdentifiers,
    PaperKeywords,
    PaperLinks,
    PaperNames,
    PaperRecord,
    PaperSource,
)
from paperseek_core.sources.contracts import (
    SourceCapabilities,
    SourceError,
    SourceSearchMetadata,
    SourceSearchRequest,
    SourceSearchResult,
)
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi, reconstruct_abstract


class OpenAlexSource:
    name = "openalex"
    BASE_URL = "https://api.openalex.org/works"
    SELECT = ",".join(
        (
            "id",
            "doi",
            "title",
            "display_name",
            "publication_year",
            "publication_date",
            "type",
            "type_crossref",
            "authorships",
            "primary_location",
            "cited_by_count",
            "abstract_inverted_index",
            "open_access",
            "keywords",
            "concepts",
            "ids",
            "primary_topic",
            "referenced_works",
            "biblio",
        )
    )

    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        api_key: str = "",
        email: str = "",
    ) -> None:
        self.transport = transport
        self.api_key = str(api_key or "").strip()
        self.email = str(email or "").strip()
        lanes = ("relevance", "semantic", "impact", "recent") if self.api_key else (
            "relevance",
            "impact",
            "recent",
        )
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=lanes,
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "citations"),
            supports_abstracts=True,
            supports_citations=True,
            supports_citation_expansion=True,
            supports_pdf_links=True,
            supports_hard_field_filter=True,
            api_key="recommended",
            max_page_size=100,
            page_size_by_lane={"semantic": 50},
            single_page_lanes=("semantic",) if self.api_key else (),
            lane_concurrency=2,
            backoff_policy="3 attempts for transport, 429, and transient 5xx responses",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        if not query:
            raise SourceError(self.name, "OpenAlex search query is empty.")
        if request.lane == "semantic" and not self.api_key:
            raise SourceError(self.name, "OpenAlex semantic search requires an API key.", query=query)
        page_limit = self.capabilities.page_size(request.lane)
        page_size = max(1, min(int(request.limit or 10), page_limit))
        params: dict[str, Any] = {
            "per-page": page_size,
            "page": max(1, int(request.page or 1)),
            "select": self.SELECT,
            "search.semantic" if request.lane == "semantic" else "search": query,
        }
        field_ids = self._normalize_field_ids(request.field_ids)
        if field_ids and request.lane != "semantic":
            params["filter"] = f"primary_topic.field.id:{'|'.join(field_ids)}"
        if request.lane == "impact":
            params["sort"] = "cited_by_count:desc"
        elif request.lane == "recent":
            params["sort"] = "publication_date:desc"
        elif request.lane == "relevance":
            params["sort"] = "relevance_score:desc"
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["mailto"] = self.email
        headers = {"Accept": "application/json", "User-Agent": self._user_agent()}
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            self.BASE_URL,
            params=params,
            headers=headers,
            timeout=45,
            query=query,
        )
        if response.status_code >= 400:
            raise SourceError(
                self.name,
                f"OpenAlex returned HTTP {response.status_code}.",
                status=response.status_code, retry_details=response.extensions.get("paperseek_retry"),
                body=response.text[:1000],
                query=query,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(
                self.name,
                "OpenAlex returned a non-JSON response.",
                body=response.text[:1000],
                query=query,
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('meta'), dict) or not isinstance(payload.get('results'), list):
            raise SourceError(self.name, "OpenAlex returned an invalid search payload.", query=query)
        meta = payload['meta']
        hits = [self._to_record(work) for work in payload.get("results") or [] if isinstance(work, dict)]
        raw_count = len(hits) if request.lane == "semantic" else None
        if request.lane == "semantic" and field_ids:
            # The semantic endpoint rejects field filters. Apply the same hard
            # restriction locally to its bounded, single-page result set.
            hits = [record for record in hits if self._record_matches_field_ids(record, field_ids)]
        return SourceSearchResult(
            metadata=SourceSearchMetadata(
                total=int(meta.get("count") or 0),
                page=int(meta.get("page") or request.page),
                limit=int(meta.get("per_page") or page_size),
                raw_count=raw_count,
            ),
            hits=hits,
        )

    async def traverse_citations(self, request: CitationSourceRequest) -> CitationSourceResult:
        """Traverse bounded backward and forward OpenAlex citation relations."""
        per_seed = max(1, min(int(request.per_seed or 4), 10))
        max_records = max(1, min(int(request.max_records or 40), 500))
        depth = max(1, min(int(request.depth or 1), 3))
        field_ids = self._normalize_field_ids(request.field_ids)
        expanded: set[tuple[str, str, int]] = set()
        direction_budget = max(1, int(math.ceil(max_records * 0.7)))
        request_budget = max(1, max_records)
        direction_stats: dict[str, dict[str, int]] = {
            direction: {
                "budget": direction_budget,
                "requests": 0,
                "fetched": 0,
                "added": 0,
                "duplicates": 0,
                "filtered": 0,
            }
            for direction in ("backward", "forward")
        }
        depth_stats: dict[str, dict[str, int]] = {}
        graph = _CitationGraph(self, field_ids, depth, direction_stats, depth_stats)
        processed_plans = 0
        consecutive_empty_plans = 0
        stop_reason = ""

        def request_count() -> int:
            return sum(stats["requests"] for stats in direction_stats.values())

        plans = list(request.seed_plans)
        role_count = len({role for plan in plans for role in plan.role.split("+")})
        minimum_plans_before_yield_stop = min(len(plans), max(6, role_count * 2))
        for plan_index, plan in enumerate(plans, 1):
            if len(graph.records) >= max_records:
                stop_reason = "max_records"
                break
            if request_count() >= request_budget:
                stop_reason = "request_budget"
                break
            seed = plan.record
            seed_id = self._work_id(seed)
            if not seed_id:
                continue
            plan_output_before = len(graph.records)
            remaining_plans = max(1, len(plans) - plan_index + 1)
            plan_record_budget = max(1, int(math.ceil((max_records - len(graph.records)) / remaining_plans)))
            seed_uid = self._record_id(seed) or seed_id
            graph.add_node(seed, "seed")
            graph.add_node(seed, f"seed_{plan.role}", seed_uid)
            frontier = [seed]
            frontier_seen = {seed_uid}
            for layer in range(1, min(plan.depth, depth) + 1):
                next_frontier: list[PaperRecord] = []
                layer_output_before = len(graph.records)
                state = graph.depth_state(layer)
                for current in frontier:
                    if (
                        len(graph.records) >= max_records
                        or len(graph.records) - plan_output_before >= plan_record_budget
                        or request_count() >= request_budget
                    ):
                        break
                    current_id = self._work_id(current)
                    if not current_id:
                        continue
                    state["expanded_items"] += 1
                    current_uid = self._record_id(current) or current_id
                    jobs: list[tuple[str, Any]] = []
                    references = list((current.raw.get("referenced_works") or [])[:per_seed])
                    backward_key = (current_id, "backward", layer)
                    if (
                        references
                        and "backward" in plan.directions
                        and direction_stats["backward"]["added"] < direction_budget
                        and request_count() < request_budget
                        and backward_key not in expanded
                    ):
                        expanded.add(backward_key)
                        jobs.append(
                            (
                                "backward",
                                self._fetch_citation_works(
                                    references,
                                    attempts=request.request_attempts,
                                    timeout=request.request_timeout,
                                ),
                            )
                        )
                    forward_key = (current_id, "forward", layer)
                    if (
                        "forward" in plan.directions
                        and direction_stats["forward"]["added"] < direction_budget
                        and request_count() + len(jobs) < request_budget
                        and forward_key not in expanded
                    ):
                        expanded.add(forward_key)
                        jobs.append(
                            (
                                "forward",
                                self._fetch_forward_citations(
                                    current_id,
                                    per_seed,
                                    field_ids=field_ids,
                                    attempts=request.request_attempts,
                                    timeout=request.request_timeout,
                                ),
                            )
                        )
                    for direction, _ in jobs:
                        direction_stats[direction]["requests"] += 1
                    tasks: list[tuple[str, asyncio.Task[list[PaperRecord]]]] = []
                    async with asyncio.TaskGroup() as group:
                        for direction, job in jobs:
                            tasks.append((direction, group.create_task(job)))
                    for direction, task in tasks:
                        records = task.result()
                        direction_stats[direction]["fetched"] += len(records)
                        state["fetched"] += len(records)
                        for neighbor in records:
                            if (
                                len(graph.records) >= max_records
                                or len(graph.records) - plan_output_before >= plan_record_budget
                                or direction_stats[direction]["added"] >= direction_budget
                            ):
                                break
                            graph.append_neighbor(
                                seed_uid=seed_uid,
                                parent_uid=current_uid,
                                record=neighbor,
                                direction=direction,
                                layer=layer,
                                next_frontier=next_frontier,
                                frontier_seen=frontier_seen,
                            )
                    if len(graph.records) >= max_records or len(graph.records) - plan_output_before >= plan_record_budget:
                        break
                frontier = next_frontier
                if len(graph.records) == layer_output_before and layer < min(plan.depth, depth):
                    break
                if not frontier or len(graph.records) >= max_records or len(graph.records) - plan_output_before >= plan_record_budget:
                    break
            processed_plans += 1
            plan_added = len(graph.records) - plan_output_before
            consecutive_empty_plans = consecutive_empty_plans + 1 if plan_added == 0 else 0
            if len(graph.records) >= max_records:
                stop_reason = "max_records"
                break
            if request_count() >= request_budget:
                stop_reason = "request_budget"
                break
            if (
                processed_plans >= minimum_plans_before_yield_stop
                and consecutive_empty_plans >= 3
                and plan_index < len(plans)
            ):
                stop_reason = "low_yield"
                break

        frontier_state = {
            "planned_items": len(plans),
            "processed_items": processed_plans,
            "skipped_items": max(0, len(plans) - processed_plans),
            "request_count": request_count(),
            "request_budget": request_budget,
            "stop_reason": stop_reason or "completed",
            "direction_stats": direction_stats,
            "depth_stats": depth_stats,
        }
        return CitationSourceResult(
            records=graph.records,
            nodes=list(graph.nodes.values()),
            edges=graph.edges,
            frontier=frontier_state,
        )

    async def _fetch_citation_works(
        self,
        values: list[str],
        *,
        attempts: int,
        timeout: int,
    ) -> list[PaperRecord]:
        identifiers = list(dict.fromkeys(self._normalize_work_id(value) for value in values if self._normalize_work_id(value)))[:100]
        if not identifiers:
            return []
        params = self._base_params()
        params.update(
            {
                "filter": "openalex:" + "|".join(identifiers),
                "per-page": len(identifiers),
                "select": self.SELECT,
            }
        )
        payload = await self._citation_payload(params, "|".join(identifiers), attempts=attempts, timeout=timeout)
        records = [self._to_record(work) for work in payload.get("results") or [] if isinstance(work, dict)]
        by_id = {self._work_id(record): record for record in records}
        return [by_id[identifier] for identifier in identifiers if identifier in by_id]

    async def _fetch_forward_citations(
        self,
        seed_id: str,
        limit: int,
        *,
        field_ids: tuple[str, ...],
        attempts: int,
        timeout: int,
    ) -> list[PaperRecord]:
        filters = [f"cites:{seed_id}"]
        if field_ids:
            filters.append("primary_topic.field.id:" + "|".join(field_ids))
        params = self._base_params()
        params.update(
            {
                "filter": ",".join(filters),
                "sort": "cited_by_count:desc",
                "per-page": max(1, min(int(limit or 4), 10)),
                "select": self.SELECT,
            }
        )
        payload = await self._citation_payload(params, seed_id, attempts=attempts, timeout=timeout)
        return [self._to_record(work) for work in payload.get("results") or [] if isinstance(work, dict)]

    async def _citation_payload(
        self,
        params: dict[str, Any],
        query: str,
        *,
        attempts: int,
        timeout: int,
    ) -> dict[str, Any]:
        response, self.last_response_info = await self.transport.request(
            self.name,
            "GET",
            self.BASE_URL,
            params=params,
            headers={"Accept": "application/json", "User-Agent": self._user_agent()},
            timeout=max(5, min(int(timeout or 10), 30)),
            query=query,
            attempts=max(1, min(int(attempts or 1), 3)),
        )
        if response.status_code >= 400:
            raise SourceError(
                self.name,
                f"OpenAlex returned HTTP {response.status_code} during citation traversal.",
                status=response.status_code, retry_details=response.extensions.get("paperseek_retry"),
                body=response.text[:1000],
                query=query,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(
                self.name,
                "OpenAlex returned a non-JSON citation response.",
                body=response.text[:1000],
                query=query,
            ) from exc
        if not isinstance(payload, dict):
            raise SourceError(self.name, "OpenAlex returned an invalid citation payload.", query=query)
        return payload

    def _base_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["mailto"] = self.email
        return params

    @staticmethod
    def _normalize_work_id(value: Any) -> str:
        return str(value or "").strip().rstrip("/").split("/")[-1]

    @classmethod
    def _work_id(cls, record: PaperRecord) -> str:
        value = record.identifiers.openalex if record.identifiers and record.identifiers.openalex else record.uid
        return cls._normalize_work_id(value)

    @staticmethod
    def _record_id(record: PaperRecord) -> str:
        return str(
            record.uid
            or (record.identifiers.openalex if record.identifiers else "")
            or record.title
            or ""
        )

    @classmethod
    def _record_matches_field_ids(cls, record: PaperRecord, field_ids: tuple[str, ...]) -> bool:
        if not field_ids:
            return True
        field = ((record.raw.get("primary_topic") or {}).get("field") or {})
        return cls._normalize_work_id(field.get("id")) in field_ids

    def _user_agent(self) -> str:
        return f"paperseek/1.0 (mailto:{self.email})" if self.email else "paperseek/1.0"

    @staticmethod
    def _normalize_field_ids(values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for value in values or ():
            identifier = str(value or "").strip().rstrip("/").split("/")[-1]
            if identifier and identifier not in normalized:
                normalized.append(identifier)
        return tuple(normalized)

    def _to_record(self, work: dict[str, Any]) -> PaperRecord:
        openalex_id = work.get("id") or ""
        title = self._clean_text(work.get("title") or work.get("display_name") or "")
        location = work.get("primary_location") or {}
        source_obj = location.get("source") or {}
        landing_page = location.get("landing_page_url") or ""
        authors = []
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") or {}
            display = self._clean_text(author.get("display_name") or "")
            if display:
                authors.append(PaperAuthor(display_name=display, wos_standard=display))
        ids = work.get("ids") or {}
        work_type = work.get("type") or work.get("type_crossref") or ""
        citation_count = int(work.get("cited_by_count") or 0)
        return PaperRecord(
            uid=openalex_id or title,
            title=title,
            types=[work_type] if work_type else [],
            source_types=[work_type] if work_type else [],
            source=PaperSource(
                source_title=self._clean_text(source_obj.get("display_name") or ""),
                publish_year=work.get("publication_year"),
                volume=str((work.get("biblio") or {}).get("volume") or ""),
                issue=str((work.get("biblio") or {}).get("issue") or ""),
                pages=work.get("biblio") or None,
            ),
            names=PaperNames(authors=authors),
            links=PaperLinks(
                record=openalex_id or landing_page,
                landing_page=landing_page,
                pdf=location.get("pdf_url") or "",
            ),
            citations=[PaperCitation(db="OpenAlex", count=citation_count)] if citation_count else [],
            identifiers=PaperIdentifiers(
                doi=normalize_doi(work.get("doi") or ""),
                issn=str(source_obj.get("issn_l") or ""),
                eissn=next((str(value) for value in source_obj.get("issn") or [] if value and value != source_obj.get("issn_l")), ""),
                openalex=openalex_id,
                pmid=(ids.get("pmid") or "").replace("https://pubmed.ncbi.nlm.nih.gov/", ""),
            ),
            keywords=PaperKeywords(author_keywords=self._extract_keywords(work)),
            abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
            provider=self.name,
            raw=work,
        )

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = unescape(str(value or ""))
        return re.sub(r"\s+", " ", re.sub(r"\\[nrt]", " ", text)).strip()

    @staticmethod
    def _extract_keywords(work: dict[str, Any]) -> list[str]:
        terms: list[str] = []
        for item in work.get("keywords") or []:
            value = item.get("display_name") or item.get("keyword") or item.get("name")
            if value and value not in terms:
                terms.append(value)
        if not terms:
            for item in work.get("concepts") or []:
                value = item.get("display_name")
                if value and value not in terms:
                    terms.append(value)
        return terms[:10]


class _CitationGraph:
    """Collect nodes, provenance edges and duplicate statistics without I/O."""

    def __init__(self, source: OpenAlexSource, field_ids: tuple[str, ...], depth: int,
                 direction_stats: dict[str, dict[str, int]], depth_stats: dict[str, dict[str, int]]) -> None:
        self.source = source
        self.field_ids, self.depth = field_ids, depth
        self.direction_stats, self.depth_stats = direction_stats, depth_stats
        self.records: list[PaperRecord] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[CitationEdge] = []
        self.seen: set[str] = set()

    def depth_state(self, layer: int) -> dict[str, int]:
        return self.depth_stats.setdefault(
            str(layer),
            {"expanded_items": 0, "fetched": 0, "added": 0, "duplicates": 0, "filtered": 0},
        )


    def add_node(self, record: PaperRecord, role: str, seed_uid: str = "") -> None:
        node_id = self.source._record_id(record)
        if not node_id:
            return
        existing = self.nodes.get(node_id)
        if existing:
            if role not in existing["roles"]:
                existing["roles"].append(role)
            if seed_uid and seed_uid not in existing["seed_uids"]:
                existing["seed_uids"].append(seed_uid)
            return
        source = record.source
        self.nodes[node_id] = {
            "id": node_id,
            "title": record.title or "(no title)",
            "year": source.publish_year if source else None,
            "source": source.source_title if source else "",
            "citations": sum(int(citation.count or 0) for citation in record.citations),
            "roles": [role],
            "seed_uids": [seed_uid] if seed_uid else [],
        }


    def append_neighbor(
        self,
        *,
        seed_uid: str,
        parent_uid: str,
        record: PaperRecord,
        direction: str,
        layer: int,
        next_frontier: list[PaperRecord],
        frontier_seen: set[str],
    ) -> bool:
        state = self.depth_state(layer)
        if not self.source._record_matches_field_ids(record, self.field_ids):
            self.direction_stats[direction]["filtered"] += 1
            state["filtered"] += 1
            return False
        record_uid = self.source._record_id(record)
        if not record_uid:
            return False
        self.add_node(record, direction, seed_uid)
        self.edges.append(
            CitationEdge(
                seed_id=seed_uid,
                parent_id=parent_uid,
                record_id=record_uid,
                direction=direction,
                depth=layer,
            )
        )
        dedupe_key = record_uid.casefold()
        added = dedupe_key not in self.seen
        if added:
            self.seen.add(dedupe_key)
            self.records.append(record)
            self.direction_stats[direction]["added"] += 1
            state["added"] += 1
        else:
            self.direction_stats[direction]["duplicates"] += 1
            state["duplicates"] += 1
        if layer < self.depth and record_uid not in frontier_seen:
            frontier_seen.add(record_uid)
            next_frontier.append(record)
        return added
