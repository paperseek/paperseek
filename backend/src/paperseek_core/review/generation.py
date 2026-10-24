"""Generate, revise and validate review sections."""
from __future__ import annotations

import json
import re
from collections.abc import Callable

from pydantic import BaseModel, Field, create_model

from paperseek_core.ports import CancellationToken, LLMPort
from paperseek_core.errors import LLMRequestError
from paperseek_core.review.evidence import (
    PAPER_MENTION,
    CitationStyleError,
    citation_style_issues,
    evidence_index,
    strip_evidence_ids,
    validate_body,
    validate_mentions,
)
from paperseek_core.review.models import (
    MAX_REVIEW_PAPERS,
    PARAGRAPH_MAX_CHARS,
    PARAGRAPH_MAX_EVIDENCE,
    EvidenceCard,
    OutlineSection,
    PaperExclusion,
    ReviewModel,
    ReviewOutline,
    ReviewPaper,
    ReviewParagraph,
    SectionBody,
    SupportCheck,
    SupportFinding,
)
from paperseek_core.review.prompts import INLINE_CITATIONS, INLINE_CITATIONS_ZH, _say
from paperseek_core.review.structured import ReviewOutputError, structured_call


def cited_papers(body: SectionBody, cards: list[EvidenceCard]) -> set[str]:
    index = evidence_index(cards)
    return {index[key].paper_id for paragraph in body.paragraphs for key in paragraph.evidence_ids if key in index}


def reconcile_omissions(body: SectionBody, section: OutlineSection, cards: list[EvidenceCard],
                        *earlier: list[PaperExclusion]) -> SectionBody:
    """Keep one reason per planned paper that the text does not cite; drop the rest."""
    cited = cited_papers(body, cards)
    reasons: dict[str, PaperExclusion] = {}
    for item in [*body.omitted, *(entry for group in earlier for entry in group)]:
        if item.paper_id in section.paper_ids and item.paper_id not in cited and item.reason.strip():
            reasons.setdefault(item.paper_id, item)
    body.omitted = [reasons[paper] for paper in section.paper_ids if paper in reasons]
    return body


def unexplained(body: SectionBody, section: OutlineSection, cards: list[EvidenceCard]) -> set[str]:
    """Planned papers neither cited nor listed with a reason."""
    return set(section.paper_ids) - cited_papers(body, cards) - {item.paper_id for item in body.omitted}


async def complete_section_sources(llm: LLMPort, cancellation: CancellationToken, body: SectionBody,
                                   section: OutlineSection, cards: list[EvidenceCard], data: dict,
                                   validate: Callable[[SectionBody], object]) -> SectionBody:
    """Integrate planned papers the draft left out without a reason, or have the writer give one."""
    index = evidence_index(cards)
    cited = {index[key].paper_id for paragraph in body.paragraphs for key in paragraph.evidence_ids}
    missing = unexplained(body, section, cards)
    if not missing:
        return body
    targets: dict[int, list[str]] = {}
    for paper_id in sorted(missing):
        target = next(i for i, plan in enumerate(section.paragraphs) if paper_id in plan.paper_ids)
        targets.setdefault(target, []).append(paper_id)
    schema = create_model("ParagraphSourceRepair", __base__=ReviewModel,
                          omitted=(list[PaperExclusion], Field(default_factory=list, max_length=MAX_REVIEW_PAPERS)),
                          **{f"paragraph_{i + 1}": (ReviewParagraph, ...) for i in targets})

    def apply(value: BaseModel, *, require_coverage: bool = True) -> SectionBody:
        updated = body.model_copy(deep=True)
        for i in targets:
            updated.paragraphs[i] = getattr(value, f"paragraph_{i + 1}")
        updated.omitted = [*body.omitted, *value.omitted]
        validate(updated)
        reconcile_omissions(updated, section, cards)
        if require_coverage and (remaining := unexplained(updated, section, cards)):
            raise ValueError(f"Cite these papers with supporting evidence, or list each in omitted with a short reason: "
                             f"{sorted(remaining)}.")
        return updated

    relevant = missing | {index[key].paper_id for i in targets for key in body.paragraphs[i].evidence_ids}
    try:
        patch = await structured_call(
            llm, cancellation, "section_sources", _say(
                data["language"],
                "Revise ONLY the supplied paragraphs to integrate the missing studies' relevant findings. "
                "Return each requested paragraph under its exact field name, with text and evidence_ids. "
                "Keep the argument, existing supported findings and their source attribution. "
                "Synthesize the added study's contribution in the prose; do not just append a citation to an unrelated claim. "
                "If a missing study does not genuinely serve the paragraph's argument, leave it out and add "
                '{"paper_id","reason"} to omitted with a short reason in the requested language. '
                + INLINE_CITATIONS +
                "Use the supplied observations and exact evidence IDs. Keep each paragraph as one continuous paragraph, "
                "in the requested language. Keep within paragraph_limits; integrate through a shared argument rather than "
                "listing every study separately. Do not add headings, paragraphs or numeric citation markers.",
                "只修改所提供的段落，把缺失研究的相关发现融入其中。"
                "每个段落按其字段名原样返回，包含 text 和 evidence_ids。"
                "保留原有论证、已有依据的发现及其文献归属。"
                "在论述中综合新增研究的贡献，不要只是在一个无关的论断后面追加引用。"
                '如果某篇缺失研究并不能真正支撑该段论点，就不引用它，并在 omitted 中加入 {"paper_id","reason"}，用中文简要说明原因。'
                + INLINE_CITATIONS_ZH +
                "使用提供的观察和准确的证据编号。每段保持为一个连续的中文段落，"
                "遵守 paragraph_limits；通过共同的论证整合文献，而不是逐篇罗列。不要添加标题、段落或数字引用标记。"),
            {"topic": data["topic"], "language": data["language"], "review_focus": data["review_focus"],
             "section_goal": section.goal,
             "paragraph_limits": data["paragraph_limits"],
             "paragraphs": {f"paragraph_{i + 1}": {"goal": section.paragraphs[i].goal,
                            "missing_paper_ids": papers, "draft": body.paragraphs[i].model_dump()}
                            for i, papers in targets.items()},
             "abstract_evidence": [item for item in data["abstract_evidence"] if item["paper_id"] in relevant],
             "paper_metadata": [paper for paper in data["paper_metadata"] if paper["id"] in relevant]},
            schema, max_tokens=min(14000, max(3500, 2000 * len(targets))), validate=apply, language=data["language"],
        )
    except ReviewOutputError as exc:
        # Source coverage is an improvement to an already valid draft. If the repair cannot be
        # validated, keep the draft; the caller reports the omitted sources for review.
        if exc.result is None:
            return body
        try:
            partial = apply(exc.result, require_coverage=False)
        except ValueError:
            return body
        covered = {index[key].paper_id for paragraph in partial.paragraphs for key in paragraph.evidence_ids}
        return partial if cited <= covered else body
    except LLMRequestError:
        return body
    return apply(patch)


