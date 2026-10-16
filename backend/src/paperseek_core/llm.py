from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from paperseek_core.key_pool import ApiKeyPool
from paperseek_core.http_retry import LOCAL_MODEL_ATTEMPTS, MODEL_ATTEMPTS, failure_details
from paperseek_core.model_http import (
    bearer_headers,
    is_local_model_endpoint,
    is_local_model_provider,
    post_model_json,
)
from paperseek_core.errors import LLMRequestError
from paperseek_core.ports import CancellationToken

# A warm-up only loads a local model; it must never inherit the long generation budget.
WARMUP_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


class HttpLLMAdapter:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        cancellation: CancellationToken,
        api_type: str,
        base_url: str,
        model: str,
        api_key: str,
        provider: str = "",
    ) -> None:
        self.client = client
        self.cancellation = cancellation
        self.api_type = api_type
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_keys = ApiKeyPool(api_key)
        self.provider = str(provider or "").strip().lower()
        # A local runtime loads a model on first use and ignores hosted-provider flags.
        self.is_local = is_local_model_endpoint(self.provider, self.base_url)
        # Only an explicitly local provider shortens the retry budget: an unknown
        # provider reached over loopback may still be a shared compatible server.
        self.attempts = LOCAL_MODEL_ATTEMPTS if is_local_model_provider(self.provider) else MODEL_ATTEMPTS

    async def warmup(self) -> None:
        """Load a locally served model before the first scored call has to wait for it."""
        if not self.is_local or self.api_type != "openai_chat":
            return
        body = {"model": self.model, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 1}
        await self._post(
            f"{self.base_url}/chat/completions",
            body,
            bearer_headers,
            attempt_keys=self.api_keys.attempts(1),
            attempts=1,
            timeout=WARMUP_TIMEOUT,
        )

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        if self.api_type == "anthropic_messages":
            return await self._anthropic(messages, temperature, max_tokens)
        if self.api_type == "openai_responses":
            return await self._responses(messages, temperature, max_tokens)
        if self.api_type != "openai_chat":
            raise LLMRequestError("The configured LLM API type is unsupported.")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        thinking_control = self._thinking_control()
        body.update(thinking_control)
        if self.model.rsplit("/", 1)[-1].lower().startswith("gpt-5.6"):
            body.pop("temperature", None)
        response, api_key = await self._post(
            f"{self.base_url}/chat/completions",
            body,
            bearer_headers,
            allow_status={400, 422},
        )
        if response.status_code in {400, 422}:
            body.pop("response_format")
            for key in thinking_control:
                body.pop(key, None)
            response, _ = await self._post(
                f"{self.base_url}/chat/completions",
                body,
                bearer_headers,
                attempt_keys=(api_key,),
            )
        try:
            choice = (response.json().get("choices") or [])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if content:
                # JSON calls must use the final answer only. A reasoning trace is
                # never a valid JSON response, even when the provider leaves
                # "content" empty (frozen-baseline rule for response_format calls).
                return str(content)
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            raise LLMRequestError("The LLM chat response is invalid.", code="provider_invalid_response") from exc
        if choice.get("finish_reason") == "length":
            raise LLMRequestError(
                "The LLM response was truncated before the final content completed.",
                code="provider_output_truncated",
            )
        raise LLMRequestError(
            "The LLM response did not contain final content.",
            code="provider_empty_content",
        )

    async def _responses(self, messages, temperature: float, max_tokens: int) -> str:
        instructions = "\n\n".join(item["content"] for item in messages if item.get("role") == "system")
        body: dict[str, Any] = {
            "model": self.model,
            "input": [item for item in messages if item.get("role") != "system"],
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if instructions:
            body["instructions"] = instructions
        if self.model.lower().startswith("gpt-5.6"):
            body["reasoning"] = {"effort": "low"}
            body.pop("temperature", None)
        elif self.model.lower().startswith(("o1", "o3", "o4", "gpt-5")):
            # JSON extraction calls never consume a visible reasoning trace.
            body["reasoning"] = {"effort": "minimal"}
        response, _ = await self._post(
            f"{self.base_url}/responses",
            body,
            bearer_headers,
        )
        try:
            payload = response.json()
            if payload.get("output_text"):
                return str(payload["output_text"])
            texts = [
                str(content["text"])
                for item in payload.get("output", [])
                for content in item.get("content", [])
                if content.get("type") in {"output_text", "text"} and content.get("text")
            ]
            return "\n".join(texts) or self._missing()
        except (ValueError, TypeError, AttributeError) as exc:
            raise LLMRequestError("The LLM responses payload is invalid.", code="provider_invalid_response") from exc

    async def _anthropic(self, messages, temperature: float, max_tokens: int) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [item for item in messages if item.get("role") != "system"],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        system = "\n\n".join(item["content"] for item in messages if item.get("role") == "system")
        if system:
            body["system"] = system
        response, _ = await self._post(
            f"{self.base_url}/v1/messages",
            body,
            self._anthropic_headers,
        )
        try:
            texts = [
                str(item["text"])
                for item in response.json().get("content", [])
                if item.get("text")
            ]
            return "\n".join(texts) or self._missing()
        except (ValueError, TypeError, AttributeError) as exc:
            raise LLMRequestError("The Anthropic response is invalid.", code="provider_invalid_response") from exc

    async def _post(
        self,
        url: str,
        body: dict,
        headers: Callable[[str], dict[str, str]],
        *,
        allow_status: set[int] | None = None,
        attempt_keys: tuple[str, ...] | None = None,
        attempts: int | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> tuple[httpx.Response, str]:
        budget = attempts or self.attempts
        if not self.api_keys.available and not url.startswith(("http://127.0.0.1", "http://localhost")):
            raise LLMRequestError("The configured LLM credential is unavailable.", code="provider_authentication_failed")
        try:
            response, used_key = await post_model_json(
                self.client, self.cancellation, url, body,
                attempt_keys or self.api_keys.attempts(budget), headers,
                attempts=budget, timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise self._transport_failure(
                "The LLM endpoint did not answer in time.", "provider_timeout", exc
            ) from exc
        except httpx.RequestError as exc:
            raise self._transport_failure(
                "The LLM endpoint could not be reached.", "provider_connection_failed", exc
            ) from exc
        if response.status_code in (allow_status or set()) or 200 <= response.status_code < 300:
            return response, used_key
        error = LLMRequestError(f"The LLM endpoint returned HTTP {response.status_code}.",
                                code=self._response_error_code(response))
        error.retry_details = response.extensions["paperseek_retry"]
        raise error

    @staticmethod
    def _transport_failure(message: str, code: str, exc: httpx.RequestError) -> LLMRequestError:
        """Keep the bounded-retry facts so a reported failure stays diagnosable."""
        error = LLMRequestError(message, code=code)
        error.retry_details = failure_details(exc)
        return error

    @staticmethod
    def _anthropic_headers(api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _response_error_code(response: httpx.Response) -> str:
        if response.status_code in {401, 403}:
            return "provider_authentication_failed"
        if response.status_code == 402:
            return "provider_quota_exhausted"
        if response.status_code == 429:
            # OpenAI-compatible APIs distinguish billing from rate limits by code/type,
            # never by the provider's free-text message (which may contain private data).
            try:
                payload = response.json()
            except ValueError:
                payload = None
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and "insufficient_quota" in (error.get("code"), error.get("type")):
                return "provider_quota_exhausted"
            return "provider_rate_limited"
        if response.status_code >= 500:
            return "provider_unavailable"
        return "provider_request_rejected"

    def _thinking_control(self) -> dict[str, Any]:
        """Suppress or minimize visible reasoning for JSON extraction calls.

        Scoring and query generation consume only the final JSON answer, so a
        reasoning trace is pure budget cost: on thinking-by-default models it
        can exhaust max_tokens before any final content is produced. Providers
        that reject the parameter fall through the 400/422 retry, which drops
        these fields together with response_format.
        """
        model = self.model.rsplit("/", 1)[-1].lower()
        if self.api_type != "openai_chat":
            return {}
        if self.is_local:
            # Ollama and other local runtimes honour reasoning_effort and ignore the
            # provider-specific flags below, so a thinking-by-default local model would
            # otherwise spend its whole budget on a trace no JSON call can use.
            return {"reasoning_effort": "none"}
        if model == "nemotron-3-super-120b-a12b":
            return {"chat_template_kwargs": {"enable_thinking": False}}
        if "api.deepseek.com" in self.base_url and model.startswith("deepseek-"):
            return {"thinking": {"type": "disabled"}}
        if "dashscope" in self.base_url or model.startswith("qwen"):
            return {"enable_thinking": False}
        if model.startswith("gpt-5.6"):
            return {"reasoning_effort": "low"}
        if model.startswith(("o1", "o3", "o4", "gpt-5")):
            return {"reasoning_effort": "minimal"}
        if "generativelanguage.googleapis.com" in self.base_url and model.startswith("gemini-"):
            return {"reasoning_effort": "low"}
        return {}

    @staticmethod
    def _missing() -> str:
        raise LLMRequestError("The LLM response did not contain text.", code="provider_invalid_response")
