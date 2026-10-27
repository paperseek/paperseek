# PaperSeek MCP

`paperseek-mcp` is the independent Model Context Protocol integration for PaperSeek. It contains no search, provider, ranking, or persistence implementation; it calls the application services supplied by the exact same-version `paperseek` distribution.

<!-- mcp-name: io.github.paperseek/paperseek -->

Install the matched release:

```text
pip install paperseek-mcp==1.0.0b5
```

The default local transport is stdio:

```text
paperseek-mcp --transport stdio
```

Loopback HTTP transports are explicit:

```text
paperseek-mcp --transport streamable-http --host 127.0.0.1 --port 8766 --path /mcp
paperseek-mcp --transport sse-legacy --host 127.0.0.1 --port 8766
```

The legacy SSE transport exists only for clients that have not migrated to Streamable HTTP. A transport session is never the search-run lifetime: `start_search` returns a durable `run_id`, and later status, event, result, citation, cancel, and export calls use that ID.

The local database defaults to `~/.paperseek/paperseek.db`. Override it with `--database` or `PAPERSEEK_DATABASE`.

See the [MCP guide](https://docs.paperseek.app/integrations/mcp) for client configuration, the nine tools, polling, pagination, and troubleshooting. Provider credentials come from the MCP process environment, as with the CLI; run `paperseek doctor --json` in that environment first.

HTTP is a private, shared Community workspace. `PAPERSEEK_MCP_SERVICE_TOKEN` enables a pre-shared Bearer token, not OAuth login or per-user isolation. Non-loopback bindings also require an HTTPS public URL and explicit Host/Origin allowlists. Prefer stdio for local agents.