def text_size(text: str, unit: str) -> int:
    """Chinese length is counted in characters, English length in words."""
    return len(text.split()) if unit == "words" else len(text)


def paragraph_limit_issues(body: SectionBody, cards: list[EvidenceCard], limits: dict, *, slack: float = 1.0) -> list[int]:
    """Paragraphs exceeding the planned length or number of distinct cited papers."""
    index = evidence_index(cards)
    issues = []
    for i, paragraph in enumerate(body.paragraphs):
        papers = limits["papers"][i] if i < len(limits["papers"]) else max(limits["papers"], default=6)
        if text_size(paragraph.text, limits["unit"]) > limits["size"] * slack or len(
                {index[key].paper_id for key in paragraph.evidence_ids}) > papers:
            issues.append(i)
    return issues


async def condense_paragraphs(llm: LLMPort, cancellation: CancellationToken, body: SectionBody,
                              section: OutlineSection, cards: list[EvidenceCard], data: dict,
                              validate: Callable[[SectionBody], object]) -> tuple[SectionBody, list[int]]:
    """Shorten only over-limit paragraphs, one at a time; never fail the section for length."""
    limits = data["paragraph_limits"]
    issues = paragraph_limit_issues(body, cards, limits)
    if not issues:
        return body, []
    index = evidence_index(cards)
    updated = body.model_copy(deep=True)
    unresolved = []
    for i in issues:
        papers = limits["papers"][i] if i < len(limits["papers"]) else max(limits["papers"], default=6)

        def check(value: ReviewParagraph, i: int = i, papers: int = papers) -> None:
            trial = updated.model_copy(deep=True)
            trial.paragraphs[i] = value
            validate(trial)
            cited = {index[key].paper_id for key in value.evidence_ids}
            size, unit = text_size(value.text, limits["unit"]), limits["unit"]
            if size > limits["size"] * 1.1 or len(cited) > papers:
                raise ValueError(f"The paragraph has {size} {unit} and {len(cited)} cited papers; "
                                 f"keep at most {limits['size']} {unit} and {papers} papers.")

        draft = updated.paragraphs[i]
        cited = {index[key].paper_id for key in draft.evidence_ids}
        try:
            updated.paragraphs[i] = await structured_call(
                llm, cancellation, "section_condense", _say(
                    data["language"],
                    'Return {"text":"...","evidence_ids":["e_..."]} for ONE paragraph. '
                    "Condense the supplied paragraph to the stated limits by tightening wording and merging "
                    "related findings into a single argument. Keep its planned argument, the strongest findings and "
                    "their correct attribution. Remove repetition and peripheral detail first. If it cites more papers "
                    "than allowed, keep those most central to the planned argument and drop the evidence IDs of the "
                    "papers you remove, along with their paper IDs in the text. " + INLINE_CITATIONS +
                    "Do not add claims or numeric citation markers. Use the requested language.",
                    '为一个段落返回 {"text":"...","evidence_ids":["e_..."]}。'
                    "通过精简措辞、把相关发现合并为同一论证，将所提供的段落压缩到规定篇幅。保留计划中的论点、"
                    "最重要的发现及其正确的文献归属。优先删去重复和次要细节。如果引用的论文超过上限，"
                    "保留与计划论点最相关的论文，并删去被移除论文的证据编号及其在正文中的论文编号。"
                    + INLINE_CITATIONS_ZH + "不要添加新论断或数字引用标记。用中文撰写。"),
                {"language": data["language"], "goal": section.paragraphs[i].goal if i < len(section.paragraphs) else "",
                 "limits": {limits["unit"]: limits["size"], "papers": papers}, "paragraph": draft.model_dump(),
                 "abstract_evidence": [item for item in data["abstract_evidence"] if item["paper_id"] in cited]},
                ReviewParagraph, max_tokens=2500, validate=check, language=data["language"],
            )
        except ReviewOutputError:
            unresolved.append(i)
        else:
            kept = {index[key].paper_id for key in updated.paragraphs[i].evidence_ids}
            reason = "篇幅限制下从本段删去。" if data["language"] == "zh" else "Removed from this paragraph to keep it within the length limit."
            updated.omitted = [*updated.omitted, *(PaperExclusion(paper_id=paper, reason=reason) for paper in sorted(cited - kept))]
    reconcile_omissions(updated, section, cards)
    return updated, unresolved


