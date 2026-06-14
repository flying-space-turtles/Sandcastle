"""Versioned contracts shared by Sandcastle AI agents and model providers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SUPPORTED_VULNERABILITIES = frozenset({"path_traversal", "command_injection", "sql_injection"})


def _require_id(name: str, value: str) -> None:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} contains unsupported characters")


def _require_non_negative(name: str, value: int | float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _json_object(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a JSON object with string keys")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON-serializable values") from exc
    return value


def canonical_json(value: object) -> str:
    """Return deterministic compact JSON for persistence, hashing, and tests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class AgentType(StrEnum):
    ATTACK_DEFENSE = "attack_defense"
    CHALLENGE_GENERATOR = "challenge_generator"


class ModelProvider(StrEnum):
    FAKE = "fake"
    OPENAI = "openai"
    OLLAMA = "ollama"
    GEMINI = "gemini"


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_id: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id("call_id", self.call_id)
        if not _TOOL_RE.fullmatch(self.tool_id):
            raise ValueError("tool_id must be a dotted lowercase identifier")
        _json_object("arguments", self.arguments)

    def as_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "arguments": self.arguments,
        }


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_id: str
    status: str
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id("call_id", self.call_id)
        if not _TOOL_RE.fullmatch(self.tool_id):
            raise ValueError("tool_id must be a dotted lowercase identifier")
        if self.status not in {"ok", "miss", "error", "skipped", "rejected"}:
            raise ValueError("unsupported tool result status")
        if len(self.summary) > 2000:
            raise ValueError("tool result summary is too large")
        _json_object("data", self.data)

    def as_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
        }


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_negative("input_tokens", self.input_tokens)
        _require_non_negative("output_tokens", self.output_tokens)
        if self.cost_usd is not None:
            _require_non_negative("cost_usd", self.cost_usd)
        if self.provider_request_id:
            _require_id("provider_request_id", self.provider_request_id)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "provider_request_id": self.provider_request_id,
        }


@dataclass(frozen=True)
class BudgetPolicy:
    max_actions_per_round: int = 2
    max_calls_per_round: int = 2
    max_calls_per_match: int = 30
    max_input_chars: int = 20_000
    max_output_tokens: int = 500
    max_cost_usd_per_call: float = 0.05
    max_cost_usd_per_match: float = 0.50
    max_cost_usd_per_day: float = 1.00
    timeout_seconds: float = 15.0
    max_retries: int = 1

    def __post_init__(self) -> None:
        positive_values = {
            "max_actions_per_round": self.max_actions_per_round,
            "max_calls_per_round": self.max_calls_per_round,
            "max_calls_per_match": self.max_calls_per_match,
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd_per_call": self.max_cost_usd_per_call,
            "max_cost_usd_per_match": self.max_cost_usd_per_match,
            "max_cost_usd_per_day": self.max_cost_usd_per_day,
            "timeout_seconds": self.timeout_seconds,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        if self.max_cost_usd_per_call > self.max_cost_usd_per_match:
            raise ValueError("per-call cost limit cannot exceed per-match limit")
        if self.max_cost_usd_per_match > self.max_cost_usd_per_day:
            raise ValueError("per-match cost limit cannot exceed daily limit")

    def as_dict(self) -> dict[str, object]:
        return {
            "max_actions_per_round": self.max_actions_per_round,
            "max_calls_per_round": self.max_calls_per_round,
            "max_calls_per_match": self.max_calls_per_match,
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd_per_call": self.max_cost_usd_per_call,
            "max_cost_usd_per_match": self.max_cost_usd_per_match,
            "max_cost_usd_per_day": self.max_cost_usd_per_day,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }


@dataclass(frozen=True)
class BudgetRejection:
    code: str
    scope: str
    limit: float
    current: float
    requested: float

    def __post_init__(self) -> None:
        if self.scope not in {"call", "round", "run", "match", "day", "input", "output"}:
            raise ValueError("unsupported budget rejection scope")
        for name in ("limit", "current", "requested"):
            _require_non_negative(name, getattr(self, name))

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "scope": self.scope,
            "limit": self.limit,
            "current": self.current,
            "requested": self.requested,
        }


