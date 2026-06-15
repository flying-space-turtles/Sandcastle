"""Model-backed agent planner adapter for Sandcastle bots.

Architecture — credential boundary
------------------------------------
LLM API keys never enter team containers. The bot calls a planning endpoint
on the bot-controller (host); the controller holds the key and calls the model.

  teamN-ssh (bot.py)
    └─ ModelBackedPlanner
         └─ RemoteModelPlannerAdapter
              └─ POST JSON → bot-controller /plan   (holds LLM API key)
                   └─ LLM provider API

Team containers only need PLAN_ENDPOINT + PLAN_TOKEN (operator credential for
the /plan route, not the LLM key). For tests, FakePlannerAdapter provides
deterministic plans with no network calls and no credentials.

Plan delivery wire contract
----------------------------
POST <PLAN_ENDPOINT>/plan
  Authorization: Bearer <PLAN_TOKEN>
  Content-Type: application/json
  Body: PlannerInput.as_dict()

Response 200:
  {"tasks": [{"target_team": N, "action_id": "..."}],
   "tokens_used": N, "cost_usd": 0.001, "model_id": "..."}

All tasks are validated against the live action registry before execution.
Unknown action IDs and invalid targets are silently dropped with a warning.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from .actions import ACTION_REGISTRY
from .runtime import BotContext, info, warn

# BotTask is imported lazily inside methods to avoid circular-import issues
# when this module is loaded as a side-effect of planners.load_planner().


# ── Exceptions ──────────────────────────────────────────────────────────────


class PlannerError(RuntimeError):
    """Base class for model planner errors."""


class PlannerTimeoutError(PlannerError):
    """Adapter call exceeded the time budget."""


class PlannerBudgetError(PlannerError):
    """Token or cost budget already exhausted; call was skipped."""


# ── Input types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionSchema:
    """Schema for one registered action, sent to the model as context."""

    id: str
    label: str
    category: str
    scope: str
    description: str
    required_capabilities: list[str]
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "scope": self.scope,
            "description": self.description,
            "required_capabilities": self.required_capabilities,
            "parameters": self.parameters,
            "required": self.required,
        }


@dataclass(frozen=True)
class PlannerObservation:
    """Structured game-state snapshot passed to the model each round."""

    my_team: int | None
    num_teams: int
    opponent_teams: list[int]
    capabilities: list[str]
    round_number: int | None = None
    previous_results: list[dict[str, object]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "my_team": self.my_team,
            "num_teams": self.num_teams,
            "opponent_teams": self.opponent_teams,
            "capabilities": self.capabilities,
            "round_number": self.round_number,
            "previous_results": list(self.previous_results),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass(frozen=True)
class BudgetConfig:
    """Hard limits enforced before and after each model call."""

    max_actions_per_round: int = 20
    max_plan_seconds: float = 10.0
    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        if self.max_actions_per_round <= 0:
            raise ValueError("max_actions_per_round must be positive")
        if self.max_plan_seconds <= 0:
            raise ValueError("max_plan_seconds must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "max_actions_per_round": self.max_actions_per_round,
            "max_plan_seconds": self.max_plan_seconds,
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
        }


@dataclass(frozen=True)
class PlannerInput:
    """Complete structured input to the planning adapter."""

    observation: PlannerObservation
    action_schemas: list[ActionSchema]
    budget: BudgetConfig

    def as_dict(self) -> dict[str, object]:
        return {
            "observation": self.observation.as_dict(),
            "action_schemas": [s.as_dict() for s in self.action_schemas],
            "budget": self.budget.as_dict(),
        }


# ── Output type ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RawTask:
    """Unvalidated task from the adapter. Validated before becoming a BotTask."""

    target_team: int
    action_id: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannerOutput:
    """Raw output from an adapter, before registry validation."""

    tasks: list[RawTask]
    tokens_used: int | None = None
    cost_usd: float | None = None
    model_id: str | None = None
    raw_response: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "tasks": [
                {
                    "target_team": t.target_team,
                    "action_id": t.action_id,
                    "arguments": t.arguments,
                }
                for t in self.tasks
            ],
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "model_id": self.model_id,
        }


# ── Validation ───────────────────────────────────────────────────────────────


def validate_plan(
    output: PlannerOutput,
    valid_action_ids: frozenset[str],
    valid_target_teams: set[int],
    budget: BudgetConfig,
) -> tuple[list[RawTask], list[str]]:
    """Check model output against the live registry and budget.

    Returns (accepted, error_messages). Never raises.
    Callers convert accepted RawTask items to BotTask before execution.
    """
    errors: list[str] = []
    accepted: list[RawTask] = []

    if output.tokens_used is not None and budget.max_tokens is not None:
        if output.tokens_used > budget.max_tokens:
            errors.append(f"token budget exceeded: {output.tokens_used} > {budget.max_tokens}")

    if output.cost_usd is not None and budget.max_cost_usd is not None:
        if output.cost_usd > budget.max_cost_usd:
            errors.append(
                f"cost budget exceeded: ${output.cost_usd:.4f} > ${budget.max_cost_usd:.4f}"
            )

    for task in output.tasks:
        if len(accepted) >= budget.max_actions_per_round:
            remaining = len(output.tasks) - len(accepted)
            errors.append(
                f"action budget exhausted at {budget.max_actions_per_round}; "
                f"{remaining} task(s) dropped"
            )
            break

        if task.action_id not in valid_action_ids:
            errors.append(f"unknown action rejected: {task.action_id!r}")
            continue

        action = ACTION_REGISTRY.get(task.action_id)
        if action is not None and action.scope == "target":
            if task.target_team not in valid_target_teams:
                errors.append(f"invalid target team {task.target_team} for {task.action_id!r}")
                continue
        elif (
            action is not None and action.scope == "self" and task.target_team in valid_target_teams
        ):
            errors.append(f"self-scoped action cannot target opponent team {task.target_team}")
            continue

        accepted.append(task)

    return accepted, errors


# ── Adapter protocol ─────────────────────────────────────────────────────────


class ModelPlannerAdapter(Protocol):
    """Provider-neutral interface. Implement to support any LLM backend."""

    def call(self, planner_input: PlannerInput, timeout: float) -> PlannerOutput:
        """Call the model. Raise PlannerTimeoutError on timeout, PlannerError on failure."""
        ...


# ── Fake adapter (deterministic, no network) ─────────────────────────────────


class FakePlannerAdapter:
    """Scripted deterministic adapter for tests. No network, no API key.

    ``script`` is consumed in order. Each element is either:
      - list of RawTask → returned as that call's plan
      - a BaseException subclass or instance → raised on that call

    After the script is exhausted, returns an empty plan.
    ``delay_seconds`` simulates latency and raises PlannerTimeoutError if it
    exceeds the timeout argument passed to call().
    """

    def __init__(
        self,
        script: list[list[RawTask] | type[BaseException] | BaseException] | None = None,
        *,
        delay_seconds: float = 0.0,
        model_id: str = "fake-v1",
        tokens_per_call: int = 100,
        cost_per_call: float | None = None,
    ) -> None:
        self._script = list(script or [])
        self._call_count = 0
        self.delay_seconds = delay_seconds
        self.model_id = model_id
        self.tokens_per_call = tokens_per_call
        self.cost_per_call = cost_per_call

    @property
    def call_count(self) -> int:
        return self._call_count

    def call(self, _planner_input: PlannerInput, timeout: float) -> PlannerOutput:
        if self.delay_seconds > 0:
            if self.delay_seconds > timeout:
                raise PlannerTimeoutError(
                    f"fake adapter delay {self.delay_seconds}s exceeds timeout {timeout}s"
                )
            time.sleep(self.delay_seconds)

        idx = self._call_count
        self._call_count += 1

        if idx < len(self._script):
            entry = self._script[idx]
            if isinstance(entry, BaseException):
                raise entry
            if isinstance(entry, type) and issubclass(entry, BaseException):
                raise entry(f"scripted failure at call {idx}")
            return PlannerOutput(
                tasks=list(entry),
                tokens_used=self.tokens_per_call,
                cost_usd=self.cost_per_call,
                model_id=self.model_id,
            )

        return PlannerOutput(tasks=[], tokens_used=0, cost_usd=None, model_id=self.model_id)


# ── Remote adapter (credentials stay on host) ────────────────────────────────


class RemoteModelPlannerAdapter:
    """Posts the planning request to the bot-controller's /plan endpoint.

    The bot-controller holds the LLM API key; the team container only needs:
      PLAN_ENDPOINT — bot-controller base URL (e.g. http://bot-controller:8080)
      PLAN_TOKEN    — operator token for the /plan route (not the LLM key)
    """

    def __init__(self, endpoint: str, operator_token: str) -> None:
        if not endpoint:
            raise ValueError("PLAN_ENDPOINT must be non-empty")
        self.endpoint = endpoint.rstrip("/") + "/plan"
        self.operator_token = operator_token

    def call(self, planner_input: PlannerInput, timeout: float) -> PlannerOutput:
        body = json.dumps(planner_input.as_dict()).encode()
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.operator_token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data: dict[str, Any] = json.loads(resp.read())
        except urllib.error.URLError as exc:
            msg = str(exc).lower()
            if "timed out" in msg or "timeout" in msg:
                raise PlannerTimeoutError(f"planning endpoint timed out: {exc}") from exc
            raise PlannerError(f"planning endpoint unreachable: {exc}") from exc

        raw_tasks = data.get("tasks", [])
        tasks = []
        for t in raw_tasks:
            if not isinstance(t, dict) or "target_team" not in t or "action_id" not in t:
                continue
            arguments = t.get("arguments", {})
            tasks.append(
                RawTask(
                    target_team=int(t["target_team"]),
                    action_id=str(t["action_id"]),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return PlannerOutput(
            tasks=tasks,
            tokens_used=data.get("tokens_used"),
            cost_usd=data.get("cost_usd"),
            model_id=data.get("model_id"),
            raw_response=json.dumps(data),
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def build_observation(
    ctx: BotContext,
    previous_results: list[dict[str, object]],
    round_number: int | None = None,
    elapsed_seconds: float = 0.0,
) -> PlannerObservation:
    opponents = [t for t in range(1, ctx.num_teams + 1) if t != ctx.my_team]
    return PlannerObservation(
        my_team=ctx.my_team,
        num_teams=ctx.num_teams,
        opponent_teams=opponents,
        capabilities=sorted(ctx.capabilities),
        round_number=round_number,
        previous_results=list(previous_results),
        elapsed_seconds=elapsed_seconds,
    )


def build_action_schemas(ctx: BotContext) -> list[ActionSchema]:
    """Return schemas for actions whose capability requirements are all satisfied."""
    schemas = []
    configured_actions = set(ctx.config.actions)
    for action in ACTION_REGISTRY.values():
        required = frozenset(getattr(action, "required_capabilities", frozenset()))
        if action.id in configured_actions and required <= ctx.capabilities:
            schemas.append(
                ActionSchema(
                    id=action.id,
                    label=action.label,
                    category=action.category,
                    scope=action.scope,
                    description=action.description,
                    required_capabilities=sorted(required),
                    parameters=dict(getattr(action, "parameters", {})),
                    required=list(getattr(action, "required", [])),
                )
            )
    return schemas


def make_model_planner() -> "ModelBackedPlanner":
    """Construct a ModelBackedPlanner from environment variables.

    Required env vars:
      PLAN_ENDPOINT — bot-controller base URL
      PLAN_TOKEN    — operator token for the /plan route

    Optional env vars:
      PLAN_MAX_ACTIONS      max tasks per round (default 20)
      PLAN_TIMEOUT_SECONDS  adapter call timeout in seconds (default 10.0)
      PLAN_MAX_TOKENS       token budget per call (default: unlimited)
      PLAN_MAX_COST_USD     cost budget per call in USD (default: unlimited)
    """
    import os

    endpoint = os.environ.get("PLAN_ENDPOINT", "")
    token = os.environ.get("PLAN_TOKEN", "")
    adapter = RemoteModelPlannerAdapter(endpoint, token)

    max_tokens_raw = os.environ.get("PLAN_MAX_TOKENS", "")
    max_cost_raw = os.environ.get("PLAN_MAX_COST_USD", "")
    budget = BudgetConfig(
        max_actions_per_round=int(os.environ.get("PLAN_MAX_ACTIONS", "20")),
        max_plan_seconds=float(os.environ.get("PLAN_TIMEOUT_SECONDS", "10.0")),
        max_tokens=int(max_tokens_raw) if max_tokens_raw.isdigit() else None,
        max_cost_usd=float(max_cost_raw) if max_cost_raw else None,
    )
    return ModelBackedPlanner(adapter=adapter, budget=budget)


# ── ModelBackedPlanner ───────────────────────────────────────────────────────


class ModelBackedPlanner:
    """Planner that delegates to a ModelPlannerAdapter.

    Drop-in replacement for ScriptedPlanner in the Planner protocol.

    Each call to plan():
      1. Builds a PlannerInput from the current BotContext + prior round results
      2. Calls the adapter with the configured time budget
      3. Validates all returned tasks against the live action registry
      4. Emits planner.model_usage telemetry (tokens, cost, latency)
      5. Yields only the accepted BotTask objects
      6. On any adapter error: logs a warning, yields nothing (no crash)
    """

    id = "model"
    label = "Model-backed agent"
    description = (
        "Delegates planning to an LLM via a remote endpoint. "
        "Validates output against the live action registry before execution. "
        "LLM credentials stay on the host/bot-controller, not in team containers."
    )

    def __init__(
        self,
        adapter: ModelPlannerAdapter,
        budget: BudgetConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.budget = budget or BudgetConfig()
        self._previous_results: list[dict[str, object]] = []
        self._round_number: int = 0

    def targets(self, ctx: BotContext, override_target: int | None = None) -> list[int]:
        if override_target is not None:
            return [override_target]
        return [t for t in range(1, ctx.num_teams + 1) if t != ctx.my_team]

    def plan(self, ctx: BotContext, override_target: int | None = None) -> Iterable[Any]:
        from .planners import BotTask  # lazy to avoid circular import at module load

        start = time.monotonic()
        self._round_number += 1

        valid_targets = set(self.targets(ctx, override_target))
        schemas = build_action_schemas(ctx)
        valid_action_ids = frozenset(schema.id for schema in schemas)

        planner_input = PlannerInput(
            observation=build_observation(
                ctx,
                self._previous_results,
                round_number=self._round_number,
            ),
            action_schemas=schemas,
            budget=self.budget,
        )

        try:
            output = self.adapter.call(planner_input, timeout=self.budget.max_plan_seconds)
        except PlannerTimeoutError as exc:
            warn(f"model planner timed out (round {self._round_number}): {exc}")
            ctx.emit("planner.timeout", round_number=self._round_number, message=str(exc)[:200])
            return
        except PlannerError as exc:
            warn(f"model planner error (round {self._round_number}): {exc}")
            ctx.emit("planner.error", round_number=self._round_number, message=str(exc)[:200])
            return
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            warn(f"model planner unexpected failure: {type(exc).__name__}: {exc}")
            ctx.emit("planner.error", round_number=self._round_number, message=str(exc)[:200])
            return

        elapsed = time.monotonic() - start
        accepted, errors = validate_plan(output, valid_action_ids, valid_targets, self.budget)

        for msg in errors:
            warn(f"model planner: {msg}")

        ctx.emit(
            "planner.model_usage",
            round_number=self._round_number,
            model_id=output.model_id,
            tokens_used=output.tokens_used,
            cost_usd=output.cost_usd,
            plan_seconds=round(elapsed, 3),
            tasks_proposed=len(output.tasks),
            tasks_accepted=len(accepted),
            validation_errors=len(errors),
        )

        info(
            f"model planner round {self._round_number}: "
            f"{len(accepted)}/{len(output.tasks)} task(s) accepted "
            f"({len(errors)} rejected) in {elapsed:.2f}s"
        )

        self._previous_results = [
            {
                "target_team": t.target_team,
                "action_id": t.action_id,
                "arguments": t.arguments,
                "round": self._round_number,
            }
            for t in accepted
        ]

        yield from (BotTask(t.target_team, t.action_id, t.arguments) for t in accepted)
