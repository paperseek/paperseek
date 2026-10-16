from __future__ import annotations

from datetime import datetime, timezone
import time

from paperseek_app.configuration import resolve_llm_route
from paperseek_app.models import RunDetail
from paperseek_app.review.execution import ReviewExecution
from paperseek_app.review.models import ReviewError
from paperseek_app.settings import requires_key
from paperseek_community.credential_resolution import resolve_model_credentials
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.model_transport import create_http_client
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.llm import HttpLLMAdapter
from paperseek_core.ports import CancellationToken


class CommunityReviewResolver:
    def __init__(self, settings: CommunityDeploymentSettings, credentials: EncryptedRunCredentialStore) -> None:
        self.settings, self.credentials = settings, credentials

    def __call__(self, run: RunDetail, cancellation: CancellationToken) -> ReviewExecution:
        route = resolve_llm_route(self.settings, dict(run.config_snapshot.get("llm") or {}))
        key = resolve_model_credentials(self.settings, route, {}, self.credentials.resolve(run.id))["llm"]
        if requires_key("llm", str(route.get("provider") or "")) and not key:
            raise ReviewError("review_credential_required", status=422)
        client = create_http_client([route])
        llm = HttpLLMAdapter(client=client, cancellation=cancellation, api_type=route["api_type"],
                             base_url=route["base_url"], model=route["model"], api_key=key,
                             provider=str(route.get("provider") or ""))
        return ReviewExecution(
            llm=llm, route=route, close=client.aclose, warmup=llm.warmup,
            deadline_at=time.monotonic() + max(0, (run.deadline_at - datetime.now(timezone.utc)).total_seconds()),
        )
