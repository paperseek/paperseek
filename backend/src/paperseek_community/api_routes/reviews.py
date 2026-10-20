from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, Header, Query, Response

from paperseek_app.configuration import LLM_PROVIDERS, resolve_llm_route
from paperseek_app.errors import RunNotFound, RunPersistenceError, RunStateError
from paperseek_app.models import RecordRename, RunDetail
from paperseek_app.review.citations import CitationStyle
from paperseek_app.review.exports import export_review
from paperseek_app.review.models import (
    OutlineSave,
    PaperSuggestionRequest,
    PaperSuggestions,
    ReviewCreate,
    ReviewDetail,
    ReviewError,
    ReviewJobRequest,
    ReviewPage,
    ReviewVersions,
    SectionSave,
)
from paperseek_app.review.rendering import render_review
from paperseek_app.execution import CancellationScope, run_blocking
from paperseek_app.review.services import ReviewService, suggest_review_papers
from paperseek_app.services.run_queries import RunQueryService
from paperseek_app.settings import (
    SettingsError,
)
from paperseek_app.submission import CredentialInput, LLMRouteInput
from paperseek_community.api_dependencies import (
    TERMINAL_STATUSES,
    ApiError,
    content_response,
    drain_cleanup_jobs,
)
from paperseek_community.artifacts import FileArtifactStore
from paperseek_community.credential_resolution import resolve_model_credentials
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.model_transport import create_http_client
from paperseek_community.persistence.sqlite_review_jobs import SQLiteReviewUnitOfWork
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_community.workspace_settings import (
    WorkspaceSettingsStore,
)
from paperseek_core.errors import LLMRequestError
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.review.evidence import fingerprint


@contextmanager
def open_reviews(database: Path, credentials: EncryptedRunCredentialStore):
    store = SQLiteReviewUnitOfWork(database, credential_store=credentials)
    try:
        yield store
    except ReviewError as exc:
        raise ApiError(exc.status, exc.code, exc.code) from exc
    except RunNotFound as exc:
        raise ApiError(404, "run_not_found", "Source run not found.") from exc
    except RunStateError as exc:
        raise ApiError(409, "run_not_cancellable", "The task is no longer cancellable.") from exc
    except ValueError as exc:
        raise ApiError(422, "review_invalid_content", "The review content or citations are invalid.") from exc
    except RunPersistenceError as exc:
        raise ApiError(500, "review_persistence_failed", "The review could not be saved.") from exc
    finally:
        store.close()


def review_view(review: ReviewDetail, citation_style: CitationStyle = "numeric") -> ReviewDetail:
    return review.model_copy(update={"document": render_review(review, citation_style)})