async def write_section(
    llm: LLMPort, cancellation: CancellationToken, *, topic: str, language: str, length: str,
    outline: ReviewOutline, section: OutlineSection, cards: list[EvidenceCard],
    instruction: str = "", previous: SectionBody | None = None, summaries: list[dict] | None = None,
    papers: list[ReviewPaper] | None = None, paragraph_length: int | None = None,
    diagnostics: list[str] | None = None,
) -> tuple[SectionBody, list[SupportFinding]]:
    assigned = [card for card in cards if card.paper_id in section.paper_ids]
    evidence = [item.model_dump(include={"id", "paper_id", "category", "text", "quote"})
                for card in assigned for item in card.evidence]
    if len(json.dumps(evidence, ensure_ascii=False)) > 48000:
        # Cards already contain concise observations. Reuse every observation and ID;
        # the support check below still compares the resulting prose to the original quotes.
        evidence = [{key: value for key, value in item.items() if key != "quote"} for item in evidence]
    allowed = set(evidence_index(assigned))
    scoped_summaries = []
    for summary in summaries or []:
        paragraphs = [paragraph for paragraph in summary["paragraphs"]
                      if paragraph["evidence_ids"] and set(paragraph["evidence_ids"]) <= allowed]
        if paragraphs:
            scoped_summaries.append({**summary, "paragraphs": paragraphs})
    data = {"topic": topic, "language": language, "length": length,
            "review_focus": outline.focus,
            "outline": [{"id": s.id, "title": s.title, "goal": s.goal, "comparisons": s.comparisons}
                        for s in outline.sections],
            "section": section.model_dump(), "abstract_evidence": evidence,
            "paper_metadata": [{"id": p.id, "title": p.title, "authors": p.authors, "year": p.year}
                               for p in papers or [] if p.id in section.paper_ids],
            "user_revision_request": instruction, "previous_draft": previous.model_dump() if previous else None,
            "written_theme_summaries": scoped_summaries}
    tokens = {"short": 3500, "standard": 5000, "detailed": 7000}[length]
    unit = "characters" if language == "zh" else "words"
    # A user-chosen target is guidance; the condense pass only acts beyond 1.5× of it.
    data["paragraph_limits"] = {"size": max(round(paragraph_length * 1.5), paragraph_length + 80) if paragraph_length
                                else (900 if language == "zh" else 750), "unit": unit,
                                "papers": [max(6, len(p.paper_ids) + 2) for p in section.paragraphs]}
    if paragraph_length:
        data["paragraph_target"] = {unit: paragraph_length}
        expected = max(len(section.paragraphs), 1) * paragraph_length * (1.2 if unit == "characters" else 2.0)
        tokens = max(tokens, min(16000, round(expected * 1.3) + 1500))
    prompt = _say(language, (
        'Return {"paragraphs":[{"text":"...","evidence_ids":["e_..."]}]}. '
        "Every paragraph needs supporting supplied evidence IDs from the abstract evidence observations. "
        "Cite only the evidence needed for that paragraph, usually 1–6 items. Do not output numeric citation markers. "
        "The application formats paper IDs into citations. " + INLINE_CITATIONS +
        "Follow the approved paragraph plan in order: one prose paragraph per planned paragraph unless "
        "the explicit user revision request asks for a different structure. Use each plan's sources where relevant, "
        "and cover the assigned literature across the section rather than stacking citations indiscriminately. "
        "A planned paper that does not genuinely serve its paragraph may be left out: then add "
        '{"paper_id","reason"} to omitted, with a short, specific reason in the requested language '
        "(for example it duplicates a stronger study, addresses a different population, or the length limit). "
        "Every planned paper is either cited or listed in omitted. "
        "Build paragraphs around a substantive advance or analytical point, connect the findings, then explain "
        "their implication for the research question. A single-paper paragraph is appropriate for a distinct "
        "contribution; do not mechanically force two citations into every paragraph. "
        "Use concrete concepts, study settings and reported findings; avoid a string of 'A found..., B found...'. "
        "Begin with the substantive issue, not instructions to the reader or an announcement of the section. "
        "Let transitions follow the argument; avoid mechanical firstly/secondly/finally and a mini-conclusion "
        "at the end of every section. No internal headings, bullet lists, paragraph labels or bibliography. "
        "Match the section's distinct goal within the full outline; do not repeat neighbouring sections' arguments. "
        "Proactively explain the strongest supported contributions and why they matter; do not leave their value implicit. "
        "Discuss different performance patterns through each study's objectives, application setting and documented "
        "trade-offs. Preserve comparison facts, but avoid defeat language or invented compensating benefits. "
        "State evidence at the scope actually measured, using concrete results rather than defensive qualifiers. "
        "Do not add a list of reviewer objections or ritual self-criticism. End by reinforcing the key insight, "
        "without suddenly negating the preceding contribution. "
        "For overview/conclusion, synthesize written themes without adding unsupported claims. "
        + (f"Write each paragraph at about {paragraph_length} {unit} (paragraph_target), as the user chose; "
           "this replaces any per-section length target. Develop substance rather than filler to reach it. "
           if paragraph_length else
           "Target about 350–550/500–750/700–1000 English words or 600–900/900–1300/1200–1800 Chinese characters "
           "per section for short/standard/detailed. Prefer a shorter substantive section to filler. ") +
        "Do not repeatedly call attention to abstract-only evidence; qualify specific inferences where needed."
        " Follow paragraph_limits for text length and distinct cited papers. Do not move remaining literature "
        "into a giant final paragraph; develop each planned argument concisely."
    ), (
        '返回 {"paragraphs":[{"text":"...","evidence_ids":["e_..."]}]}。'
        "每段都需要来自摘要证据观察的、提供的证据编号作为支撑。"
        "每段只引用所需的证据，通常 1–6 条。不要输出数字引用标记。应用程序会把论文编号排版为引文。"
        + INLINE_CITATIONS_ZH +
        "按已批准的段落计划依次撰写：每个计划段落写成一个论述段落，除非明确的用户修改要求另有安排。"
        "在相关处使用各段计划的文献，并在全节中覆盖分配的文献，不要不加区分地堆砌引用。"
        '计划文献如果不能真正支撑所在段落，可以不引用，但要在 omitted 中加入 {"paper_id","reason"}，'
        "用中文简要、具体地说明原因（例如与证据更充分的研究重复、研究对象不同、篇幅所限）。每篇计划文献要么被引用，要么列入 omitted。"
        "每段围绕一项实质性进展或一个分析要点展开，把发现联系起来，再说明它们对研究问题的意义。"
        "独特的贡献可以单篇成段；不要机械地在每段强塞两条引用。"
        "使用具体的概念、研究情境和已报告的发现，避免写成“甲发现……，乙发现……”的串联。"
        "直接从实质问题写起，不要提示读者或预告本节内容。"
        "过渡随论证自然展开，避免机械的“首先/其次/最后”，也不要在每节末尾都加一段小结。"
        "不要使用内部标题、项目符号列表、段落标签或参考文献列表。"
        "紧扣本节在整体大纲中的独特目标，不要重复相邻各节的论点。"
        "主动阐明证据最充分的贡献及其重要性，不要让其价值停留在言外。"
        "用各研究的目标、应用情境和已报告的取舍讨论表现差异。保留比较事实，但避免失败式措辞或虚构的补偿性优势。"
        "按实际测量的范围陈述证据，使用具体结果，而不是防御性的限定语。"
        "不要罗列审稿意见或程式化的自我批评。结尾强化关键见解，不要突然否定前文的贡献。"
        "overview/conclusion 节应综合已写主题，不要添加没有依据的论断。"
        + (f"按用户选择，每段约 {paragraph_length} 字（paragraph_target），这取代每节的篇幅目标。"
           "靠充实内容而不是填充语达到篇幅。"
           if paragraph_length else
           "short/standard/detailed 每节分别以约 600–900/900–1300/1200–1800 字为宜。宁可篇幅短而有实质，也不要填充。") +
        "不要反复强调证据仅来自摘要；只在具体推断需要时加以限定。"
        "遵守 paragraph_limits 中的篇幅和被引论文数量。不要把剩余文献挪进一个庞大的最后一段；简洁地展开每个计划论点。"
    ))
    planned = bool(section.paragraphs and not instruction)
    # Paragraphs kept despite a citation-placement or language issue, so later passes can still
    # revise the other paragraphs; issues that remain are reported to the user at the end.
    accepted: set[str] = set()

    # Paragraph counts of accepted drafts whose extra paragraphs were merged; noted at the end.
    merged: list[int] = []

    def validate(value: SectionBody) -> SectionBody:
        # Real evidence IDs, attributed mentions and the approved paragraph count are required.
        # Inline placement and language are requested, but cannot fail an otherwise grounded draft.
        # Length and source-count limits are guidance, enforced afterwards by a focused condense pass.
        written = len(value.paragraphs)
        if planned and written > len(section.paragraphs):
            # A merge that would exceed a paragraph's limits leaves the count wrong, so the draft is repaired.
            merge_extra_paragraphs(value, len(section.paragraphs), language)
        validate_body(value, cards, section.paper_ids)
        validate_mentions(value, cards, language=language, exempt=accepted)
        if planned and len(value.paragraphs) != len(section.paragraphs):
            raise ValueError(f"Follow the approved plan: write {len(section.paragraphs)} prose paragraphs.")
        reconcile_omissions(value, section, cards)
        if written > len(value.paragraphs):
            merged.append(written)
        return value
    # The paragraph count is checked in validate, after extra paragraphs are merged, not by the schema.
    body_schema = SectionBody
    notes: dict[int, list[str]] = {}

    def note(paragraph: int, zh: str, en: str) -> None:
        notes.setdefault(paragraph, []).append(zh if language == "zh" else en)

    try:
        body = await structured_call(llm, cancellation, "section", prompt, data, body_schema,
                                     max_tokens=tokens, validate=validate, attempts=3, language=language)
    except ReviewOutputError as exc:
        # A draft that fails only placement or language rules is kept and marked for review;
        # one with repairable citation errors is cleaned up and kept rather than failing the job.
        if not isinstance(exc.result, SectionBody):
            raise
        if diagnostics is not None:
            diagnostics.append(f"section: {exc.reason}"[:2000])
        draft = salvage(exc.result, cards, section, planned, note, language)
        if draft is None:
            raise
        if planned and len(draft.paragraphs) < len(section.paragraphs):
            # Fewer paragraphs than planned: keep the grounded draft, skip the steps that assume one
            # paragraph per planned paragraph, and flag it for review.
            note(0, f"本节写成了 {len(draft.paragraphs)} 段，少于大纲规划的 {len(section.paragraphs)} 段，请核对或重写本节。",
                 f"This section has {len(draft.paragraphs)} paragraphs instead of the {len(section.paragraphs)} planned; "
                 "check it or rewrite the section.")
            planned = False
        try:
            validate(draft)
        except CitationStyleError:
            accepted.update(paragraph.text for paragraph in draft.paragraphs)
        except ValueError:
            # Mentions without evidence stay visible: the rendered text marks them for checking.
            accepted.update(paragraph.text for paragraph in draft.paragraphs)
        body = reconcile_omissions(draft, section, cards)
    weak_phrases = ("遗憾的是", "仅为", "仍明显落后", "效果有限", "作用有限", "效应有限", "微乎其微",
                    "unfortunately", "merely", "still lags behind", "limited effectiveness")
    editing_focus = [{"paragraph": i, "expressions_to_reframe": matches}
                     for i, paragraph in enumerate(body.paragraphs)
                     if (matches := [phrase for phrase in weak_phrases if phrase in paragraph.text.lower()])]
    drafted = list(body.omitted)

    try:
        body = await structured_call(
            llm, cancellation, "section_edit", _say(
                language,
                'Edit the supplied draft into a coherent scholarly synthesis. Return the rewritten section as '
            '{"paragraphs":[{"text":"...","evidence_ids":["e_..."]}]}. '
            "Lead with the strongest substantive contribution and explain its value. Build each paragraph around "
            "an analytical point, connecting studies rather than listing authors. Follow the review focus, "
            "section goal and approved paragraph progression. Remove repetitive recaps and stock transitions. "
            "Actively rewrite weakness-centred comparisons around the observed finding, study objective or documented "
            "trade-off. Retain actual comparison values. Replace 'only X'/'仅为X' with the measured value; "
            "remove '遗憾的是', '仍明显落后', '效果有限', '作用有限', '效应有限', '微乎其微', 'unfortunately', "
            "'limited effectiveness' and defeat language. Explain the concrete conditions where the finding matters. "
            "Do not keep a negative sentence and append a positive slogan. Do not invent a compensating advantage. "
            "The ending must reinforce the central insight, without adding a weakness list or negating the contribution. "
            "Replace unsupported inferences with precise source findings, not layers of caveats. "
            "Preserve the section's substantive evidence, comparison facts and correct source attribution. "
            "Every paragraph needs actual supplied evidence IDs; copy IDs exactly. Cover the assigned papers, "
            "keeping the draft's omitted list for planned papers it deliberately does not cite. "
            + INLINE_CITATIONS +
            "Keep the approved paragraph count and order unless the explicit user revision request changes them. "
            "Keep each paragraph within paragraph_limits. "
            "Use the requested language, continuous academic prose and no internal headings or bullet lists. "
            "The editing_focus field identifies draft paragraphs that need this treatment: rewrite their argument "
            "to remove the listed expressions, rather than substituting a synonym for the same negative framing. "
            "Return the complete edited text, not advice or a verbatim copy of a draft that violates this brief.",
            '把所提供的草稿编辑成连贯的学术综述。以 {"paragraphs":[{"text":"...","evidence_ids":["e_..."]}]} '
            "返回改写后的整节。"
            "以最有实质意义的贡献开篇并说明其价值。每段围绕一个分析要点展开，把研究联系起来，而不是罗列作者。"
            "遵循综述主线、本节目标和已批准的段落递进。删去重复的回顾和套话式过渡。"
            "主动把以不足为中心的比较改写为围绕观察到的发现、研究目标或已报告的取舍来论述。保留实际的比较数值。"
            "把“仅为X”改为直接陈述测量值；删去“遗憾的是”“仍明显落后”“效果有限”“作用有限”“效应有限”“微乎其微”"
            "等失败式措辞。说明该发现在哪些具体条件下有意义。"
            "不要保留负面句子再附加一句正面口号，也不要虚构补偿性优势。"
            "结尾必须强化核心见解，不要添加不足清单或否定前文贡献。"
            "用准确的文献发现替换没有依据的推断，而不是层层加限定语。"
            "保留本节的实质证据、比较事实和正确的文献归属。"
            "每段都需要实际提供的证据编号，编号须原样复制。覆盖分配给本节的论文，并保留草稿中有意不引用的计划文献及其 omitted 理由。"
            + INLINE_CITATIONS_ZH +
            "保持已批准的段落数量和顺序，除非明确的用户修改要求另有改动。每段遵守 paragraph_limits。"
            "使用连续的中文学术论述，不要使用内部标题或项目符号列表。"
            "editing_focus 字段标出了需要这样处理的草稿段落：改写其论证以去掉所列表述，而不是换个同义词保留同样的负面框架。"
            "草稿中夹用的英文单词应改为中文表述；术语只在首次出现时括注英文，后文不再重复括注。"
            "返回完整的编辑后文本，不要返回建议，也不要原样照抄违反上述要求的草稿。"),
            {**data, "draft": body.model_dump(), "editing_focus": editing_focus},
            body_schema, max_tokens=tokens, validate=validate, language=language,
        )
    except (ReviewOutputError, LLMRequestError):
        # Editing is optional polish: keep the validated draft and still run every check below.
        note(0, "润色步骤未返回有效结构，已保留通过校验的初稿。",
             "The editing pass returned an invalid structure; the validated draft was retained.")
    body = reconcile_omissions(body, section, cards, drafted)

    async def finish(value: SectionBody) -> SectionBody:
        if planned:
            value = await complete_section_sources(llm, cancellation, value, section, assigned, data, validate)
            value, unresolved = await condense_paragraphs(llm, cancellation, value, section, cards, data, validate)
            limit = data["paragraph_limits"]["size"]
            for i in unresolved:
                size = text_size(value.paragraphs[i].text, unit)
                note(i, f"本段 {size} 字，超出建议篇幅（{limit} 字以内），可编辑精简。",
                     f"This paragraph has {size} {unit}, over the suggested {limit}; edit to tighten it.")
        return value

    async def support(value: SectionBody) -> list[SupportFinding]:
        return await checked_support(llm, cancellation, value, cards, papers, section=section,
                                     language=language, diagnostics=diagnostics)

    body = await finish(body)
    findings = await support(body)
    if any(item.status == "unsupported" or (item.status == "needs_revision" and not item.reason.startswith(("未能完成", "The automatic")))
           for item in findings):
        try:
            repaired = await structured_call(
                llm, cancellation, "section_repair", prompt + _say(
                    language,
                    " Revise ONLY the identified unsupported claims; "
                    "replace them with precise evidence-backed findings, retaining a clear affirmative narrative. "
                    "Do not pad the revision with apologies, caveats, self-criticism or a new limitations paragraph. "
                    "Preserve supported text and its citations.",
                    "只修改 issues 中指出的无依据论断，用准确、有证据支撑的发现替换它们，并保持清晰、肯定的叙述。"
                    "不要用致歉、限定语、自我批评或新增的局限性段落填充修订。保留有依据的文字及其引用。"),
                {**data, "draft": body.model_dump(), "issues": [item.model_dump() for item in findings]},
                body_schema, max_tokens=tokens, validate=validate, attempts=3, language=language,
            )
        except (ReviewOutputError, LLMRequestError):
            # Keep the draft with its findings: the section is saved as needing review, not lost.
            note(0, "自动修订未能完成，已保留原文并标出需要核对的段落。",
                 "Automatic revision could not complete; the draft is kept with the paragraphs to review marked.")
        else:
            body = await finish(reconcile_omissions(repaired, section, cards, body.omitted))
            findings = await support(body)
    # Citations not placed beside a claim are listed under their paragraph when the review is shown.
    style_notes = {
        "grouped": ("本段引用集中在段末，未对应到具体论断，请核对。",
                    "This paragraph groups its citations at the end rather than beside each claim; check them."),
        "language": ("本段含有英文句子或题名，请改写为中文。",
                     "This paragraph contains English sentences or titles; rewrite them in Chinese."),
    }
    for paragraph, kind, _ in citation_style_issues(body, cards, language):
        if kind in style_notes:
            note(paragraph, *style_notes[kind])
    if merged:
        # Any accepted draft, edit or repair that was merged; noted once the final text is known.
        note(len(body.paragraphs) - 1, f"模型写成了 {max(merged)} 段，多出的段落已并入本段，请核对篇幅与衔接。",
             f"The model wrote {max(merged)} paragraphs; the extra ones were merged into this paragraph. Check its length and flow.")
    if planned and (missing := unexplained(body, section, cards)):
        for i, plan in enumerate(section.paragraphs):
            left = sorted(missing & set(plan.paper_ids))
            if left and i < len(body.paragraphs):
                note(i, f"计划文献未被引用，也没有说明原因：{', '.join(left)}。可编辑或重写本节补充。",
                     f"Planned sources neither cited nor explained: {', '.join(left)}. Edit or rewrite this section to include them.")
    for item in body.omitted:
        target = next((i for i, plan in enumerate(section.paragraphs) if item.paper_id in plan.paper_ids), 0)
        if item.check_incomplete:
            note(min(target, len(body.paragraphs) - 1), f"未能完成 {item.paper_id} 的排除理由检查，请人工核对。",
                 f"The automatic omission reason check for {item.paper_id} could not complete; review it manually.")
        elif item.questioned:
            note(min(target, len(body.paragraphs) - 1), f"不引用 {item.paper_id} 的理由可能不成立：{item.check_reason}",
                 f"The reason for not citing {item.paper_id} may not hold: {item.check_reason}")
    for paragraph, messages in notes.items():
        finding = next((item for item in findings if item.paragraph == paragraph), None)
        if finding is None:
            continue
        text = "\n".join(messages)
        finding.reason = (finding.reason + "\n" + text if finding.status != "supported" else text)[:1500]
        finding.status = "needs_revision"
    return body, findings


