"""OpenAI Responses API adapter for structured Sandcastle tool plans."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from .agent_contracts import ModelProvider, ModelRequest, ModelResponse, ModelUsage
from .model_gateway import (
    ModelGatewayError,
    ModelGatewayResponseError,
    ModelGatewayTimeout,
    response_from_dict,
    safe_raw_response,
)

DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
MAX_PROVIDER_RESPONSE_BYTES = 1_000_000


class OpenAIProvider:
    provider = ModelProvider.OPENAI

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        endpoint: str = DEFAULT_OPENAI_ENDPOINT,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI provider")
        if not model_id:
            raise ValueError("OpenAI model_id is required")
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("OpenAI endpoint must be an HTTP(S) URL")
        for name, value in (
            ("input_cost_per_million", input_cost_per_million),
            ("output_cost_per_million", output_cost_per_million),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        self.api_key = api_key
        self.model_id = model_id
        self.endpoint = endpoint
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million

    @staticmethod
    def _tool_ids(request: ModelRequest) -> list[str]:
        tool_ids = [
            schema["id"]
            for schema in request.tool_schemas
            if isinstance(schema.get("id"), str) and schema["id"]
        ]
        if not tool_ids:
            raise ModelGatewayResponseError("OpenAI request requires a tool schema")
        return sorted(set(tool_ids))

    def _request_body(self, request: ModelRequest) -> dict[str, Any]:
        plan_schema = {
            "type": "object",
            "properties": {
                "tool_calls": {
                    "type": "array",
                    "maxItems": request.budget.max_actions_per_round,
                    "items": {
                        "type": "object",
                        "properties": {
                            "call_id": {"type": "string"},
                            "tool_id": {
                                "type": "string",
                                "enum": self._tool_ids(request),
                            },
                            "arguments": {
                                "type": "object",
                                "properties": {"target_team": {"type": "integer", "minimum": 1}},
                                "required": ["target_team"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["call_id", "tool_id", "arguments"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tool_calls"],
            "additionalProperties": False,
        }
        user_payload = {
            "observation": request.observation,
            "available_tools": request.tool_schemas,
            "instruction": (
                "Return only registered tool calls. Use target_team from the allowed "
                "opponents. An empty tool_calls list is valid."
            ),
        }
        return {
            "model": self.model_id,
            "instructions": request.system_prompt,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                user_payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        }
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sandcastle_tool_plan",
                    "strict": True,
                    "schema": plan_schema,
                }
            },
            "max_output_tokens": request.budget.max_output_tokens,
        }

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> tuple[str | None, bool]:
        refusal = False
        chunks: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "refusal":
                    refusal = True
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        if chunks:
            return "".join(chunks), refusal
        fallback = payload.get("output_text")
        return (fallback if isinstance(fallback, str) else None), refusal

    def _estimated_cost(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.input_cost_per_million is None or self.output_cost_per_million is None:
            return None
        return (
            input_tokens * self.input_cost_per_million
            + output_tokens * self.output_cost_per_million
        ) / 1_000_000

    def _parse_response(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None,
    ) -> ModelResponse:
        text, refusal = self._output_text(payload)
        usage_payload = payload.get("usage") or {}
        if not isinstance(usage_payload, dict):
            raise ModelGatewayResponseError("OpenAI response usage must be an object")
        input_tokens = int(usage_payload.get("input_tokens", 0))
        output_tokens = int(usage_payload.get("output_tokens", 0))
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": self._estimated_cost(input_tokens, output_tokens),
            "provider_request_id": request_id or payload.get("id"),
        }
        if refusal:
            return ModelResponse(
                provider=self.provider,
                model_id=str(payload.get("model") or self.model_id),
                tool_calls=[],
                usage=ModelUsage(**usage),
                finish_reason="refused",
                raw_response=safe_raw_response(payload),
            )
        if text is None:
            raise ModelGatewayResponseError("OpenAI response did not contain output text")
        try:
            plan = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelGatewayResponseError("OpenAI structured output was not valid JSON") from exc
        if not isinstance(plan, dict):
            raise ModelGatewayResponseError("OpenAI structured output must be an object")
        return response_from_dict(
            {
                "model_id": str(payload.get("model") or self.model_id),
                "tool_calls": plan.get("tool_calls"),
                "usage": usage,
                "finish_reason": "completed",
            },
            provider=self.provider,
            raw_response=payload,
        )

    def complete(self, request: ModelRequest, timeout: float) -> ModelResponse:
        body = json.dumps(self._request_body(request), separators=(",", ":")).encode()
        http_request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise ModelGatewayResponseError("OpenAI response exceeded size limit")
                payload = json.loads(raw)
                request_id = response.headers.get("x-request-id")
        except urllib.error.HTTPError as exc:
            raw_error = exc.read(MAX_PROVIDER_RESPONSE_BYTES).decode("utf-8", errors="replace")
            if exc.code == 401:
                raise ModelGatewayError("OpenAI authentication failed") from exc
            if exc.code == 429:
                raise ModelGatewayError("OpenAI rate limit exceeded") from exc
            raise ModelGatewayError(
                f"OpenAI HTTP {exc.code}: {safe_raw_response(raw_error, 500)}"
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ModelGatewayTimeout("OpenAI request timed out") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ModelGatewayTimeout("OpenAI request timed out") from exc
            raise ModelGatewayError(f"OpenAI request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ModelGatewayResponseError("OpenAI response body was not JSON") from exc

        if not isinstance(payload, dict):
            raise ModelGatewayResponseError("OpenAI response body must be an object")
        return self._parse_response(payload, request_id=request_id)
