"""Gemini provider adapter for the Sandcastle model gateway.

Cost-conscious: uses gemini-2.0-flash-lite by default (cheapest Gemini tier).
Reads GEMINI_API_KEY from environment; never stores it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .agent_contracts import ModelProvider, ModelRequest, ModelResponse, ModelUsage, ToolCall

log = logging.getLogger("gemini_provider")

_DEFAULT_MODEL = "gemini-2.0-flash-lite"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Gemini pricing (per million tokens, USD) — update if Google changes rates
_DEFAULT_INPUT_COST_PER_M = 0.075
_DEFAULT_OUTPUT_COST_PER_M = 0.30


def _gemini_url(model_id: str, api_key: str) -> str:
    return f"{_GEMINI_API_BASE}/{model_id}:generateContent?key={api_key}"


def _build_tool_declarations(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Sandcastle tool schemas to Gemini function declarations."""
    declarations = []
    for s in schemas:
        params = s.get("parameters", {})
        props: dict[str, Any] = {}
        for k, v in params.items():
            t = v.get("type", "string")
            gemini_type = {
                "integer": "INTEGER",
                "number": "NUMBER",
                "boolean": "BOOLEAN",
                "string": "STRING",
            }.get(t, "STRING")
            props[k] = {"type": gemini_type, "description": v.get("description", "")}
        decl: dict[str, Any] = {
            "name": s["id"].replace(".", "_"),  # Gemini requires safe identifiers
            "description": s.get("description", "")[:500],
        }
        if props:
            decl["parameters"] = {
                "type": "OBJECT",
                "properties": props,
                "required": s.get("required", []),
            }
        declarations.append(decl)
    return declarations


def _parse_tool_calls(
    candidates: list[dict[str, Any]],
    schema_ids: list[str],
) -> list[ToolCall]:
    """Extract tool calls from Gemini response candidates."""
    calls: list[ToolCall] = []
    for cand in candidates:
        parts = (cand.get("content") or {}).get("parts") or []
        for i, part in enumerate(parts):
            fc = part.get("functionCall")
            if not fc:
                continue
            raw_name: str = fc.get("name", "")
            # Reverse the dot→underscore mapping
            tool_id = raw_name.replace("_", ".", 1)  # only first underscore
            # Find best match in schema_ids
            if tool_id not in schema_ids:
                # Try replacing all underscores back until we find a match
                for sid in schema_ids:
                    if sid.replace(".", "_") == raw_name:
                        tool_id = sid
                        break
            args = dict(fc.get("args") or {})
            # Convert integer arguments
            for k, v in args.items():
                if isinstance(v, float) and v == int(v):
                    args[k] = int(v)
            calls.append(
                ToolCall(
                    call_id=f"gemini-{i}",
                    tool_id=tool_id,
                    arguments=args,
                )
            )
    return calls


class GeminiProvider:
    """Gemini REST API provider for the Sandcastle model gateway."""

    def __init__(
        self,
        api_key: str,
        model_id: str = _DEFAULT_MODEL,
        input_cost_per_million: float = _DEFAULT_INPUT_COST_PER_M,
        output_cost_per_million: float = _DEFAULT_OUTPUT_COST_PER_M,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiProvider requires a non-empty api_key")
        self._api_key = api_key
        self.model_id = model_id
        self._input_cost = input_cost_per_million
        self._output_cost = output_cost_per_million
        self.call_count = 0

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider.GEMINI

    def complete(self, request: ModelRequest, timeout: float = 30.0) -> ModelResponse:
        self.call_count += 1
        schema_ids = [s["id"] for s in (request.tool_schemas or [])]
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": request.system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": json.dumps(
                                {"observation": request.observation},
                                ensure_ascii=False,
                            )[: int(request.budget.max_input_chars)]
                        }
                    ],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": request.budget.max_output_tokens,
                "temperature": 0.2,
                "candidateCount": 1,
            },
        }
        if schema_ids:
            decls = _build_tool_declarations(request.tool_schemas or [])
            payload["tools"] = [{"function_declarations": decls}]
            payload["tool_config"] = {"function_calling_config": {"mode": "ANY"}}

        url = _gemini_url(self.model_id, self._api_key)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            err_body = exc.read()[:1000].decode(errors="replace")
            raise RuntimeError(f"Gemini API error {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini network error: {exc.reason}") from exc

        candidates = body.get("candidates") or []
        tool_calls = _parse_tool_calls(candidates, schema_ids)

        usage_meta = body.get("usageMetadata") or {}
        input_tokens = int(usage_meta.get("promptTokenCount") or 0)
        output_tokens = int(usage_meta.get("candidatesTokenCount") or 0)
        cost = (
            input_tokens * self._input_cost / 1_000_000
            + output_tokens * self._output_cost / 1_000_000
        )

        return ModelResponse(
            provider=ModelProvider.GEMINI,
            model_id=self.model_id,
            tool_calls=tool_calls,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            ),
        )
