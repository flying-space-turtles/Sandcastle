"""AI-011: ChallengeGeneratorAgent — organizer-scoped iterative tool loop."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot_lib.agent_contracts import (
    AgentType,
    ChallengeSpec,
    ToolCall,
    ToolResult,
    canonical_json,
)
from bot_lib.agent_memory import AgentMemoryStore, make_tool_result_entry
from bot_lib.agent_telemetry import AgentTelemetry
from bot_lib.challenge_renderer import render as render_spec
from challenge.registry import ChallengeRegistry, PublicationError
from challenge.validator import ChallengeValidator

# Default limits (overridden by arena config)
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_MAX_COST_USD = 0.25

# Tool IDs
_TOOL_CREATE = "challenge.spec.create"
_TOOL_REVISE = "challenge.spec.revise"
_TOOL_RENDER = "challenge.render"
_TOOL_VALIDATE = "challenge.validate"
_TOOL_INSPECT = "challenge.inspect_errors"
_TOOL_PUBLISH = "challenge.publish"
_TOOL_DISCARD = "challenge.discard"

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "id": _TOOL_CREATE,
        "description": "Create a new ChallengeSpec from difficulty, vulnerability kind, and seed.",
        "parameters": {
            "difficulty": {"type": "string", "enum": ["easy", "medium"]},
            "vulnerability": {
                "type": "string",
                "enum": ["path_traversal", "command_injection", "sql_injection"],
            },
            "seed": {"type": "integer", "minimum": 0},
            "decoy_endpoints": {"type": "integer", "minimum": 0, "maximum": 5},
        },
        "required": ["difficulty", "vulnerability", "seed"],
    },
    {
        "id": _TOOL_REVISE,
        "description": "Revise the current ChallengeSpec. Provide only the fields to change.",
        "parameters": {
            "vulnerability": {"type": "string"},
            "seed": {"type": "integer"},
            "decoy_endpoints": {"type": "integer"},
            "difficulty": {"type": "string"},
        },
    },
    {
        "id": _TOOL_RENDER,
        "description": "Render the current ChallengeSpec to a staged candidate.",
        "parameters": {},
    },
    {
        "id": _TOOL_VALIDATE,
        "description": "Run the validation pipeline on the staged candidate.",
        "parameters": {},
    },
    {
        "id": _TOOL_INSPECT,
        "description": "Return a bounded summary of the last validation errors.",
        "parameters": {},
    },
    {
        "id": _TOOL_PUBLISH,
        "description": "Publish the validated candidate to the challenge registry.",
        "parameters": {},
    },
    {
        "id": _TOOL_DISCARD,
        "description": "Discard the current staged candidate and clear state.",
        "parameters": {},
    },
]

ALLOWED_TOOL_IDS = frozenset(s["id"] for s in TOOL_SCHEMAS)


@dataclass
class AgentRunState:
    run_id: str
    agent_id: str
    status: str  # "running" | "published" | "failed" | "cancelled" | "budget_exhausted"
    attempt: int = 0
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    current_spec: dict[str, Any] | None = None
    last_render_id: str | None = None
    last_validation: dict[str, Any] | None = None
    published_challenge_id: str | None = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tool_history: list[dict[str, Any]] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "current_spec": self.current_spec,
            "last_render_id": self.last_render_id,
            "last_validation_status": (self.last_validation or {}).get("status"),
            "published_challenge_id": self.published_challenge_id,
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "tool_calls_count": len(self.tool_history),
        }


class ToolRejectedError(ValueError):
    """Raised when a model-proposed tool call is invalid."""


class ChallengeGeneratorAgent:
    """Organizer-scoped ChallengeGeneratorAgent.

    Iteratively creates, validates, revises, and publishes a ChallengeSpec.
    The model proposes exactly one tool call per iteration; the agent executes
    it, persists the outcome, and repeats until published or stopped.

    No provider key is ever passed to tool arguments or memory entries.
    """

    def __init__(
        self,
        memory: AgentMemoryStore,
        registry: ChallengeRegistry | None = None,
        validator: ChallengeValidator | None = None,
        staging_root: Path | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        max_cost_usd: float = _DEFAULT_MAX_COST_USD,
    ) -> None:
        self.memory = memory
        self.registry = registry or ChallengeRegistry()
        self.validator = validator or ChallengeValidator(docker=False)
        self.staging_root = staging_root
        self.max_attempts = max_attempts
        self.max_cost_usd = max_cost_usd

    # ------------------------------------------------------------------
    # Public: called by bot_api endpoints

    def start(self, request: dict[str, Any]) -> AgentRunState:
        """Start a new challenge generation run from an organizer request."""
        run_id = str(request.get("run_id") or uuid.uuid4().hex[:12])
        agent_id = "challenge-generator"
        state = AgentRunState(
            run_id=run_id,
            agent_id=agent_id,
            status="running",
            max_attempts=int(request.get("max_attempts", self.max_attempts)),
        )
        telem = self._telemetry(state)
        telem.run_started(
            team_id=0,
            provider=str(request.get("provider") or "pending"),
            model_id=str(request.get("model_id") or ""),
        )
        return state

    def execute_tool(self, state: AgentRunState, tool_call: ToolCall) -> ToolResult:
        """Validate and execute a model-proposed tool call. Returns ToolResult."""
        if tool_call.tool_id not in ALLOWED_TOOL_IDS:
            raise ToolRejectedError(f"unknown tool: {tool_call.tool_id!r}")

        state.attempt += 1
        state.touch()

        try:
            result = self._dispatch(state, tool_call)
        except ToolRejectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(
                call_id=tool_call.call_id,
                tool_id=tool_call.tool_id,
                status="error",
                summary=str(exc)[:500],
            )

        # Persist to memory (redacted)
        entry = make_tool_result_entry(
            agent_id=state.agent_id,
            agent_type=AgentType.CHALLENGE_GENERATOR,
            run_id=state.run_id,
            tool_id=tool_call.tool_id,
            call_id=tool_call.call_id,
            status=result.status,
            summary=result.summary,
            data=dict(result.data),
        )
        self.memory.append(entry)
        state.tool_history.append({"tool_id": tool_call.tool_id, "status": result.status})
        return result

    def build_observation(self, state: AgentRunState) -> dict[str, Any]:
        """Build a compact, bounded observation for the next model call."""
        recent = self.memory.recent_as_dicts(state.run_id, limit=5)
        validation_summary = None
        if state.last_validation:
            validation_summary = {
                "status": state.last_validation.get("status"),
                "exploit_succeeded": state.last_validation.get("vulnerable_exploit_succeeded"),
                "checker_before": state.last_validation.get("checker_passed_before_patch"),
                "error": (state.last_validation.get("error") or "")[:300],
            }
        obs: dict[str, Any] = {
            "attempt": state.attempt,
            "max_attempts": state.max_attempts,
            "current_spec": state.current_spec,
            "last_render_id": state.last_render_id,
            "last_validation": validation_summary,
            "recent_results": recent,
        }
        # Hard cap to keep observation small
        serialized = canonical_json(obs)
        if len(serialized) > 8000:
            obs["recent_results"] = recent[:2]
        return obs

    def cancel(self, state: AgentRunState) -> None:
        state.status = "cancelled"
        state.touch()
        self._cleanup_staging(state)

    # ------------------------------------------------------------------
    # Tool dispatch

    def _dispatch(self, state: AgentRunState, call: ToolCall) -> ToolResult:
        tid = call.tool_id
        args = call.arguments

        if tid == _TOOL_CREATE:
            return self._tool_create(state, call, args)
        if tid == _TOOL_REVISE:
            return self._tool_revise(state, call, args)
        if tid == _TOOL_RENDER:
            return self._tool_render(state, call)
        if tid == _TOOL_VALIDATE:
            return self._tool_validate(state, call)
        if tid == _TOOL_INSPECT:
            return self._tool_inspect(state, call)
        if tid == _TOOL_PUBLISH:
            return self._tool_publish(state, call)
        if tid == _TOOL_DISCARD:
            return self._tool_discard(state, call)
        raise ToolRejectedError(f"unhandled tool: {tid!r}")

    def _tool_create(self, state: AgentRunState, call: ToolCall, args: dict) -> ToolResult:
        spec = ChallengeSpec(
            seed=int(args.get("seed", 0)),
            vulnerability=str(args.get("vulnerability", "path_traversal")),
            difficulty=str(args.get("difficulty", "easy")),
            decoy_endpoints=int(args.get("decoy_endpoints", 0)),
        )
        state.current_spec = spec.as_dict()
        state.last_render_id = None
        state.last_validation = None
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            status="ok",
            summary=f"spec created: {spec.vulnerability} seed={spec.seed}",
        )

    def _tool_revise(self, state: AgentRunState, call: ToolCall, args: dict) -> ToolResult:
        if not state.current_spec:
            raise ToolRejectedError("no spec to revise; call challenge.spec.create first")
        merged = {**state.current_spec, **{k: v for k, v in args.items() if v is not None}}
        spec = ChallengeSpec(
            **{k: merged[k] for k in ChallengeSpec.__dataclass_fields__ if k in merged}
        )
        state.current_spec = spec.as_dict()
        state.last_render_id = None
        state.last_validation = None
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            status="ok",
            summary=f"spec revised: {spec.vulnerability} seed={spec.seed}",
        )

    def _tool_render(self, state: AgentRunState, call: ToolCall) -> ToolResult:
        if not state.current_spec:
            raise ToolRejectedError("no spec; call challenge.spec.create first")
        spec = ChallengeSpec(
            **{
                k: state.current_spec[k]
                for k in ChallengeSpec.__dataclass_fields__
                if k in state.current_spec
            }
        )
        candidate = render_spec(spec, staging_root=self.staging_root)
        state.last_render_id = candidate.render_id
        state.last_validation = None
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            status="ok",
            summary=f"rendered to staging: {candidate.render_id}",
            data={"render_id": candidate.render_id},
        )

    def _tool_validate(self, state: AgentRunState, call: ToolCall) -> ToolResult:
        if not state.last_render_id:
            raise ToolRejectedError("no staged candidate; call challenge.render first")
        staging_root = self.staging_root or (
            Path(__file__).resolve().parents[3] / "challenges" / "staging"
        )
        candidate_dir = staging_root / state.last_render_id
        report = self.validator.validate(
            candidate_dir=candidate_dir,
            render_id=state.last_render_id,
            spec_digest=state.current_spec.get("vulnerability", ""),
        )
        state.last_validation = report.as_dict()
        ok = report.status == "passed"
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            status="ok" if ok else "miss",
            summary=f"validation {report.status}: exploit_ok={report.vulnerable_exploit_succeeded} checker_ok={report.checker_passed_before_patch}",
            data={"validation_status": report.status},
        )

    def _tool_inspect(self, state: AgentRunState, call: ToolCall) -> ToolResult:
        if not state.last_validation:
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                status="ok",
                summary="no validation result yet",
            )
        error = (state.last_validation.get("error") or "no error detail")[:2000]
        steps_failed = [
            s for s in state.last_validation.get("steps", []) if s.get("status") == "failed"
        ]
        summary = f"status={state.last_validation.get('status')} error={error} failed_steps={len(steps_failed)}"
        return ToolResult(
            call_id=call.call_id, tool_id=call.tool_id, status="ok", summary=summary[:2000]
        )

    def _tool_publish(self, state: AgentRunState, call: ToolCall) -> ToolResult:
        if not state.last_validation or state.last_validation.get("status") != "passed":
            raise ToolRejectedError("cannot publish: validation has not passed")
        staging_root = self.staging_root or (
            Path(__file__).resolve().parents[3] / "challenges" / "staging"
        )
        candidate_dir = staging_root / state.last_render_id
        try:
            challenge_id = self.registry.publish(
                candidate_dir=candidate_dir,
                validation_report=state.last_validation,
                agent_run_id=state.run_id,
            )
        except PublicationError as exc:
            raise ToolRejectedError(str(exc)) from exc
        state.published_challenge_id = challenge_id
        state.status = "published"
        state.touch()
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            status="ok",
            summary=f"published challenge_id={challenge_id}",
            data={"challenge_id": challenge_id},
        )

    def _tool_discard(self, state: AgentRunState, call: ToolCall) -> ToolResult:
        self._cleanup_staging(state)
        state.last_render_id = None
        state.last_validation = None
        state.touch()
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            status="ok",
            summary="staged candidate discarded",
        )

    def _cleanup_staging(self, state: AgentRunState) -> None:
        if not state.last_render_id:
            return
        import shutil

        staging_root = self.staging_root or (
            Path(__file__).resolve().parents[3] / "challenges" / "staging"
        )
        candidate_dir = staging_root / state.last_render_id
        if candidate_dir.exists():
            shutil.rmtree(str(candidate_dir), ignore_errors=True)

    def _telemetry(self, state: AgentRunState) -> AgentTelemetry:
        return AgentTelemetry(
            memory=self.memory,
            agent_id=state.agent_id,
            agent_type=AgentType.CHALLENGE_GENERATOR,
            run_id=state.run_id,
        )
