# Management Layer Reference

## Installation

### Check before installing

- Locate `paperseek` with `command -v paperseek` on macOS/Linux or `Get-Command paperseek -ErrorAction SilentlyContinue` in PowerShell, then run `paperseek version`.
- Reuse the user's selected virtual environment or configured executable path. A command missing from PATH does not prove the package is absent.
- This Skill targets 1.0.0-beta4. Do not silently upgrade an existing installation or migrate its data; use the installed command help and the [upgrade guide](https://docs.paperseek.app/self-hosting/upgrade) when a version change is needed.

### Install when missing

Use Python **3.12+** and an isolated environment. If no suitable Python is available, help set it up using the host's supported installation workflow before continuing. Follow the agent environment's installation permissions; if commands cannot be executed, give the user the commands and resume after installation.

For a new local environment, use one of these examples. If the target environment already exists, inspect and reuse it rather than recreating it.

macOS/Linux (`python3 --version` must report 3.12 or newer):

```sh
python3 -m venv "$HOME/.local/share/paperseek/venv"
"$HOME/.local/share/paperseek/venv/bin/python" -m pip install "paperseek==1.0.0b4"
"$HOME/.local/share/paperseek/venv/bin/paperseek" version
"$HOME/.local/share/paperseek/venv/bin/paperseek" doctor --json
```

Windows PowerShell (use an installed Python 3.12+ interpreter):

```powershell
$paperseekVenv = Join-Path $env:LOCALAPPDATA 'PaperSeek/venv'
py -3.12 -m venv $paperseekVenv
& "$paperseekVenv/Scripts/python.exe" -m pip install "paperseek==1.0.0b4"
& "$paperseekVenv/Scripts/paperseek.exe" version
& "$paperseekVenv/Scripts/paperseek.exe" doctor --json
```

Use that environment's executable for subsequent commands. Activation, global PATH changes, and PowerShell execution-policy changes are not required. A published Community wheel includes the Web UI; Node.js is not needed to use it.

If the requested release is unavailable, report the installation failure instead of silently installing an older version. With a user-selected 1.0.0-beta4 source checkout, run `python -m pip install ./backend` from its root using the selected environment's Python. For source frontend builds, follow that checkout's `CONTRIBUTING.md`.

The Skill contains no fallback search runtime. Do not run its reference files as application code. After installation, distinguish successful package setup from missing model/source configuration; `doctor` checks the latter without making a paid search.

### Optional MCP installation

Only for an MCP client, install `paperseek-mcp==1.0.0b4` with the same environment's Python. It requires the exact same-version Community package. Configure the client with the absolute path to `paperseek-mcp` (Windows: `Scripts/paperseek-mcp.exe`) and `--transport stdio`; do not assume a desktop client inherits the terminal's PATH or credentials. Check `paperseek-mcp --help`, then connect and call `list_sources` to verify the tool connection without starting a search.

## Current configuration

Use only current `PAPERSEEK_*` environment names. Common deployment settings are:

- `PAPERSEEK_DATABASE`
- `PAPERSEEK_LLM_API_TYPE`, `PAPERSEEK_LLM_BASE_URL`, `PAPERSEEK_LLM_MODEL`, `PAPERSEEK_LLM_API_KEY`
- `PAPERSEEK_OPENALEX_API_KEY`, `PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY`, `PAPERSEEK_PUBMED_API_KEY`
- `PAPERSEEK_GOOGLE_SCHOLAR_API_KEY`, `PAPERSEEK_WOS_API_KEY`, `PAPERSEEK_WOS_DB`

Never copy secret values into chat, logs, Skill files, documentation, or tests. Doctor reports configuration status and setting names without echoing values.

For a v0.2 env file, perform the explicit one-time conversion:

```bash
paperseek migrate --legacy-env .env.v0.2 --output-env .env
```

Review the name-only report, then load the new file. Runtime settings do not permanently read old names.

## Local service

```bash
paperseek migrate --database paperseek.db
paperseek serve --database paperseek.db --host 127.0.0.1 --port 8765
```

Health endpoints are `/health` and `/ready`; product endpoints are under `/api/v1`. The Vue application uses run-scoped routes such as `/runs/{run_id}`, `/runs/{run_id}/results`, and `/runs/{run_id}/citation-map`.

Use `paperseek serve --no-worker` only when a separate `paperseek worker` process owns execution for the same database.

## Diagnostics

`paperseek doctor --json` is a static readiness check. A warning may mean an optional credential or recommended contact email is absent; a failure means required local configuration or storage is unusable. It does not treat live third-party network success as a default hard requirement.

For an agent tool client, install the exact matched `paperseek-mcp` release and use its durable run tools. The MCP distribution is not part of the Community default Compose service.
