# Contributing

PaperSeek welcomes documentation fixes, source-adapter improvements, reproducible bug reports, and focused product changes. Start with a small issue or discuss a substantial behavior change before implementation.

## Repository map

| Location | Responsibility |
| --- | --- |
| `backend/src/paperseek_core` | Retrieval, source clients, ranking, and model protocols |
| `backend/src/paperseek_app` | Application use cases and persistence ports |
| `backend/src/paperseek_community` | SQLite, local artifacts, HTTP server, and CLI |
| `web` | Shared Vue 3 + TypeScript product UI |
| `integrations/mcp` | Independent MCP adapter |
| `integrations/skills` | Agent usage instructions |
| `docs` | Public English/Chinese docs, including online usage |

PaperSeek online is built from the same Community distribution, so fix shared behavior in this repository.

## Architecture {#architecture}

The 1.0 line completes an architectural refactor into a modular monolith with persistent task execution. Research logic, application operations, and deployment-specific adapters have separate responsibilities, while shipping as a usable local application.

### Dependency boundaries

The research core implements query iteration, retrieval, candidate selection, citations, ranking, and source/model protocols. It can use Pydantic and httpx, but does not depend on web routing, account identity, or a database. Application services own run submission, cancellation, execution, and queries; storage is accessed through explicit ports. Community and the online edition supply the concrete storage, credential, network, and deployment policies.

The `paperseek` distribution contains `paperseek_core`, `paperseek_app`, and `paperseek_community`, plus the built Web UI. The core and application layer do not have separate package releases. `paperseek-mcp` is an independently installable, version-matched protocol adapter that calls the same application services.

### Run lifecycle

For a saved search, submission records the effective configuration and creates a run. A worker claims it with a lease, executes the asynchronous workflow, and commits events, checkpoints, and artifact references through application transactions. Final results become visible as a completed run only after the required data has been committed.

The frontend loads the run snapshot and follows sequenced events over SSE. Community recovery uses compatible committed checkpoints, so completed steps or batches can be reused. Cancellation and lease checks prevent outdated execution from committing late results. A checkpoint from an incompatible workflow is rejected rather than interpreted as current state.

Community stores these records in SQLite, with Alembic-managed migrations and local artifact files. The online edition provides PostgreSQL and Storage adapters, account isolation, and its own task-recovery policy. Anonymous online BYOK keeps research content in memory and does not create a durable research run. Keep these differences in the adapters and application policies, outside the search algorithm.

### Shared frontend and releases

Vue 3 + TypeScript pages consume `/api/v1`; the OpenAPI generation script produces API types and model presets. Change the backend contract and regenerate its frontend representation together.

For the changes in each release, see the [changelog](../changelog.md). For existing local installations, follow the [upgrade guide](../self-hosting/upgrade.md). The [workspace maintenance guide](architecture.md) maps the new modules to their source responsibilities and verification points.

## Development setup

Use Python 3.12+, uv, and the Node/npm versions declared in `web/package.json`.

```sh
uv sync --locked --project backend
npm --prefix web ci
npm --prefix web run typecheck
npm --prefix web run build -- --outDir ../backend/src/paperseek_community/static --emptyOutDir
uv run --locked --project backend paperseek serve
```

For frontend iteration, run `npm --prefix web run dev` while the backend is available. Vite proxies `/api` to the local backend at `http://127.0.0.1:8765`; start `paperseek serve` on its default port.

## Checks

```sh
uv run --directory backend --locked ruff check --config pyproject.toml src ../integrations/mcp/src
uv run --directory backend --locked python tests/import_boundaries.py
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_*.py"
uv run --directory backend --locked python scripts/generate_openapi_types.py --check
npm --prefix web run typecheck
npm --prefix web run build
```

Ruff checks production Python for syntax, unused names and selected correctness rules. Mypy currently checks the seven transport, credential and shared-settings modules listed in `backend/pyproject.toml`; it is not a whole-backend type check. Web TypeScript is checked in strict mode.

Run checks appropriate to the changed behavior and consult CI for the complete release checks. Keep provider-backed experiments separate from deterministic tests. Never use a production database for migration or integration tests.

### Test scope and maintenance

Keep a regression at the lowest layer that can detect it. Before adding a test, find the existing behavior owner and extend its cases; do not replay the same workflow through every adapter.

| Boundary | Primary check |
| --- | --- |
| Search, source HTTP mapping, ranking, review evidence | Backend behavior tests with controlled providers |
| SQLite transactions, migrations, cancellation, leases and recovery | Persistence and lifecycle tests, including real database and blocked I/O |
| User interactions, mobile layout and accessibility | Shared Community browser suite |
| Hosted authentication, account isolation, credits and PostgreSQL | Hosted tests against the pinned Community wheel |
| Packaging, CLI startup and MCP transports | Existing installed-product smoke tests and clean Docker builds |

Assert the result that matters to the case: source request mapping, correct ordering, rejected unauthorized access, retained data or an enforced user limit. For extensible responses, check the relevant fields. Prompt hashes, complete preset tables, display wording, private function locations and tests that only exercise their own doubles do not belong in the suite. A change to wording or internal organization should not require updating tests. Use events and controlled clocks for concurrency; timeouts prevent hangs and are not performance benchmarks. Browser tests should use roles, labels and observable outcomes.

Tests use feature names under `backend/tests/behavior/test_*.py`. Start with the owner below; fixtures live in `fixtures/`, and test modules must not import one another.

