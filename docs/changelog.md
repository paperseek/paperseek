---
outline: 2
---

# Changelog

Features, improvements, and upgrade notes, from the first demo to PaperSeek 1.0.0-beta4.

## 1.0.0-beta4 {#v1-0-0-beta4}

Reviews start from your scope, can pick papers by topic, and say which papers they leave out and why. Ranking runs in parallel with separate progress for scores and reasons. It upgrades earlier 1.0 betas in place, without a migration.

### Reviews

- Setup now comes before paper selection. Choose chapter count, paragraphs per chapter, and paragraph length in the output language's unit; each length suggests how many papers to select.
- **Auto-select by topic** considers the first 256 papers with usable abstracts in search-rank order, picks papers that cover distinct themes, and gives a reason for each. It creates nothing and does not count as a writing task.
- A review can use up to 256 papers, selected manually from the whole search.
- Each generated paragraph plan uses at most 10 papers. Papers the outline leaves out are listed with a reason; add one to a chapter to bring it back.
- While writing, a planned paper may be skipped only with a reason. The evidence check reviews each reason, marks doubtful ones, and marks a reason it could not check for manual review.
- Citations a paragraph cannot place beside a specific claim are listed under it, also in Markdown and Word exports. Internal evidence IDs no longer appear in the text.
- One failed section no longer stops the other sections; a retry writes only what is missing or needs revision.
- Evidence quotations keep the abstract's wording and language and are shortened only when a shorter supporting passage can be found reliably.

### Search ranking

- Scoring and the "Why it ranks" explanations run in parallel batches, with separate progress for each part.
- With your own key, the provider may receive several requests at once; its rate limits still apply.

### Upgrade notes

