# Upgrade to 1.0.0-beta5

Already running an earlier 1.0 beta? [Upgrade in place](#from-an-earlier-1-0-beta); no migration is needed.

Compared with 0.2, the 1.0 line is a complete architectural refactor with breaking changes to the Python runtime, frontend, local storage, and integrations. It provides an explicit user-data migration path; old Python imports, internal APIs, and frontend endpoints are not compatibility contracts. The rest of this guide moves a 0.2 installation to 1.0.0-beta5.

## What changes for a 0.2 installation

| Area | Requirement when upgrading from 0.2 |
| --- | --- |
| Runtime | Use Python 3.12 or newer; install into a fresh environment |
| Entry points | Use `paperseek serve` and the current six-command CLI; update scripts that call retired commands |
| Integrations | Update clients for `/api/v1`; install the same-version `paperseek-mcp` when needed |
| Configuration | Convert supported old environment names once, review ignored values, then load the new names into the process |
| Storage | Migrate the 0.2 database explicitly; new SQLite schema changes are managed by Alembic |
| Running tasks | Finish or cancel old work before switching versions; do not assume an unfinished old task or checkpoint can resume under the new workflow |

The new application separates the research core, task services, and deployment adapters. Saved runs now use worker leases, events, checkpoints, and artifacts. These changes are described in the [changelog](../changelog.md#v1-0-0-beta) and [architecture notes](../developers/contributing.md#architecture).

This guide covers a Community installation. Using PaperSeek online does not require running a local database migration.

## Before upgrading

1. Finish or cancel active work, then stop the old application and any worker.
2. Back up its database, artifacts, and configuration outside the checkout.
3. Install 1.0.0-beta5 in a separate environment.
4. Test the migrated copy before replacing the old installation.

Keep the original backup until you have checked saved questions, result counts, and exports.

## Migrate a 0.2 database

Use explicit source, destination, and backup paths:

```sh
paperseek migrate --from-v0.2 /backup/old-paperseek.db --database /data/paperseek/paperseek.db --backup /backup/old-paperseek.v02.db
```

Without a separate `--database`, the command targets the source path. Supplying an explicit destination makes a rehearsal easier to inspect.

Review the migration report. Do not replace the original database with an unverified output.

## Convert an old environment file

Configuration conversion is a separate operation:

```sh
paperseek migrate --legacy-env /backup/old.env --output-env /private/paperseek.env
```

Inspect converted and ignored settings. The output is not automatically loaded by `paperseek serve`; export the current variables into its process environment or load them through your service manager.

Do not combine `--legacy-env` with database-migration flags. Keep real keys and migration reports outside Git.

## Verify the new installation

```sh
paperseek version
paperseek doctor --database /data/paperseek/paperseek.db --json
paperseek serve --database /data/paperseek/paperseek.db
```

Open history, inspect a saved run, export its results, and make a small new search with your configured provider.

## From an earlier 1.0 beta {#from-an-earlier-1-0-beta}

1.0.0-beta5 keeps the database schema, configuration, and command line of the earlier 1.0 beta, so it upgrades in place without a migration.

1. Finish or cancel active work, stop the service and any worker, and back up the database.
2. Install the new version:

   ```sh
   python -m pip install --upgrade "paperseek==1.0.0b5"
   ```

   Add `"paperseek-mcp==1.0.0b5"` to the same command if you use the MCP adapter. With Docker, set `PAPERSEEK_VERSION` to `1.0.0-beta5` in `.env`, then run `docker compose pull` and `docker compose up -d`.
3. Start the service and check `paperseek version`.

After the upgrade, up to four searches and reviews run at the same time; set `PAPERSEEK_MAX_CONCURRENT_RUNS` to change that. If your network needs a proxy to reach models or data sources, set it up in **Settings › General › Network proxy** or with the variables in [Network proxy](../reference/configuration.md#network-proxy).

The journal ranking tables download from GitHub on the first search into a `journal-metrics` folder beside the database. Back it up with the database, or let it download again. Results saved before the upgrade have no venue labels; run a search again to label it.

Reviews that have no written sections yet extract evidence again the next time you generate the outline, which uses your writing model once more. Reviews with written sections keep their evidence.

The supplied `compose.yaml` publishes the port on 127.0.0.1 only. If you open PaperSeek from another device, take the new `compose.yaml`, set `PAPERSEEK_HOST=0.0.0.0` in `.env`, and protect access as described in [Network access](index.md#network-access).

## Roll back

Stop the new processes first. Restore the complete pre-upgrade data and the corresponding old application environment. Do not point an older runtime at a database that has already been migrated forward.
