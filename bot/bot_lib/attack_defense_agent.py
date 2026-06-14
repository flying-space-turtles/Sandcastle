"""AI-014: AttackDefenseAgent — first-class team deployment with model decision loop.

Implements the full observe → plan → execute → persist cycle.
Attack actions target opponents; defensive patch uses the transactional workflow.
Deterministic fallback when model/budget fails.
No offensive action may target self. No defensive action may target another team.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent_contracts import (
    AgentType,
    ToolCall,
    ToolResult,
    canonical_json,
)
from .agent_memory import AgentMemoryStore, make_tool_result_entry
from .agent_telemetry import AgentTelemetry

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Offensive tool IDs (target_team argument required, must not be own team)
OFFENSIVE_TOOLS = frozenset(
    {
        "attack.recon",
        "attack.exploit",
        "attack.submit_flag",
    }
)

# Defensive tool IDs (no target_team; always scoped to own team)
DEFENSIVE_TOOLS = frozenset(
    {
        "defend.inspect_files",
        "defend.read_file",
        "defend.search_source",
        "defend.snapshot",
        "defend.apply_patch",
        "defend.rebuild",
        "defend.run_checker",
        "defend.run_exploit_regression",
        "defend.rollback",
    }
)

ALL_TOOLS = OFFENSIVE_TOOLS | DEFENSIVE_TOOLS

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "id": "attack.recon",
        "description": "Probe an opponent team's service and gather basic info.",
        "scope": "offensive",
        "parameters": {"target_team": {"type": "integer"}},
        "required": ["target_team"],
    },
    {
        "id": "attack.exploit",
        "description": "Run a registered exploit against an opponent's service and attempt to capture a flag.",
        "scope": "offensive",
        "parameters": {"target_team": {"type": "integer"}, "vuln_type": {"type": "string"}},
        "required": ["target_team"],
    },
    {
        "id": "attack.submit_flag",
        "description": "Submit a captured flag string to the gameserver.",
        "scope": "offensive",
        "parameters": {"flag": {"type": "string"}, "target_team": {"type": "integer"}},
        "required": ["flag", "target_team"],
    },
    {
        "id": "defend.inspect_files",
        "description": "List source files in own service that may be read or patched.",
        "scope": "defensive",
        "parameters": {},
    },
    {
        "id": "defend.read_file",
        "description": "Read a bounded range of a source file from own service.",
        "scope": "defensive",
        "parameters": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        "required": ["path"],
    },
    {
        "id": "defend.search_source",
        "description": "Search own service source for a literal or regex pattern.",
        "scope": "defensive",
        "parameters": {"pattern": {"type": "string"}, "literal": {"type": "boolean"}},
        "required": ["pattern"],
    },
    {
        "id": "defend.snapshot",
        "description": "Create a snapshot of the own service source for rollback.",
        "scope": "defensive",
        "parameters": {},
    },
    {
        "id": "defend.apply_patch",
        "description": "Apply a unified diff to the own service source (requires prior snapshot).",
        "scope": "defensive",
        "parameters": {"diff": {"type": "string"}, "correlation_id": {"type": "string"}},
        "required": ["diff"],
    },
    {
        "id": "defend.rebuild",
        "description": "Rebuild and restart the own service container.",
        "scope": "defensive",
        "parameters": {},
    },
    {
        "id": "defend.run_checker",
        "description": "Run the SLA checker against the own service.",
        "scope": "defensive",
        "parameters": {},
    },
    {
        "id": "defend.run_exploit_regression",
        "description": "Run registered reference exploits against own service; verifies the patch blocks them.",
        "scope": "defensive",
        "parameters": {},
    },
    {
        "id": "defend.rollback",
        "description": "Restore own service to the last snapshot (rolls back any pending patch).",
        "scope": "defensive",
        "parameters": {},
    },
]


# ---------------------------------------------------------------------------
# Agent run state
# ---------------------------------------------------------------------------


@dataclass
class AgentDecision:
    tool_id: str
    arguments: dict[str, Any]
    call_id: str
    result: ToolResult | None = None
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class AttackDefenseRunState:
    run_id: str
    agent_id: str
    team_id: int
    status: str = "running"  # running | stopped | budget_exhausted | error
    current_round: int = 0
    decisions: list[AgentDecision] = field(default_factory=list)
    flags_captured: int = 0
    flags_submitted: int = 0
    patch_attempts: int = 0
    patches_committed: int = 0
    rollback_count: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "team_id": self.team_id,
            "status": self.status,
            "current_round": self.current_round,
            "decisions_count": len(self.decisions),
            "flags_captured": self.flags_captured,
            "flags_submitted": self.flags_submitted,
            "patch_attempts": self.patch_attempts,
            "patches_committed": self.patches_committed,
            "rollback_count": self.rollback_count,
            "model_calls": self.model_calls,
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Executor interface (thin boundary between agent and infrastructure)
# ---------------------------------------------------------------------------


class ActionExecutor:
    """Executes validated typed actions on behalf of AttackDefenseAgent.

    Subclass this in production to wire to real infrastructure;
    use FixtureActionExecutor in tests.
    """

    def __init__(self, team_id: int, opponent_teams: list[int]) -> None:
        self.team_id = team_id
        self.opponent_teams = opponent_teams

    def recon(self, target_team: int) -> ToolResult:
        raise NotImplementedError

    def exploit(self, target_team: int, vuln_type: str | None) -> ToolResult:
        raise NotImplementedError

    def submit_flag(self, flag: str, target_team: int) -> ToolResult:
        raise NotImplementedError

    def inspect_files(self) -> ToolResult:
        raise NotImplementedError

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        raise NotImplementedError

    def search_source(self, pattern: str, literal: bool = True) -> ToolResult:
        raise NotImplementedError

    def snapshot(self) -> ToolResult:
        raise NotImplementedError

    def apply_patch(self, diff: str, correlation_id: str) -> ToolResult:
        raise NotImplementedError

    def rebuild(self) -> ToolResult:
        raise NotImplementedError

    def run_checker(self) -> ToolResult:
        raise NotImplementedError

    def run_exploit_regression(self) -> ToolResult:
        raise NotImplementedError

    def rollback(self) -> ToolResult:
        raise NotImplementedError


class FixtureActionExecutor(ActionExecutor):
    """Deterministic scripted executor for unit tests — no infrastructure needed."""

    def __init__(
        self,
        team_id: int,
        opponent_teams: list[int],
        *,
        flag_capture_value: str = "FLAG{aabbccddaabbccddaabbccddaabbccdd}",
        checker_passes: bool = True,
        exploit_blocked: bool = True,
        patch_commits: bool = True,
    ) -> None:
        super().__init__(team_id, opponent_teams)
        self._flag = flag_capture_value
        self._checker_passes = checker_passes
        self._exploit_blocked = exploit_blocked
        self._patch_commits = patch_commits
        self._call_count = 0

    def _call(self, tool_id: str) -> str:
        self._call_count += 1
        return f"fixture-{self._call_count}"

    def recon(self, target_team: int) -> ToolResult:
        cid = self._call("attack.recon")
        return ToolResult(
            call_id=cid,
            tool_id="attack.recon",
            status="ok",
            summary=f"recon of team {target_team}: service is up, port 8080",
            data={"target_team": target_team, "port": 8080},
        )

    def exploit(self, target_team: int, vuln_type: str | None) -> ToolResult:
        cid = self._call("attack.exploit")
        return ToolResult(
            call_id=cid,
            tool_id="attack.exploit",
            status="ok",
            summary=f"captured flag from team {target_team}",
            data={"target_team": target_team, "flag": self._flag},
        )

    def submit_flag(self, flag: str, target_team: int) -> ToolResult:
        cid = self._call("attack.submit_flag")
        return ToolResult(
            call_id=cid,
            tool_id="attack.submit_flag",
            status="ok",
            summary=f"flag submitted for team {target_team}: accepted",
            data={"target_team": target_team, "accepted": True},
        )

    def inspect_files(self) -> ToolResult:
        cid = self._call("defend.inspect_files")
        return ToolResult(
            call_id=cid,
            tool_id="defend.inspect_files",
            status="ok",
            summary="3 source files available",
            data={"files": ["app/app.py", "requirements.txt", "checker.py"]},
        )

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        cid = self._call("defend.read_file")
        return ToolResult(
            call_id=cid,
            tool_id="defend.read_file",
            status="ok",
            summary=f"read {path} lines {start_line}-{end_line or 'end'}",
            data={"path": path, "content": "# fixture content\n"},
        )

    def search_source(self, pattern: str, literal: bool = True) -> ToolResult:
        cid = self._call("defend.search_source")
        return ToolResult(
            call_id=cid,
            tool_id="defend.search_source",
            status="ok",
            summary=f"found 1 match for {pattern!r}",
            data={"matches": [{"file": "app/app.py", "line": 42, "content": f"# {pattern}"}]},
        )

    def snapshot(self) -> ToolResult:
        cid = self._call("defend.snapshot")
        return ToolResult(
            call_id=cid,
            tool_id="defend.snapshot",
            status="ok",
            summary="snapshot created",
            data={"snapshot_id": "fixture-snap-001"},
        )

    def apply_patch(self, diff: str, correlation_id: str) -> ToolResult:
        cid = self._call("defend.apply_patch")
        if not self._patch_commits:
            return ToolResult(
                call_id=cid,
                tool_id="defend.apply_patch",
                status="error",
                summary="fixture: patch failed",
                data={},
            )
        return ToolResult(
            call_id=cid,
            tool_id="defend.apply_patch",
            status="ok",
            summary="patch committed: checker passed, exploit blocked",
            data={
                "status": "committed",
                "exploit_blocked": self._exploit_blocked,
                "checker_passed_after": self._checker_passes,
            },
        )

    def rebuild(self) -> ToolResult:
        cid = self._call("defend.rebuild")
        return ToolResult(
            call_id=cid,
            tool_id="defend.rebuild",
            status="ok",
            summary="rebuild successful",
            data={},
        )

    def run_checker(self) -> ToolResult:
        cid = self._call("defend.run_checker")
        status = "ok" if self._checker_passes else "error"
        return ToolResult(
            call_id=cid,
            tool_id="defend.run_checker",
            status=status,
            summary="checker passed" if self._checker_passes else "checker failed",
            data={"passed": self._checker_passes},
        )

    def run_exploit_regression(self) -> ToolResult:
        cid = self._call("defend.run_exploit_regression")
        return ToolResult(
            call_id=cid,
            tool_id="defend.run_exploit_regression",
            status="ok" if self._exploit_blocked else "miss",
            summary="exploit blocked" if self._exploit_blocked else "exploit still succeeds",
            data={"exploit_blocked": self._exploit_blocked},
        )

    def rollback(self) -> ToolResult:
        cid = self._call("defend.rollback")
        return ToolResult(
            call_id=cid,
            tool_id="defend.rollback",
            status="ok",
            summary="rolled back to last snapshot",
            data={},
        )


# ---------------------------------------------------------------------------
# Fallback policy
# ---------------------------------------------------------------------------


def _deterministic_fallback(
    state: AttackDefenseRunState,
    opponent_teams: list[int],
) -> ToolCall | None:
    """Return a deterministic fallback action when model is unavailable.

    Priority: service recovery (checker fail) → known vulns → recon.
    """
    if not opponent_teams:
        return None
    target = opponent_teams[state.current_round % len(opponent_teams)]
    call_id = f"fallback-{state.current_round}"
    # Always try recon as a safe fallback
    return ToolCall(call_id=call_id, tool_id="attack.recon", arguments={"target_team": target})


# ---------------------------------------------------------------------------
# AttackDefenseAgent
# ---------------------------------------------------------------------------


class ToolRejectedError(ValueError):
    """Raised when a tool call is rejected for policy or safety reasons."""


class AttackDefenseAgent:
    """Autonomous attack/defense agent for one team.

    The model proposes typed actions; the agent validates and executes them.
    Captured flags go through submit_flag; patches go through the transactional
    workflow enforced by the executor.

    No provider key is ever stored in this class.
    """

    def __init__(
        self,
        team_id: int,
        opponent_teams: list[int],
        memory: AgentMemoryStore,
        executor: ActionExecutor,
        *,
        max_actions_per_round: int = 3,
        max_model_calls: int = 30,
    ) -> None:
        if team_id in opponent_teams:
            raise ValueError("team_id must not appear in opponent_teams (self-attack)")
        self.team_id = team_id
        self.opponent_teams = list(opponent_teams)
        self.memory = memory
        self.executor = executor
        self.max_actions_per_round = max_actions_per_round
        self.max_model_calls = max_model_calls

    def start(self) -> AttackDefenseRunState:
        run_id = uuid.uuid4().hex[:12]
        agent_id = f"team{self.team_id}-attack-defense"
        state = AttackDefenseRunState(
            run_id=run_id,
            agent_id=agent_id,
            team_id=self.team_id,
        )
        telem = self._telemetry(state)
        telem.run_started(team_id=self.team_id, provider="pending", model_id="")
        return state

    def stop(self, state: AttackDefenseRunState) -> None:
        state.status = "stopped"
        state.touch()

    def execute_tool(self, state: AttackDefenseRunState, tool_call: ToolCall) -> ToolResult:
        """Validate and execute a model-proposed tool call."""
        self._validate_tool_call(state, tool_call)
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

        # Update counters
        if tool_call.tool_id == "attack.submit_flag" and result.status == "ok":
            state.flags_submitted += 1
        if tool_call.tool_id == "attack.exploit" and result.status == "ok":
            state.flags_captured += 1
        if tool_call.tool_id == "defend.apply_patch":
            state.patch_attempts += 1
            if result.status == "ok" and result.data.get("status") == "committed":
                state.patches_committed += 1
        if tool_call.tool_id == "defend.rollback":
            state.rollback_count += 1

        decisions_item = AgentDecision(
            tool_id=tool_call.tool_id,
            arguments=tool_call.arguments,
            call_id=tool_call.call_id,
            result=result,
        )
        state.decisions.append(decisions_item)

        # Persist to memory (redacted)
        entry = make_tool_result_entry(
            agent_id=state.agent_id,
            agent_type=AgentType.ATTACK_DEFENSE,
            run_id=state.run_id,
            tool_id=tool_call.tool_id,
            call_id=tool_call.call_id,
            status=result.status,
            summary=result.summary,
            data=dict(result.data),
        )
        self.memory.append(entry)
        return result

    def build_observation(
        self,
        state: AttackDefenseRunState,
        match_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a compact, bounded observation for the next model call."""
        recent = self.memory.recent_as_dicts(state.run_id, limit=8)
        obs: dict[str, Any] = {
            "my_team": self.team_id,
            "opponent_teams": self.opponent_teams,
            "current_round": state.current_round,
            "num_teams": len(self.opponent_teams) + 1,
            "flags_captured": state.flags_captured,
            "flags_submitted": state.flags_submitted,
            "patch_attempts": state.patch_attempts,
            "patches_committed": state.patches_committed,
            "rollback_count": state.rollback_count,
            "model_calls_remaining": self.max_model_calls - state.model_calls,
            "prior_results": recent,
        }
        if match_context:
            obs.update({k: v for k, v in match_context.items() if k not in obs})
        # Hard cap
        if len(canonical_json(obs)) > 10000:
            obs["prior_results"] = recent[:3]
        return obs

    def fallback_action(self, state: AttackDefenseRunState) -> ToolCall | None:
        """Return a deterministic fallback action when model/budget unavailable."""
        return _deterministic_fallback(state, self.opponent_teams)

    # ------------------------------------------------------------------
    # Validation

    def _validate_tool_call(self, state: AttackDefenseRunState, call: ToolCall) -> None:
        if call.tool_id not in ALL_TOOLS:
            raise ToolRejectedError(f"unknown tool: {call.tool_id!r}")
        if call.tool_id in OFFENSIVE_TOOLS:
            target = call.arguments.get("target_team")
            if not isinstance(target, int):
                raise ToolRejectedError("offensive action requires integer target_team")
            if target == self.team_id:
                raise ToolRejectedError(
                    f"offensive action cannot target own team (team {self.team_id})"
                )
            if target not in self.opponent_teams:
                raise ToolRejectedError(f"target_team {target} is not an allowed opponent")

    # ------------------------------------------------------------------
    # Dispatch

    def _dispatch(self, state: AttackDefenseRunState, call: ToolCall) -> ToolResult:
        t = call.tool_id
        a = call.arguments
        if t == "attack.recon":
            return self.executor.recon(int(a["target_team"]))
        if t == "attack.exploit":
            return self.executor.exploit(int(a["target_team"]), a.get("vuln_type"))
        if t == "attack.submit_flag":
            flag = str(a.get("flag", ""))
            if not flag:
                raise ToolRejectedError("submit_flag requires a non-empty flag argument")
            return self.executor.submit_flag(flag, int(a["target_team"]))
        if t == "defend.inspect_files":
            return self.executor.inspect_files()
        if t == "defend.read_file":
            return self.executor.read_file(
                str(a["path"]),
                int(a.get("start_line", 1)),
                int(a["end_line"]) if "end_line" in a else None,
            )
        if t == "defend.search_source":
            return self.executor.search_source(str(a["pattern"]), bool(a.get("literal", True)))
        if t == "defend.snapshot":
            return self.executor.snapshot()
        if t == "defend.apply_patch":
            diff = str(a.get("diff", ""))
            if not diff:
                raise ToolRejectedError("apply_patch requires a non-empty diff argument")
            correlation_id = str(a.get("correlation_id", call.call_id))
            return self.executor.apply_patch(diff, correlation_id)
        if t == "defend.rebuild":
            return self.executor.rebuild()
        if t == "defend.run_checker":
            return self.executor.run_checker()
        if t == "defend.run_exploit_regression":
            return self.executor.run_exploit_regression()
        if t == "defend.rollback":
            return self.executor.rollback()
        raise ToolRejectedError(f"unhandled tool: {t!r}")

    def _telemetry(self, state: AttackDefenseRunState) -> AgentTelemetry:
        return AgentTelemetry(
            memory=self.memory,
            agent_id=state.agent_id,
            agent_type=AgentType.ATTACK_DEFENSE,
            run_id=state.run_id,
        )