@dataclass(frozen=True)
class ModelRequest:
    agent_id: str
    agent_type: AgentType
    run_id: str
    correlation_id: str
    system_prompt: str
    observation: dict[str, Any]
    tool_schemas: list[dict[str, Any]]
    budget: BudgetPolicy
    match_id: int | None = None
    round_number: int | None = None
    team_id: int | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported model request schema version")
        for name in ("agent_id", "run_id", "correlation_id"):
            _require_id(name, getattr(self, name))
        if not self.system_prompt or len(self.system_prompt) > 12_000:
            raise ValueError("system_prompt must contain 1-12000 characters")
        _json_object("observation", self.observation)
        if any(not isinstance(schema, dict) for schema in self.tool_schemas):
            raise ValueError("tool_schemas must contain JSON objects")
        for name in ("match_id", "round_number", "team_id"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when provided")
        input_chars = len(self.system_prompt) + len(canonical_json(self.observation))
        input_chars += len(canonical_json(self.tool_schemas))
        if input_chars > self.budget.max_input_chars:
            raise ValueError("model request exceeds max_input_chars")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type.value,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "match_id": self.match_id,
            "round_number": self.round_number,
            "team_id": self.team_id,
            "system_prompt": self.system_prompt,
            "observation": self.observation,
            "tool_schemas": self.tool_schemas,
            "budget": self.budget.as_dict(),
        }


@dataclass(frozen=True)
class ModelResponse:
    provider: ModelProvider
    model_id: str
    tool_calls: list[ToolCall]
    usage: ModelUsage = field(default_factory=ModelUsage)
    finish_reason: str = "completed"
    raw_response: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported model response schema version")
        _require_id("model_id", self.model_id)
        if self.finish_reason not in {"completed", "refused", "length", "fallback", "error"}:
            raise ValueError("unsupported finish_reason")
        if self.raw_response is not None and len(self.raw_response) > 32_000:
            raise ValueError("raw_response is too large")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider.value,
            "model_id": self.model_id,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "usage": self.usage.as_dict(),
            "finish_reason": self.finish_reason,
        }


@dataclass(frozen=True)
class AgentMemoryEntry:
    agent_id: str
    run_id: str
    kind: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: int = SCHEMA_VERSION
    agent_type: str = AgentType.ATTACK_DEFENSE

    def __post_init__(self) -> None:
        for name in ("agent_id", "run_id", "kind"):
            _require_id(name, getattr(self, name))
        if len(self.summary) > 2000:
            raise ValueError("memory summary is too large")
        _json_object("data", self.data)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "run_id": self.run_id,
            "kind": self.kind,
            "summary": self.summary,
            "data": self.data,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ChallengeSpec:
    seed: int
    vulnerability: str
    service_name: str = "turtle_notes"
    route_name: str = "export"
    parameter_name: str = "file"
    entity_name: str = "note"
    decoy_endpoints: int = 0
    difficulty: str = "easy"
    template_version: str = "flask-notes-v1"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported challenge spec schema version")
        if not 0 <= self.seed <= 2**63 - 1:
            raise ValueError("seed must be a non-negative signed 64-bit integer")
        if self.vulnerability not in SUPPORTED_VULNERABILITIES:
            raise ValueError("unsupported vulnerability")
        for name in ("service_name", "route_name", "parameter_name", "entity_name"):
            if not _SAFE_NAME_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a safe lowercase identifier")
        if not 0 <= self.decoy_endpoints <= 5:
            raise ValueError("decoy_endpoints must be between 0 and 5")
        if self.difficulty not in {"easy", "medium"}:
            raise ValueError("difficulty must be easy or medium")
        _require_id("template_version", self.template_version)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "seed": self.seed,
            "vulnerability": self.vulnerability,
            "service_name": self.service_name,
            "route_name": self.route_name,
            "parameter_name": self.parameter_name,
            "entity_name": self.entity_name,
            "decoy_endpoints": self.decoy_endpoints,
            "difficulty": self.difficulty,
            "template_version": self.template_version,
        }