def register_review_routes(app: FastAPI, *, database: Path, settings: CommunityDeploymentSettings,
                           credentials: EncryptedRunCredentialStore, accept_jobs: bool,
                           workspace: WorkspaceSettingsStore) -> None:
    @app.post("/api/v1/reviews", status_code=201, response_model=ReviewDetail)
    def create_review(request: ReviewCreate,
                      idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
        with open_reviews(database, credentials) as store:
            queries = RunQueryService(store, FileArtifactStore(database.parent))
            return review_view(ReviewService(store, store, queries).create(request, idempotency_key))

    @app.get("/api/v1/reviews", response_model=ReviewPage)
    def list_reviews(offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100),
                     query: str = Query(default="", max_length=200)):
        with open_reviews(database, credentials) as store:
            return store.list_reviews(offset, limit, query)

    @app.patch("/api/v1/reviews/{review_id}", status_code=204)
    def rename_review(review_id: str, request: RecordRename):
        with open_reviews(database, credentials) as store:
            store.rename_review(review_id, request.title)
        return Response(status_code=204)

    @app.get("/api/v1/reviews/{review_id}", response_model=ReviewDetail)
    def get_review(review_id: str, citation_style: CitationStyle = "numeric"):
        with open_reviews(database, credentials) as store:
            return review_view(store.get_review(review_id), citation_style)

    @app.put("/api/v1/reviews/{review_id}/outline", response_model=ReviewDetail)
    def save_outline(review_id: str, request: OutlineSave):
        with open_reviews(database, credentials) as store:
            return review_view(store.save_outline(review_id, request))

    @app.put("/api/v1/reviews/{review_id}/sections/{section_id}", response_model=ReviewDetail)
    def save_section(review_id: str, section_id: str, request: SectionSave):
        with open_reviews(database, credentials) as store:
            return review_view(store.save_section(review_id, section_id, request))

    @app.get("/api/v1/reviews/{review_id}/versions/{part}", response_model=ReviewVersions)
    def get_versions(review_id: str, part: str):
        with open_reviews(database, credentials) as store:
            return ReviewVersions(items=store.review_versions(review_id, part))

    def model_route(request: ReviewJobRequest | PaperSuggestionRequest, fallback: dict):
        if request.connection_id:
            try:
                route, key = workspace.connection_secret(request.connection_id, "llm")
            except SettingsError as exc:
                raise ApiError(exc.status, exc.code, exc.code) from exc
            request = request.model_copy(update={
                "llm": LLMRouteInput(**route), "credential": CredentialInput(api_key=key), "connection_id": "",
            })
        overrides = {key: value for key, value in request.llm.model_dump().items() if value}
        route = resolve_llm_route(settings, overrides or fallback)
        url = urlsplit(route["base_url"])
        if (route["provider"] not in LLM_PROVIDERS or route["api_type"] not in {
            "openai_chat", "openai_responses", "anthropic_messages",
        } or url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or not route["model"]):
            raise ReviewError("review_model_configuration_invalid", status=422)
        temporary = {"llm": request.credential.model_dump()}
        if not resolve_model_credentials(settings, route, {}, temporary)["llm"]:
            raise ReviewError("review_credential_required", status=422)
        return request, route, temporary

    @app.post("/api/v1/reviews/paper-suggestions", response_model=PaperSuggestions)
    async def suggest_papers(request: PaperSuggestionRequest):
        def prepare():
            with open_reviews(database, credentials) as store:
                run = store.get_run(str(request.source_run_id))
                checked, route, temporary = model_route(request, dict(run.config_snapshot.get("llm") or {}))
                candidates = ReviewService(store, store).suggestion_candidates(str(request.source_run_id))
                return checked, route, resolve_model_credentials(settings, route, {}, temporary)["llm"], candidates

        checked, route, key, candidates = await run_blocking(prepare)
        client = create_http_client([route], workspace.proxy_policy(settings))
        try:
            llm = HttpLLMAdapter(client=client, cancellation=CancellationScope(), api_type=route["api_type"],
                                 base_url=route["base_url"], model=route["model"], api_key=key)
            return await suggest_review_papers(llm, checked, candidates)
        except ReviewError as exc:
            raise ApiError(exc.status, exc.code, exc.code) from exc
        except LLMRequestError as exc:
            raise ApiError(502, exc.code, exc.code) from exc
        finally:
            await client.aclose()

    @app.post("/api/v1/reviews/{review_id}/jobs", status_code=202, response_model=RunDetail)
    def submit_job(review_id: str, request: ReviewJobRequest,
                   idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
        task = app.state.worker_task
        if not accept_jobs or (app.state.worker_required and task is not None and task.done()):
            raise ApiError(503, "executor_unavailable", "The worker is unavailable.")
        with open_reviews(database, credentials) as store:
            review = store.get_review(review_id)
            request, route, temporary = model_route(request, dict(review.content.model_route))
            digest = fingerprint({"request": request.model_dump(mode="json", exclude={"credential", "llm"}),
                                  "route": route, "credential_digest": fingerprint(temporary)})
            return store.submit_review_job(review_id, request, route=route, credentials=temporary,
                                           idempotency_key=idempotency_key, request_digest=digest)

    @app.get("/api/v1/reviews/{review_id}/jobs/{job_id}", response_model=RunDetail)
    def get_job(review_id: str, job_id: str):
        with open_reviews(database, credentials) as store:
            return store.get_review_job(review_id, job_id)

    @app.post("/api/v1/reviews/{review_id}/jobs/{job_id}/cancel", status_code=202, response_model=RunDetail)
    def cancel_job(review_id: str, job_id: str):
        with open_reviews(database, credentials) as store:
            store.get_review_job(review_id, job_id)
            result = store.request_cancel(job_id)
            if result.status in TERMINAL_STATUSES:
                credentials.delete(job_id)
            return result

    @app.get("/api/v1/reviews/{review_id}/export")
    def export(review_id: str, format: Literal["md", "docx", "bibtex"], version: int = Query(ge=1),
               citation_style: CitationStyle = "numeric"):
        with open_reviews(database, credentials) as store:
            review = store.get_review(review_id)
            if review.version != version:
                raise ReviewError("review_version_conflict")
            return content_response(export_review(review, format, citation_style), disposition=True)

    @app.delete("/api/v1/reviews/{review_id}", status_code=204)
    def delete(review_id: str, background_tasks: BackgroundTasks, version: int = Query(ge=1)):
        with open_reviews(database, credentials) as store:
            store.delete_review(review_id, version)
        background_tasks.add_task(drain_cleanup_jobs, database)
        return Response(status_code=204)
