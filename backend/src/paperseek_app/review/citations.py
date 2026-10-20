"""Format snapshot metadata and resolve inline source mentions to verified citations."""
from collections import defaultdict
import re
from typing import Literal

from paperseek_core.review.models import CitationSpan, ReviewCitation, ReviewPaper
from paperseek_core.review.evidence import PAPER_MENTION

CitationStyle = Literal["numeric", "harvard"]
_MENTION_GROUP = re.compile(r"[（(]\s*p_\d+(?:[、,，;；\s]+p_\d+)*\s*[)）]|" + PAPER_MENTION.pattern)


def _surname(name: str) -> str:
    name = name.strip()
    if "," in name:
        return name.split(",", 1)[0].strip()
    words = name.split()
    if not words:
        return ""
    start = len(words) - 1
    while start > 0 and words[start - 1].casefold() in {"de", "del", "da", "di", "van", "von", "der", "la", "le"}:
        start -= 1
    return " ".join(words[start:])


def author_year_labels(papers: list[ReviewPaper]) -> dict[str, tuple[str, str]]:
    """Disambiguate same author/year labels by title, independently of citation order."""
    grouped = defaultdict(list)
    for paper in papers:
        names = [_surname(author) for author in paper.authors if author.strip()]
        author = (names[0] if len(names) == 1 else " and ".join(names) if len(names) == 2
                  else names[0] + " et al." if names else f'“{paper.title}”')
        grouped[(author, str(paper.year) if paper.year else "n.d.")].append(paper)
    result = {}
    for (author, year), group in grouped.items():
        for i, paper in enumerate(sorted(group, key=lambda p: (p.title.casefold(), p.id))):
            # Excel-style alphabetic suffixes also cover more than 26 same-year works.
            suffix, index = "", i + 1
            if len(group) > 1:
                while index:
                    index, remainder = divmod(index - 1, 26)
                    suffix = chr(97 + remainder) + suffix
            result[paper.id] = (author, year + suffix)
    return result


def citation_spans(text: str, citations: list[ReviewCitation],
                   language: str) -> tuple[list[CitationSpan], list[str], list[ReviewCitation]]:
    """Spans of the prose with inline citations, warnings, and the citations no claim names.

    Citations the text does not place beside a claim are returned apart, so the reader is not
    led to take them as support for the paragraph's last sentence.
    """
    by_paper = {citation.paper_id: citation for citation in citations}
    spans, warnings, used = [], [], set()
    end = 0
    for match in _MENTION_GROUP.finditer(text):
        if match.start() > end:
            spans.append(CitationSpan(text=text[end:match.start()]))
        for i, mention in enumerate(PAPER_MENTION.findall(match.group())):
            if i:
                spans.append(CitationSpan(text="; "))
            if mention in by_paper:
                spans.append(CitationSpan(citation=by_paper[mention]))
                used.add(mention)
            else:
                spans.append(CitationSpan(text="〔文献标注待核对〕" if language == "zh" else "[citation needs checking]"))
                warning = (f"正文提及所选文献第 {int(mention[2:])} 篇，但本段未绑定该文献的摘要证据，请检查文献归属。"
                           if language == "zh" else
                           f"The text mentions selected paper {int(mention[2:])}, but this paragraph has no evidence from it. Check the attribution.")
                if warning not in warnings:
                    warnings.append(warning)
        end = match.end()
    if end < len(text):
        spans.append(CitationSpan(text=text[end:]))
    return spans, warnings, [citation for citation in citations if citation.paper_id not in used]
