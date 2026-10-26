# PaperSeek

**Open-source literature search, citation exploration, and abstract-based reviews.**

PaperSeek turns a research question into a reading list. It plans and refines queries, retrieves papers from your chosen source, expands citations where supported, and ranks relevance. Explore the results in a bilingual Web workspace, save local history, and export a reading list.

[Project overview](https://github.com/paperseek/paperseek) · [Documentation](https://docs.paperseek.app/) · [中文文档](https://docs.paperseek.app/zh/) · [Try online](https://paperseek.app/)

## Install

Requires Python 3.12 or newer. The release package includes the Web UI.

```sh
python -m pip install "paperseek==1.0.0b4"
paperseek serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Configure a model API and data source in the application.

## Capabilities

- Eleven scholarly sources: OpenAlex, arXiv, Crossref, Semantic Scholar, PubMed, Google Scholar via Serper, PaperHub (widened with accepted OpenReview papers), Web of Science Starter, and the Chinese databases Wanfang Data, NSSD, and Chinese Medical Journals, which are searched in Chinese.
- Query iteration, bounded candidate retrieval, optional citation expansion, and relevance ranking.
- OpenAI Responses, OpenAI-compatible Chat Completions, and Anthropic Messages model protocols.
- Local fusion, Embedding + Rerank, or System One preliminary ranking.
- Venue labels and filters: CAS tiers, JCR quartiles and Impact Factor, CCF classes (from ShowJCR), and conference acceptance from PaperHub and OpenReview.
- Search history, filters, selection, citation maps, and complete CSV, JSON, BibTeX, and RIS exports.
- **Experimental** abstract-based reviews with editable outlines, section writing, versions, and Markdown/Word/BibTeX exports.

A run begins with one selected data source. Search is bounded, not guaranteed exhaustive; reviews use available abstracts and metadata rather than automatically read full texts.

## Terminal and integrations

The CLI provides `serve`, `worker`, `search`, `doctor`, `migrate`, and `version`. Configure process environment variables before a terminal search; a nearby `.env` is not loaded automatically.

```sh
paperseek search "How does open-source funding affect maintenance?" --source openalex --search-budget medium --output json
```

The local HTTP API is under `/api/v1`, with its schema at `/openapi.json`. MCP is packaged separately:

```sh
python -m pip install "paperseek-mcp==1.0.0b4"
paperseek-mcp --transport stdio
```

## Data and upgrades

Community stores history in `~/.paperseek/paperseek.db` by default, with artifacts and credential-encryption material in its data directory. Back up the complete directory consistently.

For Docker, separate workers, and 0.2 migration, see [self-hosting](https://docs.paperseek.app/self-hosting/) and [upgrading](https://docs.paperseek.app/self-hosting/upgrade).

## Development and license

The source distribution uses `paperseek_core` for research behavior, `paperseek_app` for use cases and ports, and `paperseek_community` for local adapters. Hosted consumes a fixed Community distribution. Common Vue sources are included for Hosted builds; installed Community serves the prebuilt assets.

[Development guide](https://docs.paperseek.app/contributing) · [Report an issue](https://github.com/paperseek/paperseek/issues) · [Apache-2.0 license](https://github.com/paperseek/paperseek/blob/main/LICENSE)