_NUMBERED_MARKER = re.compile(r"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]")


def merge_extra_paragraphs(body: SectionBody, count: int, language: str, *, drop_overflow: bool = False) -> int:
    """Fold paragraphs beyond the planned count into the last planned one, keeping their evidence.

    The merged paragraph must stay within the paragraph limits, or the section could not be saved
    and read back. A paragraph that does not fit is left out when ``drop_overflow`` is set; otherwise
    nothing is merged. Returns the number of extra paragraphs left out.
    """
    last, extra = body.paragraphs[count - 1], body.paragraphs[count:]
    joiner = "" if language == "zh" else " "
    text, evidence, left_out = last.text.rstrip(), list(last.evidence_ids), 0
    for paragraph in extra:
        joined = joiner.join([text, paragraph.text.strip()])
        keys = list(dict.fromkeys([*evidence, *paragraph.evidence_ids]))
        if len(joined) > PARAGRAPH_MAX_CHARS or len(keys) > PARAGRAPH_MAX_EVIDENCE:
            left_out += 1
            continue
        text, evidence = joined, keys
    if left_out and not drop_overflow:
        return left_out
    last.text, last.evidence_ids = text, evidence
    del body.paragraphs[count:]
    return left_out


def salvage(body: SectionBody, cards: list[EvidenceCard], section: OutlineSection, planned: bool,
            note: Callable[[int, str, str], None], language: str = "") -> SectionBody | None:
    """Clean a draft whose citations failed validation, rather than failing the whole job.

    Unknown evidence IDs are dropped, evidence IDs and numbered markers written into the prose
    are removed, and each change is noted on its paragraph. Paragraphs beyond the approved count
    are merged into the last planned one. A draft is unusable only when a paragraph keeps no real evidence.
    """
    index = evidence_index(cards)
    allowed = {key for key, item in index.items() if item.paper_id in section.paper_ids}
    result = body.model_copy(deep=True)
    if planned and len(result.paragraphs) > len(section.paragraphs):
        written, last = len(result.paragraphs), len(section.paragraphs) - 1
        left_out = merge_extra_paragraphs(result, len(section.paragraphs), language, drop_overflow=True)
        note(last, f"模型写成了 {written} 段，多出的段落已并入本段，请核对篇幅与衔接。",
             f"The model wrote {written} paragraphs; the extra ones were merged into this paragraph. Check its length and flow.")
        if left_out:
            note(last, f"另有 {left_out} 段并入后会超出段落上限，未予保留，可编辑或重写本节补充。",
                 f"{left_out} more paragraph(s) would have exceeded the paragraph limit and were left out; "
                 "edit or rewrite the section to add them.")
    for i, paragraph in enumerate(result.paragraphs):
        valid = list(dict.fromkeys(key for key in paragraph.evidence_ids if key in allowed))
        if not valid:
            return None
        if len(valid) != len(paragraph.evidence_ids):
            note(i, "已移除本段中不存在的证据编号，请核对引用。", "Evidence IDs that do not exist were removed; check this paragraph's citations.")
        paragraph.evidence_ids = valid
        text = _NUMBERED_MARKER.sub("", strip_evidence_ids(paragraph.text))
        if text != paragraph.text:
            note(i, "已从正文中删去证据编号或数字引用标记，请核对表述。",
                 "Evidence IDs or numbered markers were removed from the text; check the wording.")
            paragraph.text = text.strip() or paragraph.text
    for i, paragraph in enumerate(result.paragraphs):
        cited = {index[key].paper_id for key in paragraph.evidence_ids}
        if stray := sorted({match.group() for match in PAPER_MENTION.finditer(paragraph.text)} - cited):
            note(i, f"正文提到 {', '.join(stray)}，但本段没有它们的摘要证据，请核对文献归属。",
                 f"The text names {', '.join(stray)} without evidence from them in this paragraph; check the attribution.")
    return result


