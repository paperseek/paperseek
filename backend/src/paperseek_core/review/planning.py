"""Extract verified evidence and plan grounded review outlines."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, create_model, model_validator

from paperseek_core.errors import LLMRequestError
from paperseek_core.ports import CancellationToken, LLMPort
from paperseek_core.review.evidence import (
    validate_cards,
    validate_outline,
)
from paperseek_core.review.models import (
    EvidenceBatch,
    EvidenceCard,
    PLANNED_PAPERS_PER_PARAGRAPH,
    OutlineSection,
    PaperExclusion,
    ParagraphPlan,
    ReviewModel,
    ReviewOutline,
    ReviewPaper,
)
from paperseek_core.review.prompts import _say
from paperseek_core.review.structured import ReviewOutputError, structured_call


class _PlannedSection(OutlineSection):
    # Generated paragraphs are the source of truth; the UI still edits the full outline.
    paper_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def collect_papers(self) -> _PlannedSection:
        self.paper_ids = list(dict.fromkeys(paper for plan in self.paragraphs for paper in plan.paper_ids))
        return self


async def extract_evidence(llm: LLMPort, cancellation: CancellationToken,
                           papers: list[ReviewPaper], language: str, signature: str,
                           diagnostics: list[str] | None = None) -> list[EvidenceCard]:
    dropped: list[str] = []

    def report() -> None:
        if dropped and diagnostics is not None:
            diagnostics.append("Dropped unverifiable quotes: " + "; ".join(dropped[:8]))

    try:
        batch = await _extract_batch(llm, cancellation, papers, language, signature)
    except ReviewOutputError as exc:
        if diagnostics is not None:
            diagnostics.append(exc.reason)
        if not isinstance(exc.result, EvidenceBatch) and len(papers) == 1:
            raise
        candidates = exc.result.model_dump()["cards"] if isinstance(exc.result, EvidenceBatch) else []
        if not candidates:
            # A schema error in one card must not discard other complete, independently valid cards.
            try:
                raw = json.loads(exc.output)
            except ValueError:
                raw = {}
            if isinstance(raw, dict) and isinstance(raw.get("cards"), list):
                candidates = raw["cards"]
        # One bad quote must not discard independently verified papers in the batch.
        cards = []
        for paper in papers:
            matches = [card for card in candidates if isinstance(card, dict) and card.get("paper_id") == paper.id]
            if len(matches) != 1:
                continue
            try:
                cards.extend(validate_cards(EvidenceBatch(cards=matches), [paper], signature, dropped))
            except ValueError:
                continue
        # Retry only missing papers in isolation, with the same strict quote validation.
        # A failed batch must not force the user to rerun all successfully extracted papers.
        if len(papers) > 1:
            ready = {card.paper_id for card in cards}

            async def recover(paper: ReviewPaper) -> None:
                try:
                    recovered = await _extract_batch(llm, cancellation, [paper], language, signature)
                    cards.extend(validate_cards(recovered, [paper], signature, dropped))
                except (ReviewOutputError, LLMRequestError) as failure:
                    if diagnostics is not None:
                        reason = failure.reason if isinstance(failure, ReviewOutputError) else failure.code
                        diagnostics.append(f"{paper.id}: {reason}")

            # One at a time: this batch already holds one of the review's concurrent slots,
            # so parallel retries would exceed the overall limit.
            for paper in papers:
                if paper.id not in ready:
                    await recover(paper)
        report()
        return sorted(cards, key=lambda card: next(i for i, paper in enumerate(papers) if paper.id == card.paper_id))
    cards = validate_cards(batch, papers, signature, dropped)
    report()
    return cards


async def _extract_batch(llm: LLMPort, cancellation: CancellationToken,
                         papers: list[ReviewPaper], language: str, signature: str) -> EvidenceBatch:
    return await structured_call(
        llm, cancellation, "evidence", _say(
            language,
            'Return {"cards":[{"paper_id":"...","evidence":[{"category":"finding",'
            '"text":"concise supported observation","quote":"continuous verbatim abstract span"}]}]}. '
            "Return one card per paper and 1–6 observations per research abstract. Category: question, method, finding or scope. "
            "If the supplied text contains only bibliographic, degree-award, copyright or repository information, "
            "return that paper's card with evidence: []. Do not infer research content from its title. "
            "Build a compact comparison record: the research question/concept, study design and measures, "
            "population or context, main findings and explicitly stated boundary conditions when available. "
            "Retain distinctions needed to compare studies instead of extracting only headline conclusions. "
            "Use short precise observations in the requested output language. Do not invent unstated limitations. "
            "Copy quotes exactly, including punctuation and spelling. Each quote is the one or two abstract sentences "
            "that best support that observation's text, never the whole abstract; prefer one sentence or a short clause. "
            "Put separated observations in separate evidence entries, never join separated spans.",
            '返回 {"cards":[{"paper_id":"...","evidence":[{"category":"finding",'
            '"text":"简明且有依据的观察","quote":"摘要中连续的原文片段"}]}]}。'
            "每篇论文返回一张卡片，每篇研究摘要提取 1–6 条观察。category 取 question、method、finding 或 scope。"
            "如果提供的文本只有书目、学位授予、版权或仓储信息，仍返回该论文的卡片，并令 evidence 为 []。不要根据标题推断研究内容。"
            "为每篇论文建立便于比较的紧凑记录：研究问题或概念、研究设计与测量、研究对象或情境、主要发现，以及明确说明的适用条件。"
            "保留比较不同研究所需的区别，不要只摘取标题式结论。text 用简短准确的中文撰写，不要编造摘要未说明的局限。"
            "quote 必须逐字照抄摘要原文，保持原文语言，包括标点和拼写，不要翻译。每条 quote 只取摘要中与这条观察（text）"
            "最吻合的一到两句，绝不整段照抄摘要；宜为一句或一个短分句。相互分隔的观察放入不同的 evidence 条目，绝不拼接不连续的片段。"),
        {"language": language, "papers": [{"id": p.id, "title": p.title, "abstract": p.abstract} for p in papers]},
        EvidenceBatch, max_tokens=6500, language=language,
        validate=lambda value: validate_cards(value, papers, signature),
    )


async def prepare_outline(llm: LLMPort, cancellation: CancellationToken, *, topic: str,
                          language: str, length: str, cards: list[EvidenceCard],
                          section_count: int | None = None, papers: list[ReviewPaper] | None = None,
                          paragraph_count: int | None = None) -> ReviewOutline:
    count = section_count or {"short": 1, "standard": 3, "detailed": 6}[length]
    paragraph_limit = paragraph_count or {"short": 3, "standard": 5, "detailed": 7}[length]
    metadata = {paper.id: paper for paper in papers or []}
    paragraph_schema = create_model("PlannedParagraph", __base__=ParagraphPlan,
                                    paper_ids=(list[str], Field(min_length=1, max_length=PLANNED_PAPERS_PER_PARAGRAPH)))
    section_schema = create_model("PlannedSection", __base__=_PlannedSection,
                                  paragraphs=(list[paragraph_schema], Field(min_length=paragraph_count or 1, max_length=paragraph_limit)))
    schema = create_model("OutlinePlan", __base__=ReviewOutline,
                          sections=(list[section_schema], Field(min_length=count, max_length=count)))

    available = {card.paper_id for card in cards if card.evidence}

    def validate(value: ReviewOutline) -> None:
        # A paper without verified evidence is simply left out of the plan, not a reason to redo it.
        for section in value.sections:
            for plan in section.paragraphs:
                plan.paper_ids = [paper for paper in plan.paper_ids if paper in available]
            section.paper_ids = list(dict.fromkeys(paper for plan in section.paragraphs for paper in plan.paper_ids))
        planned = {paper for section in value.sections for paper in section.paper_ids}
        # An exclusion of a paper that is also planned, or not usable at all, is simply dropped.
        value.excluded = [item for item in {item.paper_id: item for item in value.excluded}.values()
                          if item.paper_id in available and item.paper_id not in planned]
        # Papers neither planned nor excluded are placed or excluded by a small follow-up request.
        validate_outline(value, cards, require_all=False)
        if len(value.sections) != count or any(section.parent_id for section in value.sections):
            raise ValueError(f"Return exactly {count} top-level sections, with no nested headings.")
        if count <= 4 and any(section.kind != "theme" for section in value.sections):
            raise ValueError("For 1–4 sections use thematic sections only, without separate introduction or conclusion.")
        if not value.focus or any(not section.paragraphs for section in value.sections):
            raise ValueError("Provide an overall synthesis focus and a paragraph plan for every section.")

    plan = await structured_call(
        llm, cancellation, "outline", _say(language, 'Return {"focus":"central question and strongest evidence-backed synthesis thread","sections":[{"id":"s1","title":"...",'
        '"parent_id":"","kind":"theme","goal":"what this section establishes",'
        '"comparisons":["concrete relationship to examine"],'
        '"paragraphs":[{"goal":"paragraph argument, comparison and its role in the progression",'
        '"paper_ids":["p_001","p_002"]}]}]}. '
        "First compare the evidence records across papers, grouping related concepts, findings and study conditions. "
        "Select the most substantive contributions as the narrative spine; make their distinctive value explicit. "
        "Develop distinct themes that answer the research question; merge overlapping topics. A theme must have "
        "a substantive organizing idea, not just be a keyword or an individual paper. "
        "Place methodological and contextual work according to the contribution it actually makes. "
        "Titles should state that contribution precisely, without promising an untested outcome. "
        f"Return exactly {count} sections. For 1 section, plan one continuous literature-review section. "
        "For 2–4 sections, each develops a different substantive aspect: ALL kinds must be theme; "
        "do not spend sections on an introduction, general overview or conclusion. A brief opening paragraph "
        "may orient the first section. For 5 or more sections, overview/conclusion are optional where useful "
        "for a standalone article, count toward the requested total and must not displace substantive analysis. "
        "There are only two planning levels: sections and paragraph plans. Paragraphs are prose, not subsections. "
        + (f"The user chose exactly {paragraph_count} paragraphs per section: plan exactly that many in every section. "
           if paragraph_count else
           f"Target 2–3/3–5/5–7 paragraphs per section for short/standard/detailed; never exceed {paragraph_limit}. "
           "Use fewer paragraphs when the argument is simple. ") +
        "More papers should broaden comparisons within "
        "the same paragraph, not create a paragraph for each study. Merge related contributions into one argument. "
        "Give each paragraph a specific analytical purpose and supporting papers; use a natural progression "
        "rather than forcing introduction-body-summary within every section. Connect comparable studies, "
        "explain differing results through goals and study contexts, and retain distinct minority contributions. "
        "required_paper_ids lists every selected paper. Plan each one that serves the argument in a paragraph; "
        "for each paper you leave out, add {\"paper_id\",\"reason\"} to excluded, with a short reason in the requested "
        "language (for example off the research question, duplicates a stronger study, or only background). "
        "Every selected paper appears either in a paragraph plan or in excluded, never both. "
        "Assign paper_ids at paragraph level only; the application derives each section's paper_ids from its paragraphs. "
        "Do not repeat a separate section-level paper list. Copy the exact supplied IDs, including numbering gaps. "
        "Do not assign every paper everywhere or invent a difference, limitation or gap just to fill a slot. "
        f"Distribute primary sources across substantive paragraphs, normally 2–6 papers and never more than "
        f"{PLANNED_PAPERS_PER_PARAGRAPH} per paragraph. "
        "Do not make the last paragraph a catch-all for remaining studies or repeat the entire corpus there. "
        "Plan an ending that reinforces the central insight; do not automatically add weaknesses or reverse the narrative. "
        "Write all goals, titles, comparisons and the focus in the requested language.",
        '返回 {"focus":"核心问题，以及证据最充分的综合论述主线","sections":[{"id":"s1","title":"...",'
        '"parent_id":"","kind":"theme","goal":"本节要确立的内容",'
        '"comparisons":["需要考察的具体关系"],'
        '"paragraphs":[{"goal":"本段的论点、比较及其在整体推进中的作用",'
        '"paper_ids":["p_001","p_002"]}]}]}。'
        "先在论文之间比较证据记录，把相关的概念、发现和研究条件归组。"
        "选取最有实质意义的贡献作为叙述主线，并明确说明它们各自独特的价值。"
        "形成能够回答研究问题、彼此不同的主题，合并相互重叠的话题。每个主题都要有实质性的组织思想，不能只是一个关键词或一篇论文。"
        "方法类和背景类研究按其实际贡献安排位置。"
        "标题应准确表述该贡献，不要承诺未经检验的结果。"
        f"恰好返回 {count} 个节。只有 1 个节时，规划一节连贯的文献综述。"
        "2–4 个节时，每节展开一个不同的实质方面：kind 一律为 theme；"
        "不要把节用于引言、总体概述或结论。第一节可以用一段简短的开头交代背景。"
        "5 个及以上节时，可按需要设置 overview/conclusion，使文章自成一体；它们计入总节数，且不能挤占实质分析。"
        "规划只有两级：节和段落计划。段落是连续的论述，不是小节。"
        + (f"用户指定每节恰好 {paragraph_count} 段：每节都必须规划恰好这么多段。"
           if paragraph_count else
           f"short/standard/detailed 每节分别以 2–3/3–5/5–7 段为宜，绝不超过 {paragraph_limit} 段。"
           "论证简单时可以少用几段。") +
        "论文较多时，应在同一段内拓宽比较，而不是每篇研究单独成段。把相关的贡献合并到同一论证中。"
        "每段都要有明确的分析目的和支撑论文；按自然的递进展开，"
        "不要在每节内部强行套用“引言—正文—总结”。把可比较的研究联系起来，"
        "用研究目标和研究情境解释不同结果，并保留有独特价值的少数贡献。"
        "required_paper_ids 列出了全部入选论文。对论证有用的论文安排到段落计划中；不采用的论文，"
        "在 excluded 中加入 {\"paper_id\",\"reason\"}，用中文简要说明原因（例如偏离研究问题、与证据更充分的研究重复、仅为背景）。"
        "每篇入选论文要么出现在段落计划中，要么出现在 excluded 中，不能两处都有。"
        "paper_ids 只在段落层面分配；应用程序会根据段落自动汇总每节的 paper_ids。"
        "不要另外重复节一级的论文列表。原样复制提供的编号，包括不连续的编号。"
        "不要把所有论文分配到每一处，也不要为了填满位置而编造差异、局限或研究空白。"
        f"把主要文献分散到各个实质段落中，通常每段 2–6 篇，每段绝不超过 {PLANNED_PAPERS_PER_PARAGRAPH} 篇。"
        "不要把最后一段变成收纳剩余研究的杂项段，也不要在那里重复全部文献。"
        "结尾应强化核心见解，不要自动添加不足之处或扭转前文论述。"
        "所有 goal、title、comparisons 和 focus 都用中文撰写。"),
        {"topic": topic, "language": language, "length": length, "section_count": count,
         **({"paragraphs_per_section": paragraph_count} if paragraph_count else {}),
         "required_paper_ids": [card.paper_id for card in cards],
         "papers": [{"id": card.paper_id, "claims": [
             {"category": item.category, "text": item.text} for item in card.evidence],
             "title": metadata[card.paper_id].title if card.paper_id in metadata else "",
             "year": metadata[card.paper_id].year if card.paper_id in metadata else None} for card in cards]},
        schema, max_tokens=min(14000, 2000 + count * 1600), validate=validate, language=language,
    )
    empty = {f"{section.id}:{i + 1}": (section, paragraph)
             for section in plan.sections for i, paragraph in enumerate(section.paragraphs) if not paragraph.paper_ids}
    if empty:
        repair_schema = create_model("UnsupportedParagraphPlans", __base__=ReviewModel,
                                     **{key: (ParagraphPlan, ...) for key in empty})

        def validate_repair(value: BaseModel) -> None:
            for paragraph in value.model_dump().values():
                if set(paragraph["paper_ids"]) - available or len(set(paragraph["paper_ids"])) != len(paragraph["paper_ids"]):
                    raise ValueError(f"Use distinct supplied paper IDs only: {sorted(available)}.")

        repaired = await structured_call(
            llm, cancellation, "outline_sources",
            "The supplied paragraph plans lost all their sources because those papers have no usable evidence. "
            "Revise ONLY these paragraph goals and sources to make a supported argument using the supplied evidence. "
            "Do not just attach unrelated citations to the old claim. Keep the section's theme and the paragraph's "
            "place in the review. Return each requested key with goal and paper_ids; use only the supplied IDs, "
            "without duplicates. Write goals in the requested language.",
            {"topic": topic, "language": language, "focus": plan.focus,
             "paragraphs": {key: {"section": section.title, "section_goal": section.goal, "goal": paragraph.goal}
                            for key, (section, paragraph) in empty.items()},
             "papers": [{"id": card.paper_id, "claims": [item.text for item in card.evidence]} for card in cards]},
            repair_schema, max_tokens=min(14000, max(2000, len(empty) * 600)),
            validate=validate_repair, language=language,
        )
        for key, (_, paragraph) in empty.items():
            replacement = getattr(repaired, key)
            paragraph.goal, paragraph.paper_ids = replacement.goal, replacement.paper_ids
        for section in plan.sections:
            section.paper_ids = list(dict.fromkeys(paper for paragraph in section.paragraphs for paper in paragraph.paper_ids))
    assigned = {paper for section in plan.sections for paper in section.paper_ids}
    excluded = {item.paper_id for item in plan.excluded}
    missing = [card for card in cards if card.evidence and card.paper_id not in assigned | excluded]
    if missing:
        targets = {f"{section.id}:{i + 1}": (section, paragraph)
                   for section in plan.sections for i, paragraph in enumerate(section.paragraphs)}
        room = {key: PLANNED_PAPERS_PER_PARAGRAPH - len(paragraph.paper_ids) for key, (_, paragraph) in targets.items()}
        choice = create_model("PaperPlacement", __base__=ReviewModel,
                              target=(Literal[(*targets, "exclude")], ...), reason=(str, Field(default="", max_length=400)))
        assignment_schema = create_model("MissingPaperPlacements", __base__=ReviewModel,
                                         **{card.paper_id: (choice, ...) for card in missing})

        def validate_placement(value: BaseModel) -> None:
            added: dict[str, int] = {}
            unexplained = []
            for paper_id, placement in value.model_dump().items():
                if placement["target"] == "exclude":
                    if not placement["reason"].strip():
                        unexplained.append(paper_id)
                    continue
                added[placement["target"]] = added.get(placement["target"], 0) + 1
            issues = []
            if full := sorted(key for key, count in added.items() if count > room[key]):
                issues.append(f"These paragraphs would exceed {PLANNED_PAPERS_PER_PARAGRAPH} papers: {full}. "
                              "Place the extra papers elsewhere or exclude them with a reason.")
            if unexplained:
                issues.append(f"Give a short reason for excluding {sorted(unexplained)}.")
            if issues:
                raise ValueError(" ".join(issues))

        placements = await structured_call(
            llm, cancellation, "outline_assign", _say(
                language,
                "Each supplied paper is not yet used by the outline. For each, either assign it to the existing paragraph "
                "whose argument it genuinely strengthens, or exclude it with a short reason in the requested language "
                "(for example off the research question, duplicates a stronger study, or only background). "
                'Return a JSON object mapping each paper ID to {"target":"<target id or exclude>","reason":"..."}. '
                f"A paragraph may hold at most {PLANNED_PAPERS_PER_PARAGRAPH} papers; available_slots shows its free room. "
                "Thematic fit matters more than using every paper; avoid concentrating sources in a final summary. "
                "Do not rewrite goals or add paragraphs or sections.",
                "提供的每篇论文尚未被大纲使用。逐篇判断：要么分配到其论点确实能被它加强的现有段落，要么用中文简要说明原因并排除"
                "（例如偏离研究问题、与证据更充分的研究重复、仅为背景）。"
                '返回一个 JSON 对象，把每个论文编号映射为 {"target":"<目标编号或 exclude>","reason":"..."}。'
                f"每段最多 {PLANNED_PAPERS_PER_PARAGRAPH} 篇，available_slots 是该段剩余的名额。"
                "主题契合比用上每篇论文更重要；不要把文献集中到最后的总结中。不要改写目标，也不要增加段落或节。"),
            {"topic": topic, "focus": plan.focus,
             "papers": [{"id": card.paper_id, "title": metadata[card.paper_id].title if card.paper_id in metadata else "",
                         "claims": [item.text for item in card.evidence]} for card in missing],
             "targets": [{"id": key, "section": section.title, "goal": paragraph.goal,
                          "paper_ids": paragraph.paper_ids, "available_slots": room[key]}
                         for key, (section, paragraph) in targets.items()]},
            assignment_schema, max_tokens=max(1500, 120 * len(missing)), validate=validate_placement, language=language,
        )
        for paper_id, placement in placements.model_dump().items():
            if placement["target"] == "exclude":
                plan.excluded.append(PaperExclusion(paper_id=paper_id, reason=placement["reason"]))
            else:
                targets[placement["target"]][1].paper_ids.append(paper_id)
        for section in plan.sections:
            section.paper_ids = list(dict.fromkeys(paper for paragraph in section.paragraphs for paper in paragraph.paper_ids))
    validate_outline(plan, cards)
    return ReviewOutline.model_validate(plan.model_dump())
