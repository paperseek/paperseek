# MCP

Use PaperSeek from an assistant that supports Model Context Protocol. The local MCP server exposes structured tools over the Community application.

## Install the matched MCP package

```sh
python -m pip install "paperseek-mcp==1.0.0b4"
```

The independent adapter requires the matching `paperseek` version. It reuses the Community application instead of implementing a separate search engine.

## Connect over stdio

A typical client's configuration is:

```json
{
  "mcpServers": {
    "paperseek": {
      "command": "paperseek-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

Ensure the client can find the installed executable. Supply model and source environment variables using the client's secret/environment configuration. Do not publish a client config containing real keys.

The MCP process reads the same `PAPERSEEK_*` environment variables as the CLI. Values entered only in the Web form do not configure this separate process, and it does not automatically load `.env`. See [configuration](../reference/configuration.md). Run `paperseek doctor --json` in that environment before connecting; no Web server or PaperSeek account is required.

## Available tools

| Tool | Purpose |
| --- | --- |
| `list_sources` / `get_source_capabilities` | Inspect available sources and their options |
| `start_search` | Start a durable run and receive its ID |
| `get_search_status` / `get_search_events` | Follow status and events |
| `get_search_results` | Read paginated results |
| `get_citation_graph` | Read the citation graph |
| `cancel_search` | Request cancellation |
| `export_results` | Export complete results as JSON, CSV, BibTeX, or RIS |

A connection is not the lifetime of a search. Keep the returned `run_id`, then use it to check status and retrieve results. Follow the returned pagination cursor instead of assuming the first page is complete.

## Search workflow

1. Inspect `list_sources` and `get_source_capabilities` for source and filter IDs.
2. Call `start_search` with your question, source, and budget (`fast`, `medium`, `high`, or `max`). It immediately returns `run_id` and `status`. Reuse a nonempty `idempotency_key` and the same arguments when retrying a submission.
3. Poll `get_search_status` every few seconds. Terminal statuses are `completed`, `completed_with_warnings`, `failed`, and `cancelled`. An empty event page does not indicate completion.
4. Read `get_search_results` until `next_cursor` is `null`. Event and result pages accept 1–200 items. Use `export_results` for the full JSON, CSV, BibTeX, or RIS export.

Keep the MCP process running while its worker executes a search. Closing a connection does not request cancellation; use `cancel_search`. Local runs remain in the selected database and can be read after restarting with the same database. The default is `~/.paperseek/paperseek.db`; set `--database` or `PAPERSEEK_DATABASE` to change it.

Tools return structured JSON and a compatible text representation. Invalid arguments and execution failures use MCP's `isError` result; an unknown tool is a protocol error. Reading results is marked read-only, while starting and cancelling searches declare their side effects. Search may incur charges with your configured providers.

## Local HTTP

```sh
paperseek-mcp --transport streamable-http --host 127.0.0.1 --port 8766 --path /mcp
```

Legacy SSE is available as `--transport sse-legacy` for older clients. Prefer stdio or Streamable HTTP for new configurations.

Non-loopback HTTP requires `PAPERSEEK_MCP_SERVICE_TOKEN`, an HTTPS `PAPERSEEK_MCP_PUBLIC_URL`, and explicit `PAPERSEEK_MCP_ALLOWED_HOSTS` and `PAPERSEEK_MCP_ALLOWED_ORIGINS`. TLS terminates at a reverse proxy. Do not expose an unauthenticated local server publicly.

This HTTP option is for a private Community workspace. If a service token is configured, the client must send `Authorization: Bearer <token>` on every request. It does not implement an OAuth login flow; clients that require automatic OAuth discovery should use stdio instead. All clients of one instance share its database and configured provider credentials, so this is not a public, per-user BYOK endpoint.

## Protocol and troubleshooting

The adapter uses the [official Python SDK](https://github.com/modelcontextprotocol/python-sdk) for MCP discovery, transport, and structured tool messages. It exposes tools; MCP resources and prompts are optional and are not required for this search workflow. Legacy SSE exists for compatibility, not as the default transport.

If a client cannot start the server, use the executable's absolute path and check its environment. For HTTP failures, check the token and Host/Origin allowlists. Server diagnostics go to stderr; stdout is reserved for MCP messages when using stdio. An unexpected internal error returns a short failure message; inspect the server log for details.

## Agent skill

Skill installation, first use, and configuration are covered in the separate [Agent Skill guide](skill.md). Using the Skill through the CLI does not require MCP.
