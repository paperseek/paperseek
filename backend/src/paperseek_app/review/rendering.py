"""One citation order for the reading view and every export format."""
from __future__ import annotations

from paperseek_app.review.models import ReviewDetail
from paperseek_app.review.citations import CitationStyle, PAPER_MENTION, author_year_labels, citation_spans
from paperseek_core.review.evidence import evidence_index, strip_evidence_ids, validate_body
from paperseek_core.review.models import CitedParagraph, RenderedReview, RenderedSection, ReviewCitation, ReviewReference


def render_review(review: ReviewDetail, citation_style: CitationStyle = "numeric") -> RenderedReview:
    content = review.content
    rendered = RenderedReview(id=review.id, version=review.version, title=content.topic, language=content.language,
                             citation_style=citation_style)
    if content.outline is None:
        return rendered
    evidence = evidence_index(content.cards)
    papers = {paper.id: paper for paper in content.papers}
    bodies = {section.section_id: section for section in content.sections}
    referenced = {evidence[key].paper_id for plan in content.outline.sections if plan.id in bodies
                  for paragraph in bodies[plan.id].body.paragraphs for key in paragraph.evidence_ids if key in evidence}
    labels = author_year_labels([paper for paper in content.papers if paper.id in referenced])
    numbers: dict[str, int] = {}
    for plan in content.outline.sections:
        body = bodies.get(plan.id)
        if body is None:
            continue
        # A stale section may refer to its previous paper assignment; all citations
        # must still belong to this fixed corpus and its verified evidence.
        validate_body(body.body, content.cards, list(papers))
        paragraphs = []
        for paragraph in body.body.paragraphs:
            citations: dict[str, ReviewCitation] = {}
            cited_papers = {evidence[key].paper_id for key in paragraph.evidence_ids}
            for paper_id in PAPER_MENTION.findall(paragraph.text):
                if paper_id in cited_papers:
                    numbers.setdefault(paper_id, len(numbers) + 1)
            for key in paragraph.evidence_ids:
                paper_id = evidence[key].paper_id
                numbers.setdefault(paper_id, len(numbers) + 1)
                label = f"[{numbers[paper_id]}]" if citation_style == "numeric" else f"({', '.join(labels[paper_id])})"
                citation = citations.setdefault(paper_id, ReviewCitation(number=numbers[paper_id], paper_id=paper_id,
                                                                          evidence_ids=[], label=label))
                if key not in citation.evidence_ids:
                    citation.evidence_ids.append(key)
            ordered = sorted(citations.values(), key=lambda item: item.number)
            # Older drafts may carry evidence IDs in their prose; readers never see them.
            text = strip_evidence_ids(paragraph.text)
            spans, warnings, unplaced = citation_spans(text, ordered, content.language)
            paragraphs.append(CitedParagraph(text=text, citations=ordered, spans=spans, citation_warnings=warnings,
                                             unplaced=unplaced))
        rendered.sections.append(RenderedSection(id=plan.id, title=plan.title, level=2 if plan.parent_id else 1,
                                                revision=body.revision, validation="needs_revision" if any(
                                                    p.citation_warnings or p.unplaced for p in paragraphs) else body.validation,
                                                stale=body.stale, paragraphs=paragraphs, omitted=body.body.omitted))
    for paper_id, number in numbers.items():
        paper = papers[paper_id]
        year = labels[paper_id][1] if citation_style == "harvard" else str(paper.year or "")
        fields = [", ".join(paper.authors), f"({year})" if year else "", paper.title, paper.venue]
        if citation_style == "harvard" and not any(author.strip() for author in paper.authors):
            fields = [paper.title, f"({year})", paper.venue]
        rendered.references.append(ReviewReference(number=number, paper_id=paper_id, text=". ".join(f for f in fields if f),
                                                    doi=paper.doi, url=paper.url))
    if citation_style == "harvard":
        rendered.references.sort(key=lambda ref: (*[part.casefold() for part in labels[ref.paper_id]],
                                                  papers[ref.paper_id].title.casefold()))
    rendered.draft = bool(content.consistency_findings) or len(rendered.sections) != len(content.outline.sections) or any(
        section.stale or section.validation == "needs_revision" for section in rendered.sections)
    return rendered
