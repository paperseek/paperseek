from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from paperseek_app.build_info import VERSION
from paperseek_app.execution import WorkflowExecution
from paperseek_app.models import RunDetail, RunStatus
from paperseek_app.services import (
    RunExecutionService,
    RunQueryService,
    RunSubmissionService,
)
from paperseek_community.api import create_app
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.composition import CommunityExecutionResolver, community_static_dir
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.diagnostics import community_doctor_report
from paperseek_community.legacy_settings import (
    LegacySettingsMigrationError,
    migrate_legacy_env,
)
from paperseek_community.persistence import (
    SQLiteRunUnitOfWork,
    V02MigrationError,
    migrate_database,
    migrate_v0_2_database,
)
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import WorkspaceSettingsStore
from paperseek_app.source_catalog import SOURCE_BY_ID
from paperseek_app.submission import (
    LLMRouteInput,
    RetrievalRouteInput,
    RunSubmission,
    SubmissionValidationError,
    prepare_run_submission,
)
from paperseek_app.review.execution import ReviewExecutor
from paperseek_community.review_composition import CommunityReviewResolver
from paperseek_community.persistence.sqlite_review_jobs import SQLiteReviewUnitOfWork
from paperseek_app.worker import RunExecutor
from paperseek_core.ports import CancellationToken


ExecutionResolver = Callable[[RunDetail, CancellationToken], WorkflowExecution]
TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNINGS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


def _default_database() -> Path:
    return Path(
        os.environ.get(
            "PAPERSEEK_DATABASE",
            str(Path.home() / ".paperseek" / "paperseek.db"),
        )
    ).expanduser()


