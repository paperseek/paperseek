# Maintaining the research workspace

The Community package is the source of shared Python and Vue implementation. PaperSeek online builds on the same sources; deployment adapters must not copy algorithms or pages.

Shared UI tokens, layout rules, and stylesheet ownership are documented in the [interface guidelines](./design-system.md).

Python paths below are relative to `backend/src/`; Web paths are relative to the repository root.

| Responsibility | Location |
| --- | --- |
| Compact native metadata and document identity | `paperseek_core/bibliometrics/corpus.py` |
| Distributions and coverage | `paperseek_core/bibliometrics/statistics.py` |
| Bounded sparse networks | `paperseek_core/bibliometrics/networks.py` |
| Create/read/export/delete analysis use cases | `paperseek_app/bibliometrics/services.py` |
| Storage contract and small record models | `paperseek_app/bibliometrics/ports.py`, `models.py` |
| Community persistence and HTTP composition | `paperseek_community/persistence/sqlite_bibliometrics.py`, `api_routes/bibliometrics.py` |
| Navigation, menus, module history | `web/src/features/workspace/` |
| Analysis views, controls, browser layout worker | `web/src/features/bibliometrics/` |
| Search draft and reusable model inputs | `web/src/features/search/search-draft.ts`, `web/src/features/search/use-search-configuration.ts`, `web/src/features/settings/ConnectionSheet.vue` |
| Review evidence and saved source graph | `web/src/features/review/ReviewEvidence.vue` |
| Shared settings contracts and validation | `paperseek_app/settings.py` |
| Connectivity checks | `paperseek_app/settings_probes.py`, with deployment-supplied clients and sources |
| Structured review output and repair | `paperseek_core/review/structured.py` |
| Review evidence extraction and planning | `paperseek_core/review/planning.py` |
| Review section generation and checks | `paperseek_core/review/generation.py`; shared instructions in `prompts.py` |

Retrieval keeps metadata for already-returned pages before lane/fusion selection. The ranking inputs retain their existing bounds. A completed run writes a separate `bibliometric_corpus` artifact; result APIs and event streams do not carry the corpus. Community recovery checkpoints preserve it. The online edition's policy can skip checkpoints while still retaining the final corpus artifact.

Creating an analysis copies a compact corpus and overview into independent compressed files. SQL stores summaries, descriptors, and bounded network-cache references. Detail requests read the overview only; network computation and corpus export load the corpus on demand. Deletion first marks the record, then deletes files, then retains a minimal idempotency tombstone so late retries cannot recreate it. Adapters enforce ownership before any file descriptor is exposed to a use case.

Network limits are explicit in the core. API concurrency guards bound simultaneous expensive operations; the browser computes only layout in a worker. Large source payloads, abstracts, full result records, and model credentials are not duplicated into the compact corpus. Do not move heavy analysis into history listing or page-mount effects.

Saved searches and reviews have display titles separate from their question/topic and execution versions. Module history shares UI behavior, while each API keeps its own persistence rules. Unsaved key inputs stay in form memory. Saved connections and source keys are encrypted by `paperseek_community/workspace_settings.py`; `web/src/features/settings/store.ts` caches only their public metadata. Submitted runs receive separate encrypted credential copies. Account changes must reset drafts and owner-scoped settings and history. Restoring a past run never silently changes it to current service defaults.

Run `test_bibliometrics.py`, the retrieval-boundary/recovery contracts, import-boundary checks, frontend type checks, and the offline `web/tests/browser/shell.spec.ts`, `settings.spec.ts`, `records.spec.ts`, and `reviews.spec.ts` fixtures after relevant changes. The opt-in `backend/scripts/benchmark_bibliometrics.py` records synthetic 3,000/10,000-paper costs; it is not a remote server or browser latency benchmark.

## Workspace surfaces

`web/src/features/workspace/WorkspaceShell.vue` owns the header, fully collapsible sidebar, mobile drawer, and main scroll surface. `Sidebar.vue` owns module navigation and history; `WorkspaceSettingsMenu.vue` owns the account menu. `WorkspacePreferences.vue` routes the Model APIs, Data sources, General, and User sections. `web/src/features/settings/SettingsModels.vue`, `ConnectionSheet.vue`, `SettingsSources.vue`, and `SettingsUser.vue` implement saved connections, source configuration, and data management. Model connection drafts are tested without saving; data-source dialog tests call `saveSource(false)` before probing, so a failed probe or cancelling the dialog does not roll back the saved key and options. Both dialogs also provide an explicit Save action; default selections save immediately. Search drafts select saved connection IDs. Global defaults do not replace a saved review's own model controls. `web/src/styles/ui.css` owns tokens and primitives; `web/src/styles/app.css` owns the shell and overlays. The online edition supplies account and history slots through the shared shell; it does not copy the shell.
