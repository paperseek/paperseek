# Source Routing Reference

Use this reference when deciding which PaperSeek data source to use for a user request.

## Source Map

| Source | Use for | Strengths | Limits |
| --- | --- | --- | --- |
| OpenAlex | Default natural-language literature discovery | Broad coverage, abstracts when available, citation counts, reference and citing-work traversal | API key recommended; metadata quality varies by work |
| arXiv | Preprint discovery in arXiv-covered disciplines | No key, abstracts, arXiv categories, PDF links | Preprint-focused; not a complete scholarly index |
| Semantic Scholar | Broad scholarly graph search and citation-count reference | Cross-disciplinary graph, abstracts when available, citations, DOI/PubMed/arXiv identifiers | Anonymous access is rate-limited; API key recommended for sustained use |
| PubMed | Medicine, biomedical, and life-science literature | PMID, journal, publication types, abstracts when available | Biomedical scope; email/tool metadata recommended for NCBI E-utilities |
| Google Scholar | Google Scholar result discovery through Serper | Scholar snippets, citation-count clues, and PDF links when available | Requires `PAPERSEEK_GOOGLE_SCHOLAR_API_KEY`; no native citation expansion |
| Computer science top conferences | Top CS conference papers | ICLR/ICML/NeurIPS/AAAI/NDSS-oriented conference paper search | Not exhaustive; no citation graph or DOI registry guarantees |
| Crossref | DOI and publisher bibliographic metadata | DOI/title/venue/year verification, publisher metadata, broad registry | Abstracts often absent; not ideal as the only semantic recall source |
| Web of Science Starter | Institution-backed WoS metadata when key and entitlement exist | Curated citation index metadata and citation counts | Key required; native abstracts should not be assumed |
| Wanfang Data | Chinese journal articles, theses, and conference papers | Broad Chinese coverage, Chinese abstracts and keywords | Requires `PAPERSEEK_WANFANG_API_KEY`; Chinese literature only |
| National Social Sciences Database (NSSD) | Chinese social sciences and humanities journals | No key, Chinese abstracts | Metadata and abstracts only; social sciences scope |
| Chinese Medical Journals (Yiigle) | Chinese Medical Association journals | No key, Chinese titles and abstracts | Medicine only; metadata and abstracts only |

## Default Choice

Use OpenAlex by default for open-ended literature search:

```bash
paperseek search "open innovation and digital platforms" --source openalex --output json
```

Use Crossref when:

- the task is DOI/title/metadata verification;
- OpenAlex metadata is sparse;
- the user explicitly asks for Crossref;
- you need a registry-oriented secondary check.

Use arXiv when:

- the user asks for preprints;
- the topic is in computer science, physics, mathematics, statistics, quantitative biology, electrical engineering, or economics;
- PDF links and arXiv categories are useful.

Use Semantic Scholar when:

- the user wants a broad scholarly graph source;
- citation counts and multiple external identifiers are useful;
- the user configured `PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY` or accepts light anonymous usage.

Use PubMed when:

- the topic is medicine, biomedical science, or life science;
- PMID and journal metadata matter.

Use Google Scholar through Serper when:

- the user explicitly asks for Google Scholar;
- `PAPERSEEK_GOOGLE_SCHOLAR_API_KEY` is configured;
- Scholar snippets, citation-count clues, and PDF links are useful enough for screening.

Use computer science top-conference search when:

- the user asks for top computer-science conference papers;
- the task is ML/AI/security conference discovery;
- a focused static conference index is acceptable.

Use Wanfang Data, NSSD, or Chinese Medical Journals when:

- the user wants Chinese-language literature;
- Wanfang for broad Chinese coverage, when `PAPERSEEK_WANFANG_API_KEY` is configured;
- NSSD for Chinese social sciences and humanities;
- Chinese Medical Journals for Chinese clinical and medical research.

Use WoS only when:

- the user has configured `PAPERSEEK_WOS_API_KEY`;
- the task explicitly requires WoS Starter;
- Clarivate API availability has been checked.

## Citation Expansion

Citation expansion is OpenAlex-only. It can add forward and backward citation neighbors from high-scoring seed papers, then rerank the expanded candidate pool.

Use it when:

- user wants related work discovery;
- the first query is likely to miss classic or neighboring papers;
- citation graph exploration is useful.

Avoid or disable it when:

- the user wants a small, fast smoke test;
- rate limits are a concern;
- source is not OpenAlex.

CLI flag:

```bash
paperseek search "QUERY" --source openalex --no-citation-enabled
```

## Query Guidance

- Natural-language questions may be Chinese or English; PaperSeek asks the LLM to generate source-appropriate search queries.
- For Chinese user questions, preserve the original question. Most sources get English scholarly query terms; Wanfang Data, NSSD, and Chinese Medical Journals are planned and queried in Chinese whatever language the question uses.
- Prefer concise research questions over very long pasted text.
- Add a field constraint only when the user requests one; it can over-narrow results. For OpenAlex, arXiv, and WoS, use repeated `--field-id` values from the source’s supported fields or categories (for example, `--source arxiv --field-id cs.CL`). For the other sources, use `--field-hint "TEXT"` instead. Do not abbreviate either option to `--field`, which is ambiguous.
- If results are weak, rerun with a shorter question, fewer concepts, or broader terms.

## Result Interpretation

PaperSeek ranks returned candidates with LLM relevance scoring. Treat this as a screening signal, not as evidence that the paper is high quality.

When reporting results, include:

- final query;
- data source;
- number of iterations;
- total returned count;
- top paper titles, authors, venue, year, DOI or URL when available;
- relevance score and reason;
- CAS tier, JCR quartile and Impact Factor, CCF class, or conference acceptance when the result carries them.

Do not fabricate missing abstracts, DOI, citation counts, or authors.