def _could_not_check(language: str) -> str:
    return ("未能完成自动证据检查，请人工核对本段引用。" if language == "zh"
            else "The automatic evidence check could not complete; review this paragraph's citations.")


async def checked_support(llm: LLMPort, cancellation: CancellationToken, body: SectionBody,
                          cards: list[EvidenceCard], papers: list[ReviewPaper] | None, *,
                          section: OutlineSection | None = None, language: str = "en",
                          diagnostics: list[str] | None = None) -> list[SupportFinding]:
    """Check the whole section; if that cannot complete, check paragraph by paragraph."""
    def record(exc: Exception, scope: str) -> None:
        if diagnostics is not None:
            reason = exc.reason if isinstance(exc, ReviewOutputError) else getattr(exc, "code", type(exc).__name__)
            diagnostics.append(f"support_check {scope}: {reason}"[:2000])

    try:
        return await check_support(llm, cancellation, body, cards, papers, language=language, section=section)
    except (ReviewOutputError, LLMRequestError) as exc:
        record(exc, "section")
        # A missing omission verdict must not discard valid paragraph checks or rewrite the prose.
        if (isinstance(exc, ReviewOutputError) and isinstance(exc.result, SupportCheck)
                and sorted(item.paragraph for item in exc.result.findings) == list(range(len(body.paragraphs)))):
            return exc.result.findings
    findings = []
    for i, paragraph in enumerate(body.paragraphs):
        try:
            [finding] = await check_support(llm, cancellation, SectionBody(paragraphs=[paragraph]), cards, papers,
                                            language=language)
            findings.append(finding.model_copy(update={"paragraph": i}))
        except (ReviewOutputError, LLMRequestError, ValueError) as exc:
            record(exc, f"paragraph {i}")
            findings.append(SupportFinding(paragraph=i, status="needs_revision", reason=_could_not_check(language)))
    return findings


