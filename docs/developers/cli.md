# Command line

The `paperseek` command uses the same application services as the Web UI. Configure model and source credentials in the process environment before a provider-backed search.

## Commands

| Command | Purpose |
| --- | --- |
| `serve` | Start the local HTTP server and, by default, a worker |
| `worker` | Run a separate worker against a local database |
| `search` | Run a question from the terminal and return JSON |
| `doctor` | Report configuration and installation diagnostics |
| `migrate` | Migrate local data or convert an old configuration |
| `version` | Print the installed version |

Use `paperseek <command> --help` for the installed command's options.

## Search

```sh
paperseek search "How does open-source funding affect maintenance?" --source openalex --search-budget medium --output json
```

Supported source IDs: `openalex`, `arxiv`, `crossref`, `semanticscholar`, `pubmed`, `wos`, `googlescholar`, `paperhub`, `wanfang`, `nssd`, and `yiigle`.

Budgets are `fast`, `medium`, `high`, and `max`. Use `--field-id` for supported source fields, `--field-hint` for a textual scope, and `--citation-enabled` or `--no-citation-enabled` to select citation expansion where supported.

```sh
paperseek search "Efficient long-context language models" --source arxiv --field-id cs.CL --search-budget fast
```

Model overrides are `--llm-provider`, `--llm-api-type`, `--llm-model`, and `--llm-base-url`. Read credentials from the environment rather than embedding secrets in command history.

## Use a particular database

```sh
paperseek doctor --database /data/paperseek/paperseek.db --json
paperseek serve --database /data/paperseek/paperseek.db --port 8765
```

The default is `~/.paperseek/paperseek.db`. Share the same path when the Web UI, CLI, and MCP should access the same saved runs.

A diagnostic report helps identify missing configuration; it is not proof that every upstream service will accept a subsequent request. See [configuration](../reference/configuration.md) and [migration](../self-hosting/upgrade.md).
