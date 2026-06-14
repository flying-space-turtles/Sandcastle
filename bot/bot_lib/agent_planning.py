"""Authenticated planning validation and model gateway orchestration."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agent_contracts import (
    AgentType,
    BudgetPolicy,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from .model_budget import BudgetedModelGateway
from .model_gateway import FakeModelProvider


class PlanningRequestError(ValueError):
    """The caller supplied an invalid or unauthorized planning request."""


@dataclass(frozen=True)
class PlanningCredential:
    deployment_id: str
    team_id: int
    expires_at_epoch: float


@dataclass(frozen=True)
class PlanningIdentity:
    deployment_id: str
    team_id: int
    allowed_targets: frozenset[int]
    allowed_actions: frozenset[str]


class PlanningCredentialStore:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = 86_400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("planning credential ttl must be positive")
        self.path = str(path)
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS planning_credentials (
                    deployment_id TEXT PRIMARY KEY,
                    team_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL,
                    expires_at_epoch REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at_epoch REAL NOT NULL
                )
                """
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, deployment_id: str, team_id: int) -> str:
        secret = secrets.token_urlsafe(32)
        token = f"{deployment_id}.{secret}"
        now = float(self.clock())
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO planning_credentials (
                    deployment_id, team_id, token_hash,
                    expires_at_epoch, active, created_at_epoch
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(deployment_id) DO UPDATE SET
                    team_id = excluded.team_id,
                    token_hash = excluded.token_hash,
                    expires_at_epoch = excluded.expires_at_epoch,
                    active = 1,
                    created_at_epoch = excluded.created_at_epoch
                """,
                (
                    deployment_id,
                    team_id,
                    self._hash(token),
                    now + self.ttl_seconds,
                    now,
                ),
            )
            conn.commit()
        return token

    def validate(self, token: str) -> PlanningCredential | None:
        deployment_id, separator, _secret = token.partition(".")
        if not separator or not deployment_id:
            return None
        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT deployment_id, team_id, token_hash, expires_at_epoch, active
                FROM planning_credentials WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchone()
        if row is None or not row["active"] or row["expires_at_epoch"] <= self.clock():
            return None
        if not hmac.compare_digest(str(row["token_hash"]), self._hash(token)):
            return None
        return PlanningCredential(
            deployment_id=str(row["deployment_id"]),
            team_id=int(row["team_id"]),
            expires_at_epoch=float(row["expires_at_epoch"]),
        )

    def deactivate(self, deployment_id: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE planning_credentials SET active = 0 WHERE deployment_id = ?",
                (deployment_id,),
            )
            conn.commit()


class DeterministicPlanningFakeProvider(FakeModelProvider):
    """Offline provider that chooses the first allowed action and target."""

    def complete(self, request: ModelRequest, timeout: float) -> ModelResponse:
        if self.script:
            return super().complete(request, timeout)
        self.call_count += 1
        targets = request.observation.get("opponent_teams", [])
        tool_id = request.tool_schemas[0]["id"] if request.tool_schemas else None
        calls = []
        if tool_id and targets:
            calls.append(
                ToolCall(
                    call_id=f"fake-{self.call_count}",
                    tool_id=str(tool_id),
                    arguments={"target_team": int(targets[0])},
                )
            )
        return ModelResponse(
            provider=ModelProvider.FAKE,
            model_id=self.model_id,
            tool_calls=calls,
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=0.0),
        )


class AgentPlanningService:
    def __init__(
        self,
        gateway: BudgetedModelGateway,
        *,
        num_teams: int,
        model_id: str,
        budget: BudgetPolicy,
    ) -> None:
        self.gateway = gateway
        self.num_teams = num_teams
        self.model_id = model_id
        self.budget = budget

    @staticmethod
    def _object(value: object, name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlanningRequestError(f"{name} must be an object")
        return value

    def plan(
        self,
        identity: PlanningIdentity,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        observation = self._object(payload.get("observation"), "observation")
        if observation.get("my_team") != identity.team_id:
            raise PlanningRequestError("planning team identity mismatch")
        if observation.get("num_teams") != self.num_teams:
            raise PlanningRequestError("planning arena size mismatch")

        requested_targets = observation.get("opponent_teams")
        if not isinstance(requested_targets, list) or any(
            not isinstance(team, int) for team in requested_targets
        ):
            raise PlanningRequestError("opponent_teams must be an integer list")
        if identity.team_id in requested_targets:
            raise PlanningRequestError("an offensive plan cannot target its own team")
        if not set(requested_targets) <= identity.allowed_targets:
            raise PlanningRequestError("planning request contains a disallowed target")

        raw_schemas = payload.get("action_schemas")
        if not isinstance(raw_schemas, list) or not raw_schemas:
            raise PlanningRequestError("action_schemas must be a non-empty list")
        schemas: list[dict[str, Any]] = []
        for raw_schema in raw_schemas:
            schema = self._object(raw_schema, "action schema")
            action_id = schema.get("id")
            if not isinstance(action_id, str) or action_id not in identity.allowed_actions:
                raise PlanningRequestError("planning request contains a disallowed action")
            schemas.append(
                {
                    "id": action_id,
                    "label": str(schema.get("label", ""))[:160],
                    "description": str(schema.get("description", ""))[:500],
                    "scope": str(schema.get("scope", ""))[:32],
                }
            )

        round_number = observation.get("round_number")
        if round_number is not None and (not isinstance(round_number, int) or round_number <= 0):
            raise PlanningRequestError("round_number must be positive")

        model_request = ModelRequest(
            agent_id=f"team{identity.team_id}-attack-defense",
            agent_type=AgentType.ATTACK_DEFENSE,
            run_id=identity.deployment_id,
            correlation_id=f"{identity.deployment_id}-round-{round_number or 0}",
            system_prompt=(
                "You are Sandcastle's AttackDefenseAgent. Select only registered "
                "actions and only allowed opponent teams. Do not target your own team."
            ),
            observation={
                **observation,
                "opponent_teams": sorted(identity.allowed_targets),
            },
            tool_schemas=schemas,
            budget=self.budget,
            round_number=round_number,
            team_id=identity.team_id,
        )
        result = self.gateway.call(
            model_request,
            model_id=self.model_id,
            estimated_cost_usd=self.budget.max_cost_usd_per_call,
        )

        tasks: list[dict[str, object]] = []
        for call in result.response.tool_calls:
            if call.tool_id not in identity.allowed_actions:
                raise PlanningRequestError("provider selected a disallowed action")
            target_team = call.arguments.get("target_team")
            if not isinstance(target_team, int) or target_team not in identity.allowed_targets:
                raise PlanningRequestError("provider selected a disallowed target")
            tasks.append({"target_team": target_team, "action_id": call.tool_id})
        return {
            "tasks": tasks,
            "tokens_used": result.response.usage.total_tokens,
            "cost_usd": result.response.usage.cost_usd,
            "model_id": result.response.model_id,
            "provider": result.response.provider.value,
            "used_fallback": result.used_fallback,
        }


def parse_planner_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanningRequestError("request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise PlanningRequestError("request body must be a JSON object")
    return payload