def _add_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperseek")
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="Run a durable Community literature search.")
    search.add_argument("question")
    _add_database(search)
    search.add_argument(
        "--source",
        choices=tuple(SOURCE_BY_ID),
        default="openalex",
    )
    search.add_argument(
        "--search-budget",
        choices=("fast", "medium", "high", "max"),
        default="fast",
    )
    search.add_argument("--field-id", action="append", default=[])
    search.add_argument("--field-hint", default="")
    search.add_argument("--wos-db", default=None)
    search.add_argument(
        "--citation-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    search.add_argument("--llm-provider", default="")
    search.add_argument("--llm-api-type", default="")
    search.add_argument("--llm-model", default="")
    search.add_argument("--llm-base-url", default="")
    search.add_argument("--output", choices=("json",), default="json")

    serve = commands.add_parser("serve", help="Serve the Community API and Web application.")
    _add_database(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--no-worker",
        action="store_true",
        help="Accept durable runs without starting an embedded executor.",
    )

    worker = commands.add_parser("worker", help="Run the Community SQLite worker.")
    _add_database(worker)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-seconds", type=float, default=0.25)

    doctor = commands.add_parser("doctor", help="Check local storage and provider configuration.")
    _add_database(doctor)
    doctor.add_argument(
        "--source",
        choices=tuple(SOURCE_BY_ID),
        default="openalex",
    )
    doctor.add_argument("--wos-db", default=None)
    doctor.add_argument("--json", action="store_true")

    migrate = commands.add_parser("migrate", help="Apply Community schema and data migrations.")
    _add_database(migrate)
    migrate.add_argument("--from-v0.2", dest="from_v0_2", type=Path)
    migrate.add_argument("--backup", type=Path)
    migrate.add_argument("--legacy-env", type=Path)
    migrate.add_argument("--output-env", type=Path)

    commands.add_parser("version", help="Print the PaperSeek version.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    deployment_settings: CommunityDeploymentSettings | None = None,
    execution_resolver: ExecutionResolver | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        print(VERSION)
        return 0

    database = (args.database or _default_database()).expanduser().resolve()
    if args.command == "migrate":
        return _migrate(args, database)

    settings = deployment_settings or CommunityDeploymentSettings.from_environment()
    if args.command == "doctor":
        report = community_doctor_report(
            database,
            settings,
            source=args.source,
            wos_db=args.wos_db,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            for check in report["checks"]:
                print(f"{str(check['status']).upper():7} {check['id']}: {check['summary']}")
        return 0 if report["ok"] else 1

    migrate_database(database)
    credentials = EncryptedRunCredentialStore(database)
    workspace = WorkspaceSettingsStore(database, credentials)
    resolver = execution_resolver or CommunityExecutionResolver(
        settings, credentials, proxy=lambda: workspace.proxy_policy(settings))
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            create_app(
                database,
                execution_resolver=None if args.no_worker else resolver,
                accept_submissions_without_worker=args.no_worker,
                static_dir=community_static_dir(),
                deployment_settings=settings,
                credential_store=credentials,
            ),
            host=args.host,
            port=args.port,
        )
        return 0
    if args.command == "worker":
        if args.poll_seconds <= 0:
            raise SystemExit("paperseek worker --poll-seconds must be positive")
        return _worker(
            database,
            credentials,
            resolver,
            once=args.once,
            poll_seconds=args.poll_seconds,
            settings=settings,
        )
    return _search(database, settings, credentials, resolver, args)


def _migrate(args: argparse.Namespace, database: Path) -> int:
    if args.legacy_env is not None:
        if args.from_v0_2 is not None or args.backup is not None:
            raise SystemExit("paperseek migrate accepts one migration source at a time")
        if args.output_env is None:
            raise SystemExit("paperseek migrate --legacy-env requires --output-env")
        try:
            report = migrate_legacy_env(args.legacy_env, args.output_env)
        except LegacySettingsMigrationError as exc:
            print(
                json.dumps(
                    {
                        "error_code": exc.code,
                        "settings": list(exc.settings),
                        "status": "failed",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.output_env is not None:
        raise SystemExit("paperseek migrate --output-env requires --legacy-env")
    if args.from_v0_2 is not None:
        target = args.database.resolve() if args.database is not None else args.from_v0_2.resolve()
        backup = args.backup or args.from_v0_2.with_name(
            f"{args.from_v0_2.name}.v0.2.backup"
        )
        try:
            report = migrate_v0_2_database(args.from_v0_2, target, backup)
        except V02MigrationError as exc:
            print(
                json.dumps(
                    {
                        "error_code": exc.code,
                        "message": str(exc),
                        "status": "failed",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.backup is not None:
        raise SystemExit("paperseek migrate --backup requires --from-v0.2")
    schema_version = migrate_database(database)
    print(json.dumps({"schema_version": schema_version, "status": "migrated"}, sort_keys=True))
    return 0


def _search(
    database: Path,
    settings: CommunityDeploymentSettings,
    credentials: EncryptedRunCredentialStore,
    resolver: ExecutionResolver,
    args: argparse.Namespace,
) -> int:
    submission = RunSubmission(
        question=args.question,
        source=args.source,
        search_budget=args.search_budget,
        field_ids=args.field_id,
        field_hint=args.field_hint,
        wos_db=args.wos_db,
        citation_enabled=args.citation_enabled,
        retrieval=RetrievalRouteInput(
        ),
        llm=LLMRouteInput(
            provider=args.llm_provider,
            api_type=args.llm_api_type,
            model=args.llm_model,
            base_url=args.llm_base_url,
        ),
    )
    try:
        prepared = prepare_run_submission(submission, settings)
    except SubmissionValidationError as exc:
        print(
            json.dumps(
                {"error_code": exc.code, "message": exc.message, "status": "failed"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    uow = SQLiteRunUnitOfWork(database)
    try:
        artifact_store = FileArtifactStore(database.parent)
        submission_service = RunSubmissionService(uow)
        execution_service = RunExecutionService(uow, artifact_store)
        query_service = RunQueryService(uow, artifact_store)
        run = submission_service.submit_run(
            subject="community-cli",
            idempotency_key=str(uuid4()),
            request_digest=prepared.request_digest,
            question=prepared.question,
            source=prepared.source,
            config_snapshot=prepared.config_snapshot,
        )
        executor = RunExecutor(
            execution_service,
            resolver,
            owner_id=f"community-cli-{uuid4()}",
        )
        detail = asyncio.run(_execute_until_terminal(executor, query_service, run.id))
        if detail.status in TERMINAL_STATUSES:
            credentials.delete(detail.id)
        results = []
        cursor = None
        while True:
            page = query_service.get_result_page(detail.id, page_size=200, cursor=cursor)
            results.extend(page.results)
            cursor = page.next_cursor
            if cursor is None:
                break
        print(json.dumps(_result_payload(detail, results), ensure_ascii=False, sort_keys=True))
        return 0 if detail.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS} else 1
    finally:
        uow.close()


async def _execute_until_terminal(
    executor: RunExecutor,
    service: RunQueryService,
    run_id: str,
) -> RunDetail:
    while True:
        detail = service.get_run(run_id)
        if detail.status in TERMINAL_STATUSES:
            return detail
        executed = await executor.run_once()
        if executed is None:
            detail = service.get_run(run_id)
            if detail.status in TERMINAL_STATUSES:
                return detail
            raise RuntimeError("The submitted CLI run could not be claimed.")


def _worker(
    database: Path,
    credentials: EncryptedRunCredentialStore,
    resolver: ExecutionResolver,
    *,
    once: bool,
    poll_seconds: float,
    settings: CommunityDeploymentSettings | None = None,
) -> int:
    uow = SQLiteReviewUnitOfWork(database, claim_kind=None)
    deployment = settings or CommunityDeploymentSettings.from_environment()
    try:
        service = RunExecutionService(uow, FileArtifactStore(database.parent))
        executor = RunExecutor(
            service,
            resolver,
            owner_id=f"community-worker-{uuid4()}",
            review_executor=ReviewExecutor(uow, service, CommunityReviewResolver(
                deployment, credentials, proxy=lambda: WorkspaceSettingsStore(database, credentials).proxy_policy(deployment))),
        )

        async def finished(detail: RunDetail) -> None:
            if detail.status in TERMINAL_STATUSES:
                credentials.delete(detail.id)

        async def work() -> RunDetail | None:
            if once:
                detail = await executor.run_once()
                if detail is not None:
                    await finished(detail)
                return detail
            await executor.serve(max_concurrent=deployment.max_concurrent_runs,
                                 poll_seconds=poll_seconds, on_finished=finished)

        try:
            detail = asyncio.run(work())
        except KeyboardInterrupt:
            return 130
        if once:
            payload = (
                {"run_id": detail.id, "status": detail.status.value}
                if detail is not None
                else {"status": "idle"}
            )
            print(json.dumps(payload, sort_keys=True))
        return 0
    finally:
        uow.close()


def _result_payload(detail: RunDetail, results) -> dict[str, object]:
    return {
        "run_id": detail.id,
        "status": detail.status.value,
        "source": detail.source,
        "result_count": detail.result_count,
        "error_code": detail.error_code,
        "warnings": detail.warnings,
        "results": [
            {
                **item.record.model_dump(mode="json"),
            }
            for item in results
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
