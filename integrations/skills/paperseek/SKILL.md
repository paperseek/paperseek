---
name: paperseek
description: Use local PaperSeek for literature searches, diagnostics, saved results, exports, and citation maps. Check the installation, help install Community when missing, then use its CLI, local Web/API, or matched MCP integration.
license: Apache-2.0
compatibility: Requires Python 3.12+ and PaperSeek Community; includes first-use installation guidance. MCP use additionally requires the exact same-version paperseek-mcp package.
---

# PaperSeek

Use the local PaperSeek Community product. This Skill guides setup and use; it does not bundle a search runtime or call the hosted PaperSeek service.

## References

- Read `references/source-routing.md` to choose among the eleven Community sources.
- Read `references/cli-contract.md` for the six formal CLI commands and durable-run behavior.
- Read `references/management-layer.md` for installation, diagnostics, migration, Web/API, and secret handling.
- If a reference conflicts with the installed command help or `/openapi.json`, trust the installed product and report the documentation drift.

## Default workflow

1. Check `paperseek version` in the user's selected environment. If the command is missing, first check an existing virtual environment or configured executable path, then follow `references/management-layer.md` to install Community. Use the installed executable's absolute path if it is not on PATH.
2. Run `paperseek doctor --json` without live network checks. Resolve missing required configuration before searching; report setting names, never secret values.
3. Choose a source. OpenAlex is the default for broad discovery.
4. Run a durable search with `paperseek search "QUESTION" --source openalex --output json`.
5. Report the run status, source, warnings, result count, and useful normalized paper metadata. Treat relevance scores as screening signals, not proof.
6. For interactive history, filtering, export, and citation maps, use `paperseek serve --database PATH` and the run-scoped Web routes.
7. Install and configure the same-version `paperseek-mcp` only when the user needs an MCP client. CLI use does not require MCP or a running Web server.

## Boundaries

- Do not ask users to paste secrets into chat or write them into Skill files, documentation, tests, or logs.
- Configure current `PAPERSEEK_*` variables locally. Convert a v0.2 env file once with `paperseek migrate --legacy-env OLD --output-env NEW`.
- PaperSeek returns metadata and links; it does not bypass paywalls or bulk-download PDFs.
- Never fabricate missing DOI, authors, abstracts, citation counts, or source results.
- Do not claim a systematic review is complete from a single PaperSeek run.
