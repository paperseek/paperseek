"""Labels for final results: journal partitions, open impact figures and conference acceptance."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import re
from typing import Any

from paperseek_core.acceptance import from_paperhub, merge
from paperseek_core.journal_metrics import JournalMetricsTable, normalize_issns
from paperseek_core.models import PaperRecord
from paperseek_core.sources.contracts import SourceError
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.openreview import OpenReviewClient
from paperseek_core.sources.paperhub import PaperHubSource

# Venues worth an OpenReview title lookup: preprints and machine-learning conferences.
_REVIEWED_VENUES = re.compile(
    r"arxiv|corr|openreview|\b(iclr|neurips|nips|icml|colm|tmlr|corl|aistats|uai|emnlp|acl|naacl|coling|cvpr|iccv|"
    r"eccv|aaai|ijcai|kdd|www|sigir|learning representations|neural information processing|machine learning)\b",
    re.I,
)


class OpenAlexVenueMetrics:
    """Open journal impact (2-year mean citedness, h-index) from OpenAlex sources, by ISSN. Not an Impact Factor."""

    URL = "https://api.openalex.org/sources"

    def __init__(self, *, transport: AsyncHttpTransport, email: str = "", api_key: str = "") -> None:
        self.transport = transport
        self.email = email
        self.api_key = api_key

    async def lookup(self, issns: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(issns), 50):
            batch = issns[start:start + 50]
            params = {"filter": "issn:" + "|".join(batch), "per-page": 50,
                      "select": "id,display_name,issn_l,issn,summary_stats"}
            if self.email:
                params["mailto"] = self.email
            if self.api_key:
                params["api_key"] = self.api_key
            response, _ = await self.transport.request("openalex", "GET", self.URL, params=params, timeout=30,
                                                       query="journal metrics", attempts=2)
            if response.status_code >= 400:
                raise SourceError("openalex", f"OpenAlex sources returned HTTP {response.status_code}.",
                                  status=response.status_code)
            for source in response.json().get("results") or []:
                stats = source.get("summary_stats") or {}
                metrics = {"two_year_mean_citedness": stats.get("2yr_mean_citedness"), "h_index": stats.get("h_index"),
                           "i10_index": stats.get("i10_index"), "source": source.get("id") or ""}
                for issn in normalize_issns(source.get("issn_l"), *(source.get("issn") or [])):
                    found.setdefault(issn, metrics)
        return found


class ResultEnricher:
    def __init__(
        self,
        *,
        journals: Callable[[], Awaitable[JournalMetricsTable | None]] | None = None,
        openalex: OpenAlexVenueMetrics | None = None,
        openreview: OpenReviewClient | None = None,
        # OpenReview is paced at one request a second, so this bounds the added time per search.
        openreview_lookups: int = 15,
        concurrency: int = 3,
    ) -> None:
        self.journals = journals
        self.openalex = openalex
        self.openreview = openreview
        self.openreview_lookups = openreview_lookups
        self.concurrency = concurrency

    async def annotate(self, documents: list[PaperRecord]) -> dict[str, dict[str, Any]]:
        table = await self.journals() if self.journals else None
        acceptance = {document.uid: self._known_acceptance(document) for document in documents}
        journal = {document.uid: self._journal(document, table, acceptance[document.uid]) for document in documents}
        await self._open_impact(documents, journal)
        await self._openreview(documents, journal, acceptance)
        for document in documents:
            # A conference found on OpenReview gets its CCF class from the venue it was accepted at.
            if table and "ccf" not in journal[document.uid] and (venue := acceptance[document.uid].get("venue")):
                if ccf := table.ccf("", [venue]):
                    journal[document.uid] = {"journal": venue, "matched_by": "title", **journal[document.uid], "ccf": dict(ccf)}
        return {
            uid: {key: value for key, value in (("journal_metrics", journal[uid]), ("acceptance", acceptance[uid])) if value}
            for uid in journal
            if journal[uid] or acceptance[uid]
        }

    @staticmethod
    def _issns(document: PaperRecord) -> list[str]:
        identifiers = document.identifiers
        return normalize_issns(identifiers.issn, identifiers.eissn) if identifiers else []

    def _journal(self, document: PaperRecord, table: JournalMetricsTable | None, acceptance: dict[str, Any]) -> dict[str, Any]:
        if not table:
            return {}
        venue = document.source.source_title if document.source else ""
        return table.lookup(self._issns(document), venue, [acceptance["venue"]] if acceptance.get("venue") else [])

    @staticmethod
    def _known_acceptance(document: PaperRecord) -> dict[str, Any]:
        if document.provider == "paperhub":
            return dict(document.raw.get("acceptance") or from_paperhub(document.raw))
        return PaperHubSource.cached_acceptance(document.title)

    async def _open_impact(self, documents: list[PaperRecord], journal: dict[str, dict[str, Any]]) -> None:
        if self.openalex is None:
            return
        issns = list(dict.fromkeys(issn for document in documents for issn in self._issns(document)))
        if not issns:
            return
        try:
            found = await self.openalex.lookup(issns)
        except Exception:  # Open impact is optional; partitions and acceptance still apply.
            return
        for document in documents:
            metrics = next((found[issn] for issn in self._issns(document) if issn in found), None)
            if metrics:
                labels = journal[document.uid] or {"journal": document.source.source_title if document.source else "",
                                                   "matched_by": "issn"}
                journal[document.uid] = {**labels, "openalex": metrics}

    async def _openreview(self, documents: list[PaperRecord], journal: dict[str, dict[str, Any]],
                          acceptance: dict[str, dict[str, Any]]) -> None:
        if self.openreview is None:
            return
        candidates = [document for document in documents if self._worth_lookup(document, journal, acceptance)]
        candidates = candidates[:self.openreview_lookups]
        semaphore = asyncio.Semaphore(self.concurrency)
        blocked = asyncio.Event()

        async def lookup(document: PaperRecord) -> None:
            async with semaphore:
                if blocked.is_set():
                    return
                try:
                    found = await self.openreview.find(document.title)
                except Exception:
                    blocked.set()  # Anonymous access is sometimes challenged; stop instead of retrying each title.
                    return
                if found:
                    acceptance[document.uid] = merge(acceptance[document.uid], found)

        await asyncio.gather(*(lookup(document) for document in candidates))

    @staticmethod
    def _worth_lookup(document: PaperRecord, journal: dict[str, dict[str, Any]],
                      acceptance: dict[str, dict[str, Any]]) -> bool:
        if acceptance[document.uid].get("venue") or not document.title:
            return False
        venue = document.source.source_title if document.source else ""
        year = document.source.publish_year if document.source else None
        if year and year < 2013:
            return False
        if _REVIEWED_VENUES.search(venue):
            return True
        if any(platform in journal[document.uid] for platform in ("cas", "jcr")):
            return False
        arxiv = bool(document.identifiers and document.identifiers.arxiv)
        return document.provider == "arxiv" or arxiv or not venue.strip()


__all__ = ["OpenAlexVenueMetrics", "ResultEnricher"]
