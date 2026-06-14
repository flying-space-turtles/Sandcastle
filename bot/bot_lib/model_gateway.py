"""Provider-neutral model gateway for Sandcastle AI agents."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .agent_contracts import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
    canonical_json,
)

MAX_RAW_RESPONSE_CHARS = 32_000
_FLAG_RE = re.compile(r"FLAG\{[a-f0-9]{32}\}", re.IGNORECASE)
_SECRET_FIELD_RE = re.compile(
    r"(?:api[_-]?key|authorization|password|secret|token|credential)",
    re.IGNORECASE,
)


class ModelGatewayError(RuntimeError):
    """Base class for model gateway failures."""


class ModelGatewayTimeout(ModelGatewayError):
    """Provider call exceeded the configured timeout."""


class ModelGatewayResponseError(ModelGatewayError):
    """Provider returned malformed or policy-invalid output."""


class ModelProviderAdapter(Protocol):
    provider: ModelProvider

    def complete(self, request: ModelRequest, timeout: float) -> ModelResponse:
        """Return a validated structured response or raise ModelGatewayError."""
        ...


def _redact(value: Any, key: str = "") -> Any:
    if key and _SECRET_FIELD_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _FLAG_RE.sub("FLAG{<redacted>}", value)
    return value


def safe_raw_response(value: object, limit: int = MAX_RAW_RESPONSE_CHARS) -> str:
    """Serialize, redact, and bound a provider response for diagnostics."""
    if limit <= 0:
        raise ValueError("raw response limit must be positive")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            safe = _FLAG_RE.sub("FLAG{<redacted>}", value)
        else:
            safe = canonical_json(_redact(decoded))
    else:
        safe = canonical_json(_redact(value))
    return safe[:limit]


def response_from_dict(
    payload: dict[str, Any],
    *,
    provider: ModelProvider,
    raw_response: object | None = None,
) -> ModelResponse:
    """Parse an adapter payload into the stable internal response contract."""
    if not isinstance(payload, dict):
        raise ModelGatewayResponseError("provider response must be a JSON object")
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        raise ModelGatewayResponseError("provider response tool_calls must be a list")

    calls: list[ToolCall] = []
    try:
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                raise ValueError("tool call must be an object")
            arguments = item.get("arguments", {})
            calls.append(
                ToolCall(
                    call_id=str(item.get("call_id") or f"call-{index + 1}"),
                    tool_id=str(item["tool_id"]),
                    arguments=arguments,
                )
            )
        usage_payload = payload.get("usage") or {}
        if not isinstance(usage_payload, dict):
            raise ValueError("usage must be an object")
        usage = ModelUsage(
            input_tokens=int(usage_payload.get("input_tokens", 0)),
            output_tokens=int(usage_payload.get("output_tokens", 0)),
            cost_usd=(
                float(usage_payload["cost_usd"])
                if usage_payload.get("cost_usd") is not None
                else None
            ),
            provider_request_id=(
                str(usage_payload["provider_request_id"])
                if usage_payload.get("provider_request_id")
                else None
            ),
        )
        return ModelResponse(
            provider=provider,
            model_id=str(payload.get("model_id") or f"{provider.value}-unknown"),
            tool_calls=calls,
            usage=usage,
            finish_reason=str(payload.get("finish_reason", "completed")),
            raw_response=(safe_raw_response(raw_response if raw_response is not None else payload)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelGatewayResponseError(f"invalid provider response: {exc}") from exc


class FakeModelProvider:
    """Deterministic scripted provider for unit tests and offline CI."""

    provider = ModelProvider.FAKE

    def __init__(
        self,
        script: list[ModelResponse | dict[str, Any] | str | BaseException] | None = None,
        *,
        model_id: str = "fake-v1",
        delay_seconds: float = 0.0,
    ) -> None:
        self.script = list(script or [])
        self.model_id = model_id
        self.delay_seconds = delay_seconds
        self.call_count = 0

    def complete(self, request: ModelRequest, timeout: float) -> ModelResponse:
        del request
        if self.delay_seconds > timeout:
            raise ModelGatewayTimeout(
                f"fake provider delay {self.delay_seconds}s exceeds timeout {timeout}s"
            )
        if self.delay_seconds:
            time.sleep(self.delay_seconds)

        index = self.call_count
        self.call_count += 1
        if index >= len(self.script):
            return ModelResponse(
                provider=self.provider,
                model_id=self.model_id,
                tool_calls=[],
                usage=ModelUsage(),
            )

        entry = self.script[index]
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, ModelResponse):
            if entry.provider is not self.provider:
                raise ModelGatewayResponseError("fake script response has wrong provider")
            return entry
        if isinstance(entry, str):
            try:
                payload = json.loads(entry)
            except json.JSONDecodeError as exc:
                raise ModelGatewayResponseError("fake provider returned invalid JSON") from exc
        else:
            payload = entry
        return response_from_dict(payload, provider=self.provider, raw_response=entry)


@dataclass(frozen=True)
class GatewayResult:
    response: ModelResponse
    provider_attempts: tuple[str, ...]
    used_fallback: bool = False


class ModelGateway:
    """Select providers, enforce bounded retry, and return validated responses."""

    def __init__(
        self,
        adapters: dict[ModelProvider, ModelProviderAdapter],
        *,
        primary_provider: ModelProvider,
        fallback_provider: ModelProvider | None = None,
        max_retries: int = 1,
    ) -> None:
        if primary_provider not in adapters:
            raise ValueError("primary provider adapter is not configured")
        if fallback_provider is not None and fallback_provider not in adapters:
            raise ValueError("fallback provider adapter is not configured")
        if not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        self.adapters = dict(adapters)
        self.primary_provider = primary_provider
        self.fallback_provider = fallback_provider
        self.max_retries = max_retries

    def call(self, request: ModelRequest) -> GatewayResult:
        attempts: list[str] = []
        last_error: ModelGatewayError | None = None
        primary = self.adapters[self.primary_provider]

        for _ in range(self.max_retries + 1):
            attempts.append(self.primary_provider.value)
            try:
                response = primary.complete(request, request.budget.timeout_seconds)
                return GatewayResult(response=response, provider_attempts=tuple(attempts))
            except ModelGatewayError as exc:
                if getattr(exc, "retryable", True) is False:
                    raise
                last_error = exc
            except Exception as exc:
                last_error = ModelGatewayError(
                    f"{self.primary_provider.value} provider failed: {type(exc).__name__}"
                )

        fallback_provider = self.fallback_provider
        if fallback_provider is not None and fallback_provider != self.primary_provider:
            attempts.append(fallback_provider.value)
            fallback = self.adapters[fallback_provider]
            try:
                response = fallback.complete(request, request.budget.timeout_seconds)
            except ModelGatewayError:
                raise last_error or ModelGatewayError("model gateway failed") from None
            except Exception as exc:
                raise last_error or ModelGatewayError(
                    f"{fallback_provider.value} provider failed: {type(exc).__name__}"
                ) from exc
            fallback_response = ModelResponse(
                provider=response.provider,
                model_id=response.model_id,
                tool_calls=response.tool_calls,
                usage=response.usage,
                finish_reason="fallback",
                raw_response=response.raw_response,
            )
            return GatewayResult(
                response=fallback_response,
                provider_attempts=tuple(attempts),
                used_fallback=True,
            )

        raise last_error or ModelGatewayError("model gateway failed")
