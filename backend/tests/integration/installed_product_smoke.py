from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from zipfile import ZipFile

import httpx

from paperseek_app import build_info
from paperseek_app.services import RunQueryService
from paperseek_community.artifacts import FileArtifactStore
import paperseek_community.cli as cli_module
from paperseek_community.persistence import SQLiteRunUnitOfWork


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for the installed product smoke.")
    return value


def _clean_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("PAPERSEEK_")
    }
    environment["PYTHONUTF8"] = "1"
    environment.update(overrides or {})
    return environment


def _run(
    executable: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        cwd=executable.parent.parent,
        env=_clean_environment(environment),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        check=False,
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"serve exited early: {stdout}\n{stderr}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("installed paperseek serve --no-worker did not become healthy")


def main() -> None:
    executable = Path(_required("PAPERSEEK_CLI_EXECUTABLE")).resolve()
    prefix = Path(_required("PAPERSEEK_ISOLATED_PREFIX")).resolve()
    fixture = Path(_required("PAPERSEEK_CLI_FIXTURE")).resolve()
    database = Path(_required("PAPERSEEK_CLI_DATABASE")).resolve()
    if not executable.is_file() or not fixture.is_file():
        raise AssertionError("The installed executable or controlled fixture is missing.")
    for module in (sys.modules[RunQueryService.__module__], cli_module):
        if prefix not in Path(module.__file__).resolve().parents:
            raise AssertionError(f"Product module did not load from the isolated prefix: {module.__file__}")
    with ZipFile(Path(cli_module.__file__).parent / "web_source.zip") as shared_web:
        names = set(shared_web.namelist())
        assert {"src/App.vue", "src/api/client.ts", "src/state/run.ts", "package.json", "package-lock.json"} <= names
        assert all(name.startswith(("src/", "public/")) or name in {"package.json", "package-lock.json", "tools/generate_openapi_types.py"} for name in names)
        assert all(not part.startswith(".") for name in names for part in name.split("/"))

    expected_commit = os.environ.get("PAPERSEEK_EXPECTED_COMMIT", "").strip()
    if expected_commit and build_info.COMMUNITY_COMMIT != expected_commit:
        raise AssertionError(
            f"Installed build commit {build_info.COMMUNITY_COMMIT!r} != {expected_commit!r}."
        )
    expected_dirty = os.environ.get("PAPERSEEK_EXPECTED_DIRTY", "").strip().lower()
    if expected_dirty:
        wanted_dirty = expected_dirty in {"1", "true", "yes"}
        if build_info.SOURCE_TREE_DIRTY is not wanted_dirty:
            raise AssertionError(
                f"Installed dirty marker {build_info.SOURCE_TREE_DIRTY!r} != {wanted_dirty!r}."
            )

    version = _run(executable, "version")
    if version.returncode != 0 or version.stdout.strip() != "1.0.0b4":
        raise AssertionError(version)

    doctor = _run(
        executable,
        "doctor",
        "--database",
        str(database),
        "--source",
        "arxiv",
        "--json",
    )
    doctor_payload = json.loads(doctor.stdout)
    if doctor.returncode != 1 or doctor_payload["network"] != "not_run":
        raise AssertionError((doctor.returncode, doctor_payload, doctor.stderr))

    missing_key = _run(
        executable,
        "search",
        "installed missing credential",
        "--database",
        str(database),
        "--source",
        "arxiv",
        "--output",
        "json",
    )
    missing_payload = json.loads(missing_key.stdout)
    if missing_key.returncode != 1 or missing_payload["error_code"] != "provider_authentication_failed":
        raise AssertionError((missing_key.returncode, missing_payload, missing_key.stderr))

    hook = Path(__file__).with_name("cli_test_hook")
    if not (hook / "sitecustomize.py").is_file():
        raise AssertionError("The installed product test hook is missing.")
    controlled = _run(
        executable,
        "search",
        "durable literature search",
        "--database",
        str(database),
        "--source",
        "openalex",
        "--output",
        "json",
        environment={
            "PYTHONPATH": str(hook),
            "PAPERSEEK_CLI_FIXTURE": str(fixture),
        },
    )
    controlled_payload = json.loads(controlled.stdout)
    if controlled.returncode != 0 or controlled_payload["status"] != "completed":
        raise AssertionError((controlled.returncode, controlled_payload, controlled.stderr))

    uow = SQLiteRunUnitOfWork(database)
    try:
        service = RunQueryService(uow, FileArtifactStore(database.parent))
        results = service.get_result_page(controlled_payload["run_id"], page_size=10).results
        history_ids = {item.id for item in service.get_history_page(page_size=20).items}
    finally:
        uow.close()
    if [item.record.uid for item in results] != ["fixed-001"]:
        raise AssertionError(results)
    if controlled_payload["run_id"] not in history_ids:
        raise AssertionError(history_ids)

    port = _free_port()
    server = subprocess.Popen(
        [
            str(executable),
            "serve",
            "--database",
            str(database),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-worker",
        ],
        cwd=prefix,
        env=_clean_environment(),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(port, server)
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/v1/runs",
            headers={"Idempotency-Key": "installed-no-worker"},
            json={"question": "queued for split worker", "source": "arxiv"},
            timeout=5,
            trust_env=False,
        )
        response.raise_for_status()
        queued_id = response.json()["id"]
        time.sleep(0.25)
        queued = httpx.get(f"http://127.0.0.1:{port}/api/v1/runs/{queued_id}", timeout=5, trust_env=False).json()
        if queued["status"] != "queued":
            raise AssertionError(queued)

        worker = _run(executable, "worker", "--database", str(database), "--once")
        worker_payload = json.loads(worker.stdout)
        if worker.returncode != 0 or worker_payload["run_id"] != queued_id:
            raise AssertionError((worker.returncode, worker_payload, worker.stderr))
        claimed = httpx.get(f"http://127.0.0.1:{port}/api/v1/runs/{queued_id}", timeout=5, trust_env=False).json()
        if claimed["status"] != "failed":
            raise AssertionError(claimed)
    finally:
        server.terminate()
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)

    print(
        json.dumps(
            {
                "completed_run_id": controlled_payload["run_id"],
                "database": str(database),
                "status": "passed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
