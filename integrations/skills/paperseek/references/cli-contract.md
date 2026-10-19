# PaperSeek CLI Contract

The v1.0.0-beta3 Community distribution exposes exactly six product commands:

```text
paperseek search
paperseek serve
paperseek worker
paperseek doctor
paperseek migrate
paperseek version
```

Use `paperseek COMMAND --help` as the installed authority. There is no implicit question command and no legacy `smoke`, `sources`, `history`, or `config` command.

## Search

```bash
paperseek search "QUESTION" \
  --database paperseek.db \
  --source openalex \
  --search-budget medium \
  --output json
```

Supported sources are `openalex`, `arxiv`, `semanticscholar`, `pubmed`, `googlescholar`, `paperhub`, `crossref`, and `wos`. Optional functional switches include repeated `--field-id`, `--field-hint`, `--wos-db`, and `--citation-enabled`. LLM provider, API type, model, and base URL can be selected without placing a key on the command line; keys come from current environment configuration.

Search uses the same submission normalizer, SQLite unit of work, artifact store, executor, and durable run state as the HTTP API. JSON output contains `run_id`, `status`, `source`, `result_count`, `error_code`, `warnings`, and normalized `results`.

## Serve and worker

```bash
paperseek serve --database paperseek.db --host 127.0.0.1 --port 8765
paperseek serve --database paperseek.db --no-worker
paperseek worker --database paperseek.db --once
```

`--no-worker` accepts durable submissions without starting an embedded executor. Run `paperseek worker` against the same SQLite database to claim them. Do not start both an embedded worker and a separate worker unintentionally.

## Doctor

```bash
paperseek doctor --database paperseek.db --source openalex --json
```

Doctor checks local environment, storage, and selected provider configuration without requiring live network success. Its report is structured and never returns secret values.

## Migrate

```bash
paperseek migrate --database paperseek.db
paperseek migrate --from-v0.2 old.db --database paperseek.db --backup old.db.v0.2.backup
paperseek migrate --legacy-env .env.v0.2 --output-env .env
```

SQLite and environment migration are explicit, mutually exclusive operations. Existing targets are not silently overwritten. Unknown source names are listed in `ignored_settings` and ignored; unknown target names, target conflicts, and invalid input are rejected. Reports include names, never secret values.

## Version

```bash
paperseek version
```

The independent `paperseek-mcp` package must use the exact same version as `paperseek`.
