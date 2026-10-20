# Contributing to PaperSeek

PaperSeek welcomes focused changes to source adapters, application services, diagnostics, tests, and documentation. Preserve the architecture boundary: Community shared code lives under `backend/`, the Vue client under `web/`, and the independent MCP adapter under `integrations/mcp/`.

## Development setup

See the [English development guide](https://docs.paperseek.app/contributing) or
[中文开发指南](https://docs.paperseek.app/zh/contributing) for the repository map,
shared frontend build, and documentation workflow.

```bash
git clone https://github.com/paperseek/paperseek.git
cd paperseek
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./backend
```

On Windows PowerShell, activate with `./.venv/Scripts/Activate.ps1`.

## Local checks

```bash
uv run --directory backend --locked ruff check --config pyproject.toml src ../integrations/mcp/src
uv run --directory backend --locked mypy
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_*.py"
uv run --directory backend --locked python -m unittest discover -s tests/integration -p "*integration.py"
uv run --directory backend --locked python -m unittest discover -s tests/migration -p "*.py"
uv run --directory backend --locked python tests/import_boundaries.py
npm --prefix web run typecheck
npm --prefix web run build
python -m pip wheel ./backend -w dist --no-deps
```

Install Web dependencies with `npm --prefix web ci` before Web checks. For a
usable packaged UI, build with `npm --prefix web run build -- --outDir
../backend/src/paperseek_community/static --emptyOutDir` before creating the wheel.

Install the wheel in a fresh environment outside the repository and verify all six CLI commands, including a controlled provider-backed `search`; fixture-only tests are not product evidence.

Python type checking currently covers the shared HTTP transport, key rotation, credential resolution, and ranking output codec. Expand this checked boundary as modules are changed. Web type checking includes unused locals and parameters.

Keep tests few and meaningful: assert behavior that protects users (credentials, data, cancellation, results) and core journeys, not exact copy, layout details, or every viewport. Keep permanent automated tests and fixtures in the repository. Store private migration records, live API experiments, temporary test output, and credentials outside the source tree; they must not enter distribution archives.

Follow the [test scope and maintenance policy](docs/developers/contributing.md#test-scope-and-maintenance) for layer ownership, focused commands and unstable tests. User-facing changes must include English/Chinese usage documentation and an Unreleased changelog entry; test infrastructure changes belong in contributor documentation.

## Scope and safety

- Extend the owning feature test for a behavior change; do not add a second suite for the same outcome.
- Do not duplicate submission, workflow, provider, ranking, persistence, or tool logic across adapters.
- Do not add compatibility fallbacks for removed commands, Python APIs, configuration names, or runtimes.
- Keep credentials, cookies, private deployment configuration, user data, and full sensitive logs out of commits.
- Do not add paywall bypass or bulk PDF downloading.

Pull requests should explain the behavior change, checks executed, affected product surfaces, and remaining limitations.
