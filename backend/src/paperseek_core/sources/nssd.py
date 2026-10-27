"""National Social Sciences Database (国家哲学社会科学文献中心, ncpssd.cn): Chinese social sciences and humanities.

POST https://www.ncpssd.cn/searchHandler/search, form fields ``search``, ``pageNum``, ``pageSize``,
``sort``; no key. The search expression addresses field codes: title IKTE, subject terms IKST,
abstract IKSE. PaperSeek writes plain Boolean queries (``(乡村振兴 OR 农村振兴) AND 数字治理``) and
each term becomes ``(IKTE="t" OR IKST="t" OR IKSE="t")``; a term spanning two fields would
otherwise be missed when its words are split across title and subject terms.
"""

from __future__ import annotations

import re
from typing import Any

from paperseek_core.models import (
    PaperAuthor,
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
from paperseek_core.sources.normalization import normalize_doi

_TOKEN = re.compile(r'"([^"]*)"|“([^”]*)”|(\()|(\))|([^\s()"“”]+)')
_ISSN = re.compile(r"^\d{4}-\d{3}[\dXx]$")
_AFFILIATION = re.compile(r"[\[［][^\]］]*[\]］]")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def nssd_expression(query: str) -> str:
    """A Boolean query of terms, as NSSD's field-coded expression. Adjacent terms are ANDed."""
    parts: list[str] = []
    previous = ""
    for match in _TOKEN.finditer(query or ""):
        phrase = match.group(1) if match.group(1) is not None else match.group(2)
        word = match.group(5)
        if match.group(3):
            token, kind = "(", "open"
        elif match.group(4):
            token, kind = ")", "close"
        elif phrase is None and word and word.upper() in {"AND", "OR", "NOT"}:
            token, kind = word.upper(), "operator"
        else:
            term = re.sub(r'["\\]', "", (phrase if phrase is not None else word or "")).strip()
            if not term:
                continue
            token, kind = f'(IKTE="{term}" OR IKST="{term}" OR IKSE="{term}")', "term"
        if kind in {"term", "open"} and previous in {"term", "close"}:
            parts.append("AND")
        if kind == "operator" and token == "NOT" and previous in {"term", "close"}:
            parts.append("AND")
        parts.append(token)
        previous = kind
    return " ".join(parts)


def _authors(value: Any) -> list[str]:
    text = _AFFILIATION.sub("", str(value or ""))
    names = [name.strip() for name in re.split(r"[;；,，]", text) if name.strip()]
    return list(dict.fromkeys(names))


class NssdSource:
    name = "nssd"
    BASE_URL = "https://www.ncpssd.cn/searchHandler/search"

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self.transport = transport
        self.capabilities = SourceCapabilities(
            source=self.name,
            lanes=("relevance",),
            metadata_fields=("title", "names", "source", "identifiers", "abstract", "keywords"),
            supports_abstracts=True,
            api_key="not_required",
            max_page_size=100,
            max_lane_limit=500,
            backoff_policy="3 attempts for transport, 429, and transient 5xx responses",
        )
        self.last_response_info: dict[str, Any] = {}

    async def search(self, request: SourceSearchRequest) -> SourceSearchResult:
        query = request.query.strip()
        expression = nssd_expression(query)
        if not expression:
            raise SourceError(self.name, "NSSD search query is empty.")
        page_size = max(1, min(int(request.limit or 20), 100))
        form = {"search": expression, "pageNum": max(1, int(request.page or 1)), "pageSize": page_size, "sort": ""}
        response, self.last_response_info = await self.transport.request(
            self.name, "POST", self.BASE_URL, data=form, timeout=45, query=query,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest", "User-Agent": _USER_AGENT},
        )
        if response.status_code >= 400:
            raise SourceError(self.name, f"NSSD returned HTTP {response.status_code}.", status=response.status_code,
                              retry_details=response.extensions.get("paperseek_retry"), body=response.text[:1000], query=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "NSSD returned a non-JSON response.", body=response.text[:1000], query=query) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("rows") or [], list):
            raise SourceError(self.name, "NSSD returned an invalid search payload.", body=str(payload)[:1000], query=query)
        hits = [record for row in data.get("rows") or [] if isinstance(row, dict) and (record := self._to_record(row))]
        return SourceSearchResult(
            metadata=SourceSearchMetadata(total=int(data.get("total") or 0), page=max(1, request.page), limit=page_size),
            hits=hits,
        )

    @staticmethod
    def _to_record(row: dict[str, Any]) -> PaperRecord | None:
        identity, title = str(row.get("id") or "").strip(), str(row.get("title") or "").strip()
        if not identity or not title:
            return None
        year_text = str(row.get("years") or "")
        year = int(year_text[:4]) if year_text[:4].isdigit() else None
        issn = str(row.get("issn") or "").strip()
        keywords = [term.strip() for term in re.split(r"[;；]|\s+", str(row.get("subject") or "")) if term.strip()]
        kind = str(row.get("type") or "").strip()
        url = f"https://www.ncpssd.cn/Literature/articleinfo?id={identity}&type=journalArticle"
        return PaperRecord(
            uid=f"nssd:{identity}",
            title=title,
            types=["journal-article"],
            source_types=[kind] if kind else [],
            source=PaperSource(source_title=str(row.get("cbw_name") or "").strip(), publish_year=year),
            names=PaperNames(authors=[PaperAuthor(display_name=name) for name in _authors(row.get("creator"))]),
            links=PaperLinks(record=url, landing_page=url),
            # 集刊 rows carry an ISBN in the ISSN slot; only a real ISSN is kept.
            identifiers=PaperIdentifiers(doi=normalize_doi(str(row.get("doi") or "")), issn=issn if _ISSN.match(issn) else ""),
            keywords=PaperKeywords(author_keywords=list(dict.fromkeys(keywords))),
            abstract=str(row.get("remark") or "").strip(),
            provider="nssd",
            raw={key: row.get(key) for key in ("id", "title", "creator", "cbw_name", "years", "issn", "subject", "range",
                                                "type", "doi", "remark")},
        )


__all__ = ["NssdSource", "nssd_expression"]