| Feature | Where to change its cases |
| --- | --- |
| Submission, defaults and query syntax | `test_submission.py`, `test_query_syntax.py`; API/CLI/MCP only check forwarding and error translation |
| Retrieval and models | `test_query_*`, `test_search_*`, `test_candidate_selection.py`, `test_ranking.py`; HTTP mapping and retries live in `test_source_http.py`, `test_model_http.py`, `test_http_retry.py` |
| Review content | `test_review_evidence.py`, `test_review_planning.py`, `test_review_generation.py`, `test_review_selection.py`, `test_review_output.py` |
| Saved reviews and requests | `test_review_storage.py` owns persistence/recovery; `test_review_api.py` owns HTTP, versioning and credential lifetime, without repeating export formatting |
| Run execution | `test_run_*`, `test_worker.py`, `test_results.py`; database migrations remain under `tests/migration` |
| Shared UI | `web/tests/browser`; Hosted browser cases cover account and service integration around these same components |

Keep default-value assertions with the feature that defines them. Other scenarios should supply explicit configuration, compare forwarded inputs, or check only the fields relevant to their outcome. For example, a queue race supplies its own slot count; a change to the deployment default should not alter that race. Extend an existing scenario when it already catches the defect. Do not add a test for a mechanical change merely to increase the count.

During development, run the affected module first:

```sh
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_review*.py" -v -b
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_*.py" -k cancel -v -b
npm --prefix web run test:browser -- reviews.spec.ts
```

CI still runs the behavior suites on Linux and Windows, with source-only lint, type and dependency checks once on Linux. Browser failures retain traces for seven days. Diagnose a failing assertion or uncontrolled fixture before changing expectations; increasing retries or skipping a failing test is not a fix. An intentional behavior change must update its test and user documentation together.

For repository examples, [LangChain keeps tests within each library](https://github.com/langchain-ai/langchain/tree/master/libs/core/tests), while [Dify separates backend unit and integration tests](https://github.com/langgenius/dify/tree/main/api/tests). Their CI selects affected packages or components. Adopt clear ownership and focused local runs without copying their large CI infrastructure. [Vue's public-behavior guidance](https://vuejs.org/guide/scaling-up/testing.html) and [pytest's analysis of flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html) explain assertion scope and isolation.

## Work on the docs

```sh
npm ci
npm run docs:dev
npm run docs:build
npm run docs:check
npm run docs:lint
cd web
npx playwright test --config playwright.docs.config.ts scrolling.spec.ts
```

English pages live at `docs/`; Chinese equivalents at `docs/zh/`. The two trees mirror each other file for file, and `docs:check` fails when a page exists in only one language. Update both languages when behavior changes. Site configuration and theme are in `docs/.vitepress/`.

### Page types

Each page serves one job. Choose the type before writing, and keep the page to that job:

| Type | Answers | Structure | Example |
| --- | --- | --- | --- |
| Tutorial | "How do I do this the first time?" | Prerequisites → steps → observable result → next steps | [local quick start](../get-started/local.md) |
| How-to | "How do I accomplish this task?" | Goal → procedure → verification → caveats | [search](../guide/search.md) |
| Reference | "What are the exact parameters?" | Complete fields, types, defaults, constraints | [configuration](../reference/configuration.md) |
| Explanation | "Why does it work this way?" | Principle → design choices → boundaries and trade-offs | [workspace](../guide/workspace.md) |

When a task guide starts explaining architecture, or a reference page starts teaching, split the page and cross-link instead.

### Style

- Task-first headings: "Configure a source", not "Data source configuration".
- Short sentences. Prefer the concrete command, path, or button name over abstraction.
- No marketing language in guides. State what the software does — and what it does not do.
- UI labels match the product: **Settings › Model APIs** in Community, **Settings › Model service** online.

### Methodological limits and evidence

PaperSeek is a research tool, so limits are part of correct usage:

- Bibliometrics pages must state that an analysis covers the retrieved candidate corpus, not necessarily the complete literature of a field.
- Review pages must distinguish evidence quoted from abstracts, model-generated text, and human-verified findings.
- Every feature page states what it does not guarantee: exhaustive coverage, study-quality assessment, or full-text access.

### Redirects and links

Never delete a published URL. Move the page and leave a noindex stub in its place — see `docs/user-manual.md` for the pattern. The build fails on dead internal links.

Public guides should explain stable product behavior. Production keys, host-specific checklists, test transcripts, and one-off release records do not belong in public docs. Keep generated output outside source control; local builds may use `DOCS_OUT_DIR` and `DOCS_CACHE_DIR`.

For every user-visible change, update the corresponding English and Chinese task guide or reference: how to use it, defaults and limits, and what a failure or incomplete result means. Add a changelog entry under **Unreleased** until the package is published. Internal test organization belongs in this contributing guide, not in the user workflow.

Pull requests build the site to catch errors, and the site updates after a merge. Edit the source pages under `docs/` only.

## Submit a change

Explain the problem, resulting behavior, affected surfaces, and validation. Include before/after screenshots for visible UI changes. Add behavior tests where needed; avoid tests tied only to a temporary file arrangement.

Follow the [Code of Conduct](https://github.com/paperseek/paperseek/blob/main/CODE_OF_CONDUCT.md), preserve attribution, and keep contributions compatible with [Apache-2.0](https://github.com/paperseek/paperseek/blob/main/LICENSE).