async def check_support(llm: LLMPort, cancellation: CancellationToken,
                        body: SectionBody, cards: list[EvidenceCard],
                        papers: list[ReviewPaper] | None = None, *, language: str = "en",
                        section: OutlineSection | None = None) -> list[SupportFinding]:
    """Check each paragraph against its sources, and each omission reason against the omitted paper.

    Omission results are recorded on ``body.omitted`` (questioned flag and check reason).
    """
    index = evidence_index(cards)
    abstracts = {paper.id: paper.abstract for paper in papers or []}
    omitted = [item for item in body.omitted if item.paper_id in abstracts] if section else []
    for item in body.omitted:
        item.questioned, item.check_incomplete, item.check_reason = False, True, ""
    goals = {paper: plan.goal for plan in (section.paragraphs if section else []) for paper in plan.paper_ids}

    def validate(check: SupportCheck) -> None:
        if sorted(item.paragraph for item in check.findings) != list(range(len(body.paragraphs))):
            raise ValueError("Check each paragraph exactly once, using zero-based indices.")
        if sorted(item.paper_id for item in check.omissions) != sorted(item.paper_id for item in omitted):
            raise ValueError("Check each supplied omitted paper exactly once, with no extra IDs: "
                             + ", ".join(item.paper_id for item in omitted))
        if any(not item.reason.strip() for item in check.omissions):
            raise ValueError("Give a nonempty reason for every omission verdict, including accepted ones.")

    check = await structured_call(
        llm, cancellation, "support_check",
        'Return {"findings":[{"paragraph":0,"status":"supported","reason":"..."}],'
        '"omissions":[{"paper_id":"p_001","status":"accepted","reason":"..."}]}. '
        "Check every paragraph against its cited papers' quotes AND supplied full abstracts. "
        "Status is supported, needs_revision or unsupported. A claim supported elsewhere in the same cited "
        "paper's abstract is valid even if the selected excerpt is incomplete. Do not use uncited papers. "
        "Check numeric conditions, causality, scope, cross-paper comparisons and invented field-wide gaps. "
        "Check each substantive clause: a paragraph needs revision if ANY claim exceeds its evidence, even "
        "when the remaining claims are supported. In particular, flag invented mechanisms, necessity, sufficiency, "
        "superiority and claimed improvements on outcomes the abstracts did not evaluate. The topic itself is not evidence. "
        "Grouping complementary findings and comparing their explicit settings is valid synthesis and need not "
        "be stated verbatim in a single source. Different populations or measures are not direct contradictions. "
        "Distinguish a review's organizing interpretation, conceptual link or 'mechanism clue' from a claim "
        "that a causal mechanism was demonstrated. Accept interpretations grounded in the reported associations; "
        "do not demand a new experiment for thematic synthesis. Reject invented effects, untested stronger/weaker "
        "comparisons and an association explicitly recast as a demonstrated causal effect. "
        "Do not require every result in a cited abstract to be repeated. For a needed revision, identify the "
        "specific erroneous clause and the conflicting or missing source fact, not merely that it is interpretive. "
        "An accurately attributed author's interpretation or policy recommendation is supported when present "
        "in the source: do not demand an experiment proving a recommendation, or mistake 'the authors propose' "
        "for an empirical effectiveness claim. Evaluate factual support, not whether the prose includes enough caveats. "
        "Do not request generic limitations, hedging or self-criticism as a condition of being supported. "
        "A real citation alone does not mean its claims are supported. Do not add claims. "
        "Keep each reason concise, at most 500 characters. For supported paragraphs, a brief confirmation suffices. "
        "omitted_papers lists planned papers the section chose not to cite, with the writer's reason, the goal of the "
        "paragraph it was planned for, and its abstract. Return one omissions entry with a nonempty reason for each. Mark it questioned ONLY "
        "when the abstract plainly provides evidence for that paragraph goal AND the reason claims the paper is "
        "irrelevant, off-topic or lacks evidence. Accept reasons about duplication, a stronger or more direct study, "
        "a different population, setting or measure, a narrower scope, or the length limit, and accept whenever "
        "uncertain: leaving a paper out is an editorial choice, not an error. "
        + ("Write every reason in Chinese." if language == "zh" else "Write every reason in English."),
        {"paragraphs": [{"index": i, "text": paragraph.text,
                         "abstract_quotes": [{"paper_id": index[key].paper_id, "quote": index[key].quote}
                                             for key in paragraph.evidence_ids if key in index],
                         "cited_paper_abstracts": [{"paper_id": paper_id, "abstract": abstracts[paper_id]}
                                                   for paper_id in sorted({index[key].paper_id for key in paragraph.evidence_ids
                                                                           if key in index})
                                                   if paper_id in abstracts]}
                        for i, paragraph in enumerate(body.paragraphs)],
         "omitted_papers": [{"paper_id": item.paper_id, "reason": item.reason, "planned_goal": goals.get(item.paper_id, ""),
                             "abstract": abstracts[item.paper_id]} for item in omitted]},
        SupportCheck, max_tokens=min(24000, 4096 + len(omitted) * 80), validate=validate, language=language,
    )
    verdicts = {item.paper_id: item for item in check.omissions}
    for item in body.omitted:
        verdict = verdicts.get(item.paper_id)
        item.check_incomplete = verdict is None
        item.questioned = bool(verdict and verdict.status == "questioned")
        item.check_reason = verdict.reason[:600] if item.questioned and verdict else ""
    return check.findings