- From an earlier 1.0 beta, install 1.0.0b4 with pip or set `PAPERSEEK_VERSION=1.0.0-beta4` for Docker; your data needs no migration. See [upgrading from an earlier 1.0 beta](self-hosting/upgrade.md#from-an-earlier-1-0-beta).
- Reviews that have no written sections yet extract evidence again the next time you generate the outline. Reviews with written sections keep their evidence.

See [Literature reviews](guide/reviews.md) for the workflow and limits.

## 1.0.0-beta3 {#v1-0-0-beta3}

Searches that find nothing get a careful second try, data sources are paced across every running task, and you can route traffic through your own proxy. It upgrades earlier 1.0 betas in place, without a migration.

### Search

- When a search finds no papers, PaperSeek tries up to two more queries. Each one builds on the earlier queries and their counts and is checked against your question first, so required concepts, relationships and constraints stay in place. If nothing turns up, the search ends with a notice that says why.
- If the data source cannot be reached for one of these extra queries, the search ends with no papers and a notice instead of failing.
- When the model proposes a query that already ran, it is shown the earlier result and asked once more before refinement stops.
- The intent check accepts model replies that wrap their answer in extra text.

### Data sources

- PubMed rate-limit messages are recognized as rate limits instead of network errors, and PaperSeek waits before trying again.
- All running tasks share each source's pacing and cooldowns, including retries. arXiv receives one request every three seconds however many searches are running.
- Ordinary failures are retried after about a second; only rate limits wait longer.
- Web of Science is selectable again.

### Network proxy

- **Settings › Data sources › Network proxy** routes requests through your own HTTP(S) proxy. Choose any of: model requests, sources that limit by network address (arXiv, Crossref, PaperHub, and PubMed or OpenAlex without a key), and other sources. Everything else follows your system proxy settings, and local model endpoints always connect directly.
- Several proxies are used in turn. Addresses are stored encrypted on your machine, shown only by host and port, and removed by **Clear saved keys**.
- Server deployments can set `PAPERSEEK_PROXY_URLS` and `PAPERSEEK_PROXY_SCOPE` instead. See [Network proxy](reference/configuration.md#network-proxy).

### Running tasks

- Up to four searches and reviews run at the same time; the rest wait in the queue. `PAPERSEEK_MAX_CONCURRENT_RUNS` changes the limit, and `0` removes it. See [Concurrent tasks](reference/configuration.md#concurrent-tasks).

### Upgrade notes

- From an earlier 1.0 beta, install 1.0.0b3 with pip or set `PAPERSEEK_VERSION=1.0.0-beta3` for Docker; your data needs no migration. See [upgrading from an earlier 1.0 beta](self-hosting/upgrade.md#from-an-earlier-1-0-beta).
- The new settings are optional. Without them, PaperSeek behaves as before, except that up to four tasks now run at once.

## 1.0.0-beta2 {#v1-0-0-beta2}

A maintenance release. Reviews and search rankings now carry on when a model gets part of its answer wrong, running searches stay in your history, and Docker Compose is private to your machine by default. It upgrades 1.0.0-beta1 in place, without a migration.

### Reviews

- A review no longer fails because a few papers come back without usable evidence. Those papers are left out of the outline and marked in the paper list; the rest of the review goes ahead, and preparing the outline again retries only them.
- A paper keeps every quote that matches its abstract, even when the model gets another quote wrong. Quotes stitched together with an ellipsis (…) are checked as separate passages instead of being rejected.
- The outline no longer stops on a paper without checked evidence. That paper is dropped from the plan, and a paragraph left without sources is given new ones from the checked evidence.

### Search ranking

- When the model scores only some of the candidates, the scores it gave are kept and only the missing papers are asked for again. Papers that still have no score are listed after the scored ones in their original order, and the search reports a warning.
- "Why it ranks" explanations are requested in smaller groups, so long explanations, Chinese ones especially, are no longer cut off. Papers a reply skipped are asked for once more.

### History

- Queued and running searches now appear in the search history, also after you reload the page, and cannot be deleted while they run.

### Self-hosting

- The supplied Docker Compose file publishes PaperSeek on this machine only (127.0.0.1:8765). To open it from other devices, set `PAPERSEEK_HOST=0.0.0.0` in `.env` and protect access; `PAPERSEEK_PORT` still changes the port.
- The MCP Docker image builds again from a clean checkout.
- Third-party notice tables display correctly in every distribution.

### Documentation site

- On the home page, a resting finger or the end of a trackpad swipe no longer jumps to another section.

### Upgrade notes

- From 1.0.0-beta1, install 1.0.0b2 with pip or set `PAPERSEEK_VERSION` to `1.0.0-beta2` for Docker; your data needs no migration. See [upgrading from an earlier 1.0 beta](self-hosting/upgrade.md#from-an-earlier-1-0-beta).
- If you reach a Docker deployment from another device, take the new `compose.yaml` and set `PAPERSEEK_HOST` as described above.

## 1.0.0-beta1 {#v1-0-0-beta}

PaperSeek 1.0.0-beta1 is a **complete architectural refactor with breaking changes**. It rebuilds the execution model, frontend, storage boundaries, and distribution around a shared research application, with an explicit migration path for existing user data.

### Three modules and independent histories

- A redesigned workspace: Search, Bibliometrics, and Reviews in a collapsible sidebar with each module's history, and the account at the bottom.
- Settings pages for model APIs, data sources, general preferences, and the user's data: clear saved keys or all history, each after typing a confirmation phrase. Running tasks are stopped before history is cleared.
- Preliminary bibliometrics over the actually retrieved deduplicated candidate corpus; native-field coverage, keyword co-occurrence, bounded bibliographic coupling, cached networks, and independent exports. LLM scoring remains a subset.
- Review citation evidence alongside papers, outline, and draft; new reviews preserve available source graph snapshots.
- Rename history records; the original question and settings stay unchanged.

Install it from pip as `1.0.0b1`; the Docker image tag is `1.0.0-beta1`.

### PaperSeek online

- Sign in for PaperSeek Service: built-in models, no keys, and free daily credits during the public beta (20 a day). A search or an analysis uses 1 credit and a review 5, deducted when the task starts; a failed or cancelled task returns them.
- Your own key: save model connections and data-source keys to your account in Settings; tasks then use your API and no credits. Every data source that takes a key (OpenAlex, PubMed, Semantic Scholar, Google Scholar, Web of Science) requires one in this mode; arXiv, Crossref, and PaperHub work without.
- Without signing in, run one temporary search with your own key typed into the form. It is held in memory only and never saved.

### Architecture rebuilt around clear responsibilities

- Separate `paperseek_core` for retrieval, query iteration, citation expansion, ranking, and source/model protocols; `paperseek_app` for run submission, execution, persistence, and queries; and deployment adapters for Community and the online edition.
- Use one application layer across Web, CLI, HTTP API, and MCP. The search core is independent of FastAPI, authentication, SQLite, and Supabase.
- The online edition and Community share one search implementation and the same research pages.
- Rebuild the frontend with Vue 3, TypeScript, and Vite. Shared research pages use the versioned `/api/v1` contract; generated API types keep frontend and backend interfaces aligned.
- Use explicit asynchronous workflows and httpx clients, with Pydantic models at the boundaries. Retire obsolete runtime entry points.

### Persistent tasks, cancellation, and recovery

- Make saved searches database-backed runs with worker leases, ordered events, checkpoints, and result artifacts. Community can resume compatible interrupted work from committed checkpoints.
- Restore saved pages from a run snapshot and replayed events, then follow updates through SSE. Reconnecting the browser does not submit a new search.
- Coordinate cancellation and lease ownership so an obsolete worker or a late provider response cannot commit results after cancellation.
- Separate source/model failures from persistence failures: optional retrieval or ranking stages can report warnings and fall back, while a failed result commit cannot be presented as success.
- Introduce versioned SQLite migrations through Alembic and explicit migration commands. Community uses SQLite and local files. A signed-out online search is temporary and is not added to history.

Community can run the API and an embedded worker as one local service, or run them separately. Reconnecting the browser restores the page; an interrupted task resumes from its checkpoint.

### A complete research workspace

- Vue 3 interface with English and Chinese, consistent navigation, and task-stage progress.
- Question-driven query iteration, bounded retrieval, relevance ranking, and transparent search history.
- Eight scholarly source integrations, with source-specific fields and access settings.
- Local fusion, Embedding + Rerank, and System One preliminary-ranking options.
- Citation expansion and an interactive citation map through OpenAlex.
- Saved runs, filtering, selection, and complete CSV/JSON exports.
- **Experimental** abstract-based reviews with editable outlines, section writing, versions, citations, and Markdown/Word/BibTeX exports.

### One application, several entry points

The Web UI, six-command CLI, versioned HTTP API, and independent same-version MCP adapter reuse the same application services. Community keeps data in local SQLite and files. The online product adds accounts, service models, queueing, and isolated storage.

### Upgrade notes

Python **3.12+** is required. Old Python imports, internal APIs, frontend endpoints, and retired CLI/configuration entries are not retained. Supported 0.2 data and environment settings have explicit migration commands; migrated configuration uses the new names without a legacy runtime fallback. See the [migration guide](self-hosting/upgrade.md) and [architecture notes](developers/contributing.md#architecture).

Search uses abstracts and metadata provided by the source. Standalone cross-source abstract and citation-count enrichment are no longer part of the configuration.

## 0.2.1 {#v0-2-1}

July 5, 2026 · Provider routing and hosted service updates

### Models and retrieval

- Expanded provider routing with NVIDIA NIM and OpenRouter support.
- Updated PaperHub query handling and candidate-pool narrowing.

### Reliability

- Corrected NVIDIA reranker endpoint handling and improved ranking fallbacks.
- Added retries for transient API failures.
- Unified shared LLM route logging across providers.

### Documentation

- Expanded PaperSeek Service documentation, including hosted data-source credentials and release notes.

[Original release notes](https://github.com/paperseek/paperseek/releases/tag/v0.2.1)

## 0.2.0 {#v0-2-0}

July 4, 2026 · More sources and multi-lane retrieval

### Search and ranking

- Expanded the source lineup with arXiv, Semantic Scholar, PubMed, computer-science conferences, and Crossref.
- Introduced source-specific queries, intent recognition, and feedback across query iterations.
- Added preliminary ranking before LLM scoring. Retrieval combines relevance, influence or citations, freshness, and local quality signals, using RRF, BM25/term coverage, and local hashing embeddings.
- Added optional OpenAI-compatible embedding and reranker services.

### Online accounts

- Updated email registration and sign-in, with GitHub and ModelScope OAuth.
- Introduced a daily allowance of 20 successful searches for signed-in users at that release.

[Original release notes](https://github.com/paperseek/paperseek/releases/tag/v0.2.0)

## 0.1.1 {#v0-1-1}

June 6, 2026 · Discipline filters and bilingual use

### Search filters

- Added source-specific disciplines using OpenAlex Fields and Web of Science Categories.
- Applied OpenAlex discipline filters to both initial retrieval and citation expansion.
- Fixed handling of OpenAlex Field labels containing commas across the CLI, environment settings, API, and interface selections.

### Interface and packaging

- Added Chinese/English switching and refreshed user and deployment documentation.
- Reorganized the community package around its self-contained core.
- Included Docker/runtime packaging fixes, history improvements, and provider updates.

[Original release notes](https://github.com/paperseek/paperseek/releases/tag/v0.1.1)

## 0.1.0 {#v0-1-0}

June 2, 2026 · First demo release

The first release established the literature-discovery workflow:

- Research questions in Chinese or English, LLM-generated queries, and iterative widening or narrowing based on source results.
- Metadata collection and LLM relevance scores with brief explanations.
- OpenAlex citation expansion and an interactive map with dragging, zooming, and panning.
- Web results with search, filters, selection, and CSV export.
- Local Web UI, command-line use, Docker deployment, and an optional Agent Skill.
- Support for hosted model providers, compatible endpoints, and local Ollama.

The demo release linked to its commit history without a separate feature list. This summary follows the [README at the 0.1.0 tag](https://github.com/paperseek/paperseek/blob/v0.1.0/README.md).

[Original release](https://github.com/paperseek/paperseek/releases/tag/v0.1.0) · [Code history](https://github.com/paperseek/paperseek/commits/v0.1.0/)
