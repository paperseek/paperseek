<div align="center">
  <h1>
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/public/paperseek-lockup-dark.svg">
      <img src="docs/public/paperseek-lockup.svg" width="320" alt="PaperSeek">
    </picture>
  </h1>
  <p><strong>Follow the question. Find the evidence.</strong></p>
  <p>Open-source literature search, preliminary bibliometrics, and abstract-based reviews.</p>
  <p>
    <a href="https://github.com/paperseek/paperseek/blob/main/LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-171717?style=flat-square"></a>
    <img alt="Version 1.0.0-beta3" src="https://img.shields.io/badge/version-1.0.0--beta3-171717?style=flat-square">
    <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-171717?style=flat-square">
    <img alt="Vue 3" src="https://img.shields.io/badge/frontend-Vue%203-171717?style=flat-square">
    <a href="https://modelscope.cn/studios/HongMingfeng/PaperSeek"><img alt="Try it on ModelScope" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.paperseek.app%2Fbadges%2Fmodelscope.json&style=flat-square"></a>
  </p>
  <p>
    <a href="https://paperseek.app/">Try online</a> ·
    <a href="https://docs.paperseek.app/">Documentation</a> ·
    <a href="#quick-start">Quick start</a> ·
    <a href="README.zh-CN.md">简体中文</a>
  </p>
</div>

---

