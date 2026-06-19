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
_OPENAI_TOOL_NAME_PREFIX = "sandcastle_tool_"
_OPENAI_GPT5_REASONING_EFFORT = "low"


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

    @staticmethod
    def _argument_property_schema(raw_spec: dict[str, Any], *, required: bool) -> dict[str, Any]:
        prop_type = str(raw_spec.get("type", "string"))
        prop: dict[str, Any] = {
            "type": prop_type if required else [prop_type, "null"],
            "description": str(raw_spec.get("description", ""))[:300],
        }
        if isinstance(raw_spec.get("enum"), list):
            enum_values = [str(item) for item in raw_spec["enum"]]
            prop["enum"] = enum_values if required else [*enum_values, None]
        if "minimum" in raw_spec:
            prop["minimum"] = raw_spec["minimum"]
        if "maximum" in raw_spec:
            prop["maximum"] = raw_spec["maximum"]
        return prop

    @classmethod
    def _argument_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        """Build an OpenAI strict-compatible argument schema for one tool."""
        raw_params = schema.get("parameters", {})
        if not isinstance(raw_params, dict):
            raw_params = {}
        required_params = schema.get("required", [])
        if not isinstance(required_params, list):
            required_params = []
        required_names = {name for name in required_params if isinstance(name, str)}
        properties: dict[str, Any] = {}
        for name, raw_spec in raw_params.items():
            if not isinstance(name, str) or not isinstance(raw_spec, dict):
                continue
            properties[name] = cls._argument_property_schema(
                raw_spec,
                required=name in required_names,
            )
        return {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        }

    @classmethod
    def _tool_call_item_schema(cls, request: ModelRequest) -> dict[str, Any]:
        variants: list[dict[str, Any]] = []
        for schema in request.tool_schemas:
            tool_id = schema.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            variants.append(
                {
                    "type": "object",
                    "properties": {
                        "call_id": {"type": "string"},
                        "tool_id": {"type": "string", "enum": [tool_id]},
                        "arguments": cls._argument_schema(schema),
                    },
                    "required": ["call_id", "tool_id", "arguments"],
                    "additionalProperties": False,
                }
            )
        if not variants:
            raise ModelGatewayResponseError("OpenAI request requires a tool schema")
        return {"anyOf": variants}

    @staticmethod
    def _openai_tool_name(index: int) -> str:
        return f"{_OPENAI_TOOL_NAME_PREFIX}{index + 1}"

    @classmethod
    def _tool_name_map(cls, request: ModelRequest) -> dict[str, str]:
        """Map Responses API-safe function names back to Sandcastle tool IDs."""
        mapping: dict[str, str] = {}
        for index, schema in enumerate(request.tool_schemas):
            tool_id = schema.get("id")
            if isinstance(tool_id, str) and tool_id:
                mapping[cls._openai_tool_name(index)] = tool_id
        if not mapping:
            raise ModelGatewayResponseError("OpenAI request requires a tool schema")
        return mapping

    @classmethod
    def _function_tools(cls, request: ModelRequest) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for index, schema in enumerate(request.tool_schemas):
            tool_id = schema.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            description = str(schema.get("description") or "")[:700]
            tools.append(
                {
                    "type": "function",
                    "name": cls._openai_tool_name(index),
                    "description": f"Sandcastle tool id: {tool_id}. {description}"[:1024],
                    "parameters": cls._argument_schema(schema),
                    "strict": True,
                }
            )
        if not tools:
            raise ModelGatewayResponseError("OpenAI request requires a tool schema")
        return tools

    @staticmethod
    def _uses_reasoning_effort(model_id: str) -> bool:
        return model_id.startswith("gpt-5")

    def _request_body(self, request: ModelRequest) -> dict[str, Any]:
        user_payload = {
            "observation": request.observation,
            "available_tool_ids": self._tool_ids(request),
            "instruction": (
                "Select only configured function tools. Each function description contains "
                "the Sandcastle tool id it represents. For offensive tools, use target_team "
                "from the allowed opponents. For organizer or defensive tools, provide only "
                "the arguments defined by that tool schema. Use null for optional arguments "
                "that are not relevant."
            ),
        }
        body: dict[str, Any] = {
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
            "tools": self._function_tools(request),
            "tool_choice": (
                "required" if request.agent_type.value == "challenge_generator" else "auto"
            ),
            "parallel_tool_calls": request.budget.max_actions_per_round > 1,
            "max_output_tokens": request.budget.max_output_tokens,
        }
        if self._uses_reasoning_effort(self.model_id):
            body["reasoning"] = {"effort": _OPENAI_GPT5_REASONING_EFFORT}
        return body

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

    @staticmethod
    def _incomplete_error(payload: dict[str, Any]) -> str | None:
        if payload.get("status") != "incomplete":
            return None
        details = payload.get("incomplete_details")
        reason = ""
        if isinstance(details, dict):
            reason = str(details.get("reason") or "")
        if reason:
            return f"OpenAI response was incomplete: {reason}"
        return "OpenAI response was incomplete"

    @staticmethod
    def _function_arguments(raw_arguments: object) -> dict[str, Any]:
        if raw_arguments is None or raw_arguments == "":
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str):
            try:
                decoded = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ModelGatewayResponseError(
                    "OpenAI function call arguments were not valid JSON"
                ) from exc
            if isinstance(decoded, dict):
                return decoded
        raise ModelGatewayResponseError("OpenAI function call arguments must be an object")

    def _function_tool_calls(
        self,
        payload: dict[str, Any],
        request: ModelRequest,
    ) -> list[dict[str, Any]]:
        name_to_tool_id = self._tool_name_map(request)
        calls: list[dict[str, Any]] = []
        for index, item in enumerate(payload.get("output", [])):
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name = item.get("name")
            if not isinstance(name, str) or name not in name_to_tool_id:
                raise ModelGatewayResponseError("OpenAI returned an unknown function call")
            arguments = self._function_arguments(item.get("arguments"))
            calls.append(
                {
                    "call_id": str(item.get("call_id") or item.get("id") or f"call-{index + 1}"),
                    "tool_id": name_to_tool_id[name],
                    "arguments": arguments,
                }
            )
        return calls

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
        request: ModelRequest,
        request_id: str | None,
    ) -> ModelResponse:
        incomplete_error = self._incomplete_error(payload)
        if incomplete_error:
            raise ModelGatewayResponseError(incomplete_error)

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

        function_calls = self._function_tool_calls(payload, request)
        if function_calls:
            for item in function_calls:
                item["arguments"] = {
                    key: value for key, value in item["arguments"].items() if value is not None
                }
            return response_from_dict(
                {
                    "model_id": str(payload.get("model") or self.model_id),
                    "tool_calls": function_calls[: request.budget.max_actions_per_round],
                    "usage": usage,
                    "finish_reason": "completed",
                },
                provider=self.provider,
                raw_response=payload,
            )

        if text is None:
            plan = {"tool_calls": []}
        else:
            try:
                plan = json.loads(text)
            except json.JSONDecodeError as exc:
                if text.lstrip().startswith(("{", "[")):
                    raise ModelGatewayResponseError(
                        "OpenAI structured output was not valid JSON"
                    ) from exc
                plan = {"tool_calls": []}
        if not isinstance(plan, dict):
            raise ModelGatewayResponseError("OpenAI structured output must be an object")
        for item in plan.get("tool_calls") or []:
            if isinstance(item, dict) and isinstance(item.get("arguments"), dict):
                item["arguments"] = {
                    key: value for key, value in item["arguments"].items() if value is not None
                }
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
        return self._parse_response(payload, request=request, request_id=request_id)
