"""Normalized agent telemetry event emitters for Sandcastle AI agents.

Emits structured ``agent.*`` events correlated with match, round, team,
agent ID, run ID, and correlation ID where applicable.

Sensitive values (flags, API keys, planning tokens, raw model reasoning,
and full source files) are redacted before any event is emitted or stored.

Events are written to the agent memory store so they survive controller
restarts and are visible in the ``GET /agent-runs/<id>/memory`` endpoint.
The gameserver telemetry endpoint integration is intentionally deferred to
avoid a hard dependency on a running gameserver during unit tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .agent_contracts import AgentType
from .agent_memory import AgentMemoryStore, redact

if TYPE_CHECKING:
    pass

# ── Event type constants ─────────────────────────────────────────────────────

AGENT_RUN_STARTED = "agent.run_started"
AGENT_RUN_STOPPED = "agent.run_stopped"
AGENT_PLAN_REQUESTED = "agent.plan_requested"
AGENT_PLAN_COMPLETED = "agent.plan_completed"
AGENT_PLAN_FAILED = "agent.plan_failed"
AGENT_TOOL_RESULT = "agent.tool_result"
AGENT_MEMORY_PRUNED = "agent.memory_pruned"
AGENT_BUDGET_EXHAUSTED = "agent.budget_exhausted"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentTelemetry:
    """Emit normalized agent lifecycle events into the memory store.

    All events are stored as ``kind='telemetry'`` memory entries so they
    appear in the agent's memory timeline alongside tool calls and results.
    """

    def __init__(
        self,
        memory: AgentMemoryStore,
        agent_id: str,
        agent_type: AgentType,
        run_id: str,
    ) -> None:
        self.memory = memory
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.run_id = run_id

    def _emit(
        self,
        event_type: str,
        summary: str,
        data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        from .agent_contracts import AgentMemoryEntry

        payload: dict[str, Any] = {
            "event_type": event_type,
            "agent_id": self.agent_id,
            "agent_type": str(self.agent_type),
            "run_id": self.run_id,
        }
        if correlation_id:
            payload["correlation_id"] = correlation_id
        if data:
            payload.update(redact(data))  # type: ignore[arg-type]

        entry = AgentMemoryEntry(
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            run_id=self.run_id,
            kind="telemetry",
            summary=summary[:2000],
            data=payload,
            created_at=_now_iso(),
        )
        try:
            self.memory.append(entry)
        except Exception:  # noqa: BLE001 — telemetry must not crash the agent
            pass

    def run_started(
        self,
        *,
        team_id: int | None = None,
        provider: str = "fake",
        model_id: str = "",
    ) -> None:
        self._emit(
            AGENT_RUN_STARTED,
            f"Agent run started: {self.agent_id} ({self.agent_type})",
            {
                "team_id": team_id,
                "provider": provider,
                "model_id": model_id,
            },
        )

    def run_stopped(
        self,
        *,
        reason: str = "stopped",
        final_status: str = "STOPPED",
    ) -> None:
        self._emit(
            AGENT_RUN_STOPPED,
            f"Agent run stopped: {self.agent_id} reason={reason}",
            {
                "reason": reason,
                "final_status": final_status,
            },
        )

    def plan_requested(
        self,
        *,
        round_number: int | None = None,
        correlation_id: str | None = None,
        tool_count: int = 0,
    ) -> None:
        self._emit(
            AGENT_PLAN_REQUESTED,
            f"Planning requested round={round_number} tools={tool_count}",
            {
                "round_number": round_number,
                "available_tools": tool_count,
            },
            correlation_id=correlation_id,
        )

    def plan_completed(
        self,
        *,
        round_number: int | None = None,
        correlation_id: str | None = None,
        model_id: str = "",
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
        tasks_accepted: int = 0,
        tasks_proposed: int = 0,
        plan_seconds: float = 0.0,
        used_fallback: bool = False,
    ) -> None:
        self._emit(
            AGENT_PLAN_COMPLETED,
            (
                f"Plan completed round={round_number} "
                f"tasks={tasks_accepted}/{tasks_proposed} "
                f"tokens={input_tokens}+{output_tokens}"
            ),
            {
                "round_number": round_number,
                "model_id": model_id,
                "provider": provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "tasks_accepted": tasks_accepted,
                "tasks_proposed": tasks_proposed,
                "plan_seconds": round(plan_seconds, 3),
                "used_fallback": used_fallback,
            },
            correlation_id=correlation_id,
        )

    def plan_failed(
        self,
        *,
        round_number: int | None = None,
        correlation_id: str | None = None,
        error: str = "",
        error_type: str = "",
    ) -> None:
        self._emit(
            AGENT_PLAN_FAILED,
            f"Plan failed round={round_number}: {error[:200]}",
            {
                "round_number": round_number,
                "error": error[:500],
                "error_type": error_type,
            },
            correlation_id=correlation_id,
        )

    def tool_result(
        self,
        *,
        tool_id: str,
        call_id: str,
        status: str,
        summary: str,
        round_number: int | None = None,
        correlation_id: str | None = None,
        target_team: int | None = None,
    ) -> None:
        self._emit(
            AGENT_TOOL_RESULT,
            f"Tool {tool_id} → {status}: {summary[:200]}",
            {
                "tool_id": tool_id,
                "call_id": call_id,
                "status": status,
                "round_number": round_number,
                "target_team": target_team,
                "result_summary": summary[:500],
            },
            correlation_id=correlation_id,
        )

    def memory_pruned(self, *, deleted_count: int) -> None:
        self._emit(
            AGENT_MEMORY_PRUNED,
            f"Memory pruned: {deleted_count} entries removed",
            {"deleted_count": deleted_count},
        )

    def budget_exhausted(
        self,
        *,
        scope: str,
        limit: float,
        current: float,
        correlation_id: str | None = None,
    ) -> None:
        self._emit(
            AGENT_BUDGET_EXHAUSTED,
            f"Budget exhausted: {scope} {current}/{limit}",
            {
                "scope": scope,
                "limit": limit,
                "current": current,
            },
            correlation_id=correlation_id,
        )
