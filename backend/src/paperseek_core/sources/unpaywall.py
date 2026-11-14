"""Unpaywall lookup of a legal open-access copy by DOI (https://unpaywall.org/products/api).

One DOI per request, identified by a contact email instead of a key. Only links are read;
PaperSeek does not download the copy.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from paperseek_core.sources.contracts import SourceError
from paperseek_core.sources.http import AsyncHttpTransport
from paperseek_core.sources.normalization import normalize_doi


def open_access(work: dict[str, Any]) -> dict[str, Any]:
    """The copy Unpaywall ranks best, with a direct PDF from any other copy when the best one has none."""
    if not work.get("is_oa"):
        return {}
    locations = [item for item in [work.get("best_oa_location"), *(work.get("oa_locations") or [])]
                 if isinstance(item, dict)]
    if not locations:
        return {}
    best = locations[0]
    chosen = next((item for item in locations if item.get("url_for_pdf")), best)
    found = {
        "status": work.get("oa_status") or "",
        "pdf_url": chosen.get("url_for_pdf") or "",
        "url": chosen.get("url_for_landing_page") or chosen.get("url") or "",
        "version": chosen.get("version") or "",
        "license": chosen.get("license") or "",
        "host_type": chosen.get("host_type") or "",
    }
    return {key: value for key, value in found.items() if value} if found["pdf_url"] or found["url"] else {}


class UnpaywallClient:
    name = "unpaywall"
    URL = "https://api.unpaywall.org/v2/"

    def __init__(self, *, transport: AsyncHttpTransport, email: str) -> None:
        self.transport = transport
        self.email = email

    async def find(self, doi: str) -> dict[str, Any]:
        doi = normalize_doi(doi)
        response, _ = await self.transport.request(
            self.name, "GET", self.URL + quote(doi, safe="/"), params={"email": self.email},
            headers={"Accept": "application/json"}, timeout=20, query=doi, attempts=2,
        )
        if response.status_code == 404:  # A DOI Unpaywall does not index.
            return {}
        if response.status_code >= 400:
            raise SourceError(self.name, f"Unpaywall returned HTTP {response.status_code}.",
                              status=response.status_code, query=doi)
        try:
            work = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "Unpaywall returned an unexpected body.", query=doi) from exc
        return open_access(work) if isinstance(work, dict) else {}


__all__ = ["UnpaywallClient", "open_access"]
