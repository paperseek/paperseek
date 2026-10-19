from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import time

from paperseek_app.configuration import resolve_llm_route
from paperseek_app.models import RunDetail
from paperseek_app.review.execution import ReviewExecution
from paperseek_app.review.models import ReviewError
from paperseek_community.credential_resolution import resolve_model_credentials
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.model_transport import create_http_client
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.ports import CancellationToken
from paperseek_core.sources.proxy import ProxyPolicy


class CommunityReviewResolver:
    def __init__(self, settings: CommunityDeploymentSettings, credentials: EncryptedRunCredentialStore, *,
                 proxy: Callable[[], ProxyPolicy] | None = None) -> None:
        self.settings, self.credentials = settings, credentials
        self.proxy = proxy or (lambda: settings.proxy)

    def __call__(self, run: RunDetail, cancellation: CancellationToken) -> ReviewExecution:
        route = resolve_llm_route(self.settings, dict(run.config_snapshot.get("llm") or {}))
        key = resolve_model_credentials(self.settings, route, {}, self.credentials.resolve(run.id))["llm"]
        if not key:
            raise ReviewError("review_credential_required", status=422)
        client = create_http_client([route], self.proxy())
        return ReviewExecution(
            llm=HttpLLMAdapter(client=client, cancellation=cancellation, api_type=route["api_type"],
                              base_url=route["base_url"], model=route["model"], api_key=key),
            route=route, close=client.aclose,
            deadline_at=time.monotonic() + max(0, (run.deadline_at - datetime.now(timezone.utc)).total_seconds()),
        )
