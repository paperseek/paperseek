# Run PaperSeek yourself

Community includes the search engine, Web UI, local history, citation maps, and experimental reviews. Choose a Python installation or a persistent Docker service.

Just want to try it out? The [local quick start](../get-started/local.md) walks through a first search step by step.

## Install the Python package

Requires **Python 3.12 or newer**. The package includes the built Web UI; Node.js is only needed when developing the frontend.

```sh
python -m pip install "paperseek==1.0.0b3"
paperseek serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Configure a model and data source in the application — the [local quick start](../get-started/local.md) shows these steps.

To install a checkout instead:

```sh
git clone https://github.com/paperseek/paperseek.git
cd paperseek
python -m pip install ./backend
paperseek serve
```

For a checkout without built Web assets, follow the [development setup](../developers/contributing.md) to build the frontend before packaging.

## Docker Compose

Use the repository's `compose.yaml`:

```sh
git clone https://github.com/paperseek/paperseek.git
cd paperseek
cp .env.example .env
PAPERSEEK_VERSION=1.0.0-beta3 docker compose up -d
```

On PowerShell, replace `cp` with `Copy-Item`, then run `$env:PAPERSEEK_VERSION="1.0.0-beta3"; docker compose up -d`. Edit `.env` with your deployment defaults before starting.

Compose publishes **127.0.0.1:8765** by default; `PAPERSEEK_PORT` changes the host port while the container always uses 8765. The service listens on port **8765** and stores data in the `paperseek-data` volume. The command explicitly pins `1.0.0-beta3`, also the default in the supplied Compose file and `.env.example`. Prereleases do not update `latest`; change `PAPERSEEK_VERSION` deliberately when upgrading.

```sh
docker compose logs -f paperseek
docker compose stop
docker compose start
```

`docker compose down` keeps the named volume. Adding `--volumes` removes it and its saved data.

## Data and credentials

The default local database is `~/.paperseek/paperseek.db`. Use `--database` or `PAPERSEEK_DATABASE` to choose another path.

Keep the complete data directory together, including artifacts and the local credential encryption key. Do not put it in the source repository or a public file share. Submitted runtime credentials are handled separately from saved research content.

The CLI and local server read process environment variables. They do **not** automatically load a nearby `.env`; Docker Compose explicitly loads the file.

## Separate the API and worker

The default `serve` starts the worker for you. For process supervision, run both processes against the same database:

```sh
paperseek serve --database /srv/paperseek/paperseek.db --no-worker
paperseek worker --database /srv/paperseek/paperseek.db
```

Do not start an additional worker unintentionally by omitting `--no-worker`.

## Network access

The default server binds to loopback. Community is designed for a trusted local environment. Binding the CLI with `--host 0.0.0.0` or setting Compose's `PAPERSEEK_HOST=0.0.0.0` exposes it beyond your machine; add appropriate authentication and network controls before sharing access. Changing the listen address does not turn Community into a multi-user service.

## Back up

Stop the API and worker before copying the whole data directory or Docker volume. Restore into a separate location first and verify history, results, and artifacts before replacing a live installation.

Continue with [configuration](../reference/configuration.md) or the [1.0.0-beta3 migration guide](upgrade.md).