[![PaperSeek: follow the question, find the evidence](docs/public/images/readme-banner-en.png)](https://paperseek.app/)

PaperSeek turns a research question into a reading list. It plans and refines queries, retrieves candidate papers from your chosen scholarly source, expands citations where supported, and ranks relevance to the question. You can inspect the process, explore connections, save results, and develop a cited review from selected abstracts.

Run the Community edition on your own machine or server, or use [PaperSeek online](https://paperseek.app/). Bring your preferred model API; use the Web UI, CLI, HTTP API, or MCP integration.

## Contents

- [Why PaperSeek](#why-paperseek)
- [What’s new in 1.0](#whats-new-in-10)
- [How it works](#how-it-works)
- [Data sources](#data-sources)
- [Quick start](#quick-start)
- [Models and ranking](#models-and-ranking)
- [Reviews · Experimental](#reviews--experimental)
- [Community and online](#community-and-online)
- [CLI, MCP, and API](#cli-mcp-and-api)
- [Agent Skill](#agent-skill)
- [Documentation](#documentation)
- [Architecture and contributing](#architecture-and-contributing)
- [Acknowledgements and license](#acknowledgements-and-license)

## Why PaperSeek

**Start with a question, not a perfect query.** Describe the relationship, method, population, or research gap you want to investigate. PaperSeek translates it into source-specific searches and refines them using retrieval feedback.

**See how the reading list was formed.** Follow query iterations, source hits, candidate retrieval, and ranking. Search budgets bound the work, while saved runs keep results and events available for later inspection.

**Connect papers instead of reading a flat list.** OpenAlex citation expansion follows references and citing works. The interactive map provides another route through the literature around your results.

**Choose your model stack.** Use OpenAI Responses, OpenAI-compatible Chat Completions, or Anthropic Messages. Start with local preliminary ranking, or configure Embedding + Rerank or the Jev decision model.

**Keep the next step close.** Filter and select papers, export complete results, reopen history, or turn a selected evidence set into an experimental review project.

## What’s new in 1.0

The 1.0 line rebuilds PaperSeek around durable tasks and a shared core. Releases before 1.0 are in the [full changelog](https://docs.paperseek.app/changelog).

### 1.0.0-beta3

- **Empty searches** get up to two more queries, each checked against your question first; if nothing turns up, the search tells you why instead of dropping required conditions.
- **Data sources** are paced across every running task, PubMed rate limits are recognized as rate limits, and Web of Science is selectable again.
- **Network proxy** in Settings › Data sources sends model requests, sources that limit by network address, or other sources through your own proxy.
- **Several tasks at once:** up to four searches and reviews run side by side (`PAPERSEEK_MAX_CONCURRENT_RUNS`).

It upgrades earlier 1.0 betas in place. See the [changelog](https://docs.paperseek.app/changelog#v1-0-0-beta3) and the [upgrade steps](https://docs.paperseek.app/self-hosting/upgrade#from-an-earlier-1-0-beta).

### 1.0.0-beta2

- **Reviews** no longer fail because a few papers lack usable evidence: those papers are left out and marked, and the review continues.
- **Search ranking** keeps the scores a model did give and asks again only for the missing papers; long “Why it ranks” explanations are no longer cut off.
- **History** shows queued and running searches, also after you reload the page.
- **Docker Compose** publishes PaperSeek on this machine only by default; set `PAPERSEEK_HOST=0.0.0.0` to reach it from other devices.

See the [changelog](https://docs.paperseek.app/changelog#v1-0-0-beta2).

### 1.0.0-beta1

**A complete architectural refactor with breaking changes.** The runtime, frontend, persistence, and integrations now form a modular application with durable task execution.

- Search, Bibliometrics, and Reviews in a collapsible sidebar, each with its own history; models, sources, and preferences in Settings.
- Preliminary bibliometrics over the full retrieved deduplicated candidate corpus: distributions, keyword co-occurrence, bibliographic coupling, field coverage, and independent exports. LLM scoring remains a subset.
- Separate research core, application services, and deployment adapters, shared across Community, PaperSeek online, CLI, HTTP, and MCP.
- Database-backed local runs with worker leases, checkpoint recovery, cancellation, saved history, and result artifacts.
- Versioned `/api/v1` endpoints and generated frontend types.
- Query-history feedback, bounded candidate selection, and multiple preliminary-ranking paths.
- Experimental reviews with editable outlines, section writing, versions, and cited exports.
- Independent, version-matched `paperseek-mcp` packaging and an agent usage skill.

Upgrading from 0.2 requires Python 3.12+ and the [migration guide](https://docs.paperseek.app/self-hosting/upgrade): user data has a migration path, while old internal imports, frontend endpoints, and retired commands/configuration are not retained. See the [changelog](https://docs.paperseek.app/changelog#v1-0-0-beta), [workspace and history](https://docs.paperseek.app/guide/workspace), and [bibliometrics](https://docs.paperseek.app/guide/bibliometrics).

## How it works

![PaperSeek 1.0.0-beta3 research workspace](docs/public/images/workspace-results-en.png)

*Community 1.0.0-beta3 with a demonstration record. PaperSeek online shares the same workspace and adds an account and service options.*

| Step | What happens | What you can inspect |
| --- | --- | --- |
| 1. Understand | Interpret the question and plan source-specific queries | Research scope and search strategy |
| 2. Refine | Test queries and adapt them using source feedback | Query history and source hit counts |
| 3. Retrieve & rank | Collect candidates, optionally expand citations, and score relevance | Retrieval progress and ranking |
| 4. Explore | Present papers, connections, and exports | Results, citation map, and saved history |

A run starts with **one selected data source**. Complementary retrieval paths do not mean that every database is searched at once. Source hit counts, downloaded candidates, and final results describe different sets.

PaperSeek is a discovery and organization tool. It does not guarantee an exhaustive systematic search, and a relevance score is not a study-quality assessment. Read original papers before relying on generated claims.

## Data sources

| Source | Useful for | Access |
| --- | --- | --- |
| OpenAlex | Broad scholarly discovery and citation connections | API key recommended |
| arXiv | Scientific preprints | No key required |
| Crossref | DOI and publication metadata | No key required |
| Semantic Scholar | Papers and scholarly graph metadata | Optional API key |
| PubMed | Biomedical and life-science literature | Optional NCBI API key |
| Google Scholar via Serper | Scholar results through a supported API | Serper key required |
| PaperHub | Top computer-science conference collections | No key required |
| Web of Science Starter | WoS collection search | Clarivate key required |

OpenAlex and Serper support multi-key rotation. Metadata coverage and field filters vary by source. See the [source guide](https://docs.paperseek.app/guide/sources) for configuration and credential links.

## Quick start

### Python package

Requires **Python 3.12+**. The release package includes the built Web UI.

```sh
python -m pip install "paperseek==1.0.0b3"
paperseek serve
```

Open [localhost:8765](http://127.0.0.1:8765), configure a model connection and source, then enter your research question. You do not need a PaperSeek account for Community.

### Docker

```sh
git clone https://github.com/paperseek/paperseek.git
cd paperseek
cp .env.example .env
PAPERSEEK_VERSION=1.0.0-beta3 docker compose up -d
```

Edit `.env` before starting. On PowerShell, use `Copy-Item` instead of `cp`, then `$env:PAPERSEEK_VERSION="1.0.0-beta3"; docker compose up -d`. The included Compose service publishes 127.0.0.1:8765 and uses a persistent `paperseek-data` volume. These commands pin the beta image; prereleases do not update `latest`. The supplied Compose file and `.env.example` also default to `1.0.0-beta3`.

### ModelScope Studio

Try the [PaperSeek Studio](https://modelscope.cn/studios/HongMingfeng/PaperSeek) on ModelScope, or fork it to run your own copy as a Docker Studio:

<a href="https://modelscope.cn/studios/fork?target=HongMingfeng/PaperSeek"><img src="docs/public/deploy-modelscope.svg" alt="Deploy to ModelScope" height="32"></a>

Community is built for one user in a trusted environment and has no sign-in. Keep your copy private: everyone who can open a public Studio shares its settings, saved keys, and history.

### From source

```sh
git clone https://github.com/paperseek/paperseek.git
cd paperseek
python -m pip install ./backend
```

The Python project lives in `backend/`. Frontend development and building a checkout without prebuilt assets are covered in [Contributing](CONTRIBUTING.md).

### Configuration and data

The Web UI exposes model and search configuration. For deployment defaults, see [.env.example](.env.example) and the [configuration reference](https://docs.paperseek.app/reference/configuration).

Direct CLI execution reads process environment variables; it does not automatically load `.env`. Docker Compose explicitly loads that file.

Local history defaults to `~/.paperseek/paperseek.db`. Keep its surrounding data directory, artifacts, and encryption key together when backing up. See [self-hosting](https://docs.paperseek.app/self-hosting/) for storage and process management.

## Models and ranking

The main model handles query planning and relevance evaluation. Select a protocol your provider actually supports:

| Protocol | Configuration value |
| --- | --- |
| OpenAI Responses | `openai_responses` |
| OpenAI-compatible Chat Completions | `openai_chat` |
| Anthropic Messages | `anthropic_messages` |

Preliminary ranking has three paths:

- **Local fusion:** combines local lexical and semantic signals; no separate ranking service to configure.
- **Embedding + Rerank:** uses external vector and reranking services.
- **System One:** uses a configured decision service for preliminary selection, such as TypeSafe Jev.

Local fusion is the default preliminary-ranking path; it does not remove the main language-model requirement. See [Models & ranking](https://docs.paperseek.app/guide/models).

### Preliminary ranking with Jev

PaperSeek now supports Jev for preliminary ranking: add a System One connection with the TypeSafe provider (`jev-latest`), then choose **System One decision** as the preliminary ranking. Jev assesses the locally fused candidates, up to twice the final candidate budget, before the main model scores the final subset.

## Reviews · Experimental

Select papers with useful abstracts, create a review project, generate and edit an outline, then write and revise sections. The workspace supports versions, linked citations, and **Markdown, Word, and BibTeX** exports.

Reviews are based on available abstracts and metadata. They do not imply that the model has read full-text PDFs. Check the evidence and citations against the original research before using a draft.

[Read the review guide →](https://docs.paperseek.app/guide/reviews)

## Community and online

| | Community | PaperSeek online |
| --- | --- | --- |
| Run location | Your machine or server | Managed website |
| Research data | Local SQLite and files | Account-scoped storage for signed-in work |
| Models | Your configuration | PaperSeek Service or Bring Your Own Key |
| Account | Not required | Required for saved history and reviews |
| Anonymous use | Local workspace | Temporary BYOK search |
| Local model endpoints | Supported when reachable | Non-public destinations blocked |

The online edition uses the same search implementation and pages as Community, and its user guide is part of this documentation.

[Online guide →](https://docs.paperseek.app/online/)

## CLI, MCP, and API

### Command line

```sh
paperseek search "How does open-source funding affect maintenance?" --source openalex --search-budget medium --output json
```

Configure credentials in the process environment first. The six commands are `serve`, `worker`, `search`, `doctor`, `migrate`, and `version`.

### Model Context Protocol

```sh
python -m pip install "paperseek-mcp==1.0.0b3"
paperseek-mcp --transport stdio
```

MCP tools start a run, follow its status, read results and citation graphs, cancel work, and export results. Streamable HTTP is available for compatible clients.

### HTTP API

The local API is under `/api/v1`, with its installed schema at `/openapi.json`.

[CLI guide](https://docs.paperseek.app/integrations/cli) · [MCP setup](https://docs.paperseek.app/integrations/mcp) · [HTTP API](https://docs.paperseek.app/integrations/api)

## Agent Skill

Add the [PaperSeek Skill](integrations/skills/paperseek) to a compatible agent for local literature research. It guides installation checks, isolated Community installation when missing, configuration, searches, and saved results. CLI use does not require MCP.

[Install and use the Skill →](https://docs.paperseek.app/integrations/skill)

## Documentation

| Start here | Go deeper |
| --- | --- |
| [Quick start](https://docs.paperseek.app/guide/quick-start) | [Models & ranking](https://docs.paperseek.app/guide/models) |
| [Search workflow](https://docs.paperseek.app/guide/search) | [Citation maps](https://docs.paperseek.app/guide/citation-map) |
| [Results & history](https://docs.paperseek.app/guide/results) | [Reviews · Experimental](https://docs.paperseek.app/guide/reviews) |
| [Online accounts](https://docs.paperseek.app/online/account) | [Self-hosting & upgrades](https://docs.paperseek.app/self-hosting/) |
| [Configuration](https://docs.paperseek.app/reference/configuration) | [Troubleshooting](https://docs.paperseek.app/reference/troubleshooting) |

[English documentation](https://docs.paperseek.app/) · [中文文档](https://docs.paperseek.app/zh/)

## Architecture and contributing

PaperSeek uses a **modular monolith with a persistent task executor**. Its layers separate research logic from task management and deployment:

```text
Web / CLI / HTTP / MCP
          │
    paperseek_app ────→ paperseek_core
          │            Retrieval, ranking, source and model clients
          ▼
  Community adapters   SQLite and local artifacts
```

- **`paperseek_core`** implements the research workflow and source/model protocols, independently of FastAPI, authentication, SQLite, or Supabase.
- **`paperseek_app`** owns submission, execution, cancellation, checkpoints, and result access through storage ports.
- **`paperseek_community`** supplies SQLite, local artifacts, FastAPI, and CLI. A single `paperseek` wheel includes these three packages and the built Web UI.
- **PaperSeek online** builds on the same packages and Vue sources, adding its own authentication and storage adapters. Shared fixes belong in Community.

Community can run its API and embedded worker as one service. The online edition runs them as separate processes on the same application layer. See the [architecture and contribution notes](https://docs.paperseek.app/contributing#architecture) for execution and dependency boundaries.

Contributions are welcome: improve docs, add a reproducible issue, test a source integration, or propose a focused change. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations, and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Acknowledgements and license

PaperSeek is built with Python, FastAPI, httpx, Vue, Vite, and the work of the scholarly data providers listed above. The documentation uses VitePress; the interface takes design inspiration from [shadcn/ui](https://ui.shadcn.com/). See [third-party notices](THIRD_PARTY_NOTICES.md) for bundled components.

Licensed under [Apache-2.0](LICENSE). External data and APIs remain subject to their respective providers' terms.
