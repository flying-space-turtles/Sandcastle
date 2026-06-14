#!/usr/bin/env python3
"""Local deployment controller for Sandcastle team bots."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot_lib import ARENA_DEFAULTS, action_catalog, planner_catalog
from bot_lib.agent_contracts import AgentType, BudgetPolicy, ModelProvider
from bot_lib.agent_memory import AgentMemoryStore
from bot_lib.agent_planning import (
    AgentPlanningService,
    DeterministicPlanningFakeProvider,
    PlanningCredentialStore,
    PlanningIdentity,
    PlanningRequestError,
)
from bot_lib.challenge_run_store import ChallengeRunStore
from bot_lib.model_budget import BudgetedModelGateway, ModelBudgetExceeded, ModelBudgetLedger
from bot_lib.model_gateway import ModelGateway, ModelGatewayError, ModelProviderAdapter
from bot_lib.gemini_provider import GeminiProvider
from bot_lib.openai_provider import OpenAIProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO_ROOT / "bot" / "deploy.sh"
ARENA_CONFIG = Path(os.environ.get("ARENA_CONFIG_FILE", REPO_ROOT / "config" / "arena.env"))
DATABASE = Path(
    os.environ.get("BOT_CONTROLLER_DB", REPO_ROOT / ".sandcastle" / "bot-controller.db")
)
ALLOWED_ORIGINS = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
ACTIVE_STATUSES = ("DEPLOYING", "RUNNING")
MAX_PLAN_REQUEST_BYTES = 256_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], timeout: int = 30, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
            env=env,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, f"command timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - surfaced as an operator error
        return -1, str(exc)


def _arena_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ARENA_CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _team_token(team_id: int) -> str:
    return _arena_values()["ARENA_TEAM_TOKEN_PATTERN"].replace("{team}", str(team_id))


def _gameserver_url() -> str:
    return f"http://{ARENA_DEFAULTS.network_prefix}.0.2:8000"


def _planning_url() -> str:
    return f"http://{ARENA_DEFAULTS.network_prefix}.0.4:{ARENA_DEFAULTS.bot_api_port}"


def _container_name(team_id: int) -> str:
    return f"team{team_id}-ssh"


def _deployment_dir(deployment_id: str) -> str:
    return f"/tmp/sandcastle-bot/deployments/{deployment_id}"


def _docker_state(team_id: int) -> dict[str, Any] | None:
    rc, output = _run(
        ["docker", "inspect", "--format", "{{json .State}}", _container_name(team_id)]
    )
    if rc != 0:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _container_file(team_id: int, path: str, lines: int | None = None) -> str:
    command = ["docker", "exec", _container_name(team_id)]
    if lines is None:
        command += ["cat", path]
    else:
        command += ["tail", f"-n{lines}", path]
    rc, output = _run(command)
    return output if rc == 0 else ""


def _runtime_status(team_id: int, deployment_id: str) -> tuple[bool, int | None]:
    state = _docker_state(team_id)
    if not state or not state.get("Running"):
        return False, None
    rc, output = _run(
        [
            "docker",
            "exec",
            _container_name(team_id),
            "pgrep",
            "-a",
            "-f",
            f"{_deployment_dir(deployment_id)}/bot.py",
        ]
    )
    if rc != 0 or not output:
        return False, None
    lines = [line for line in output.splitlines() if "python3" in line]
    if not lines:
        return False, None
    first = lines[0].split()[0]
    return True, int(first) if first.isdigit() else None


_RESTART_LOOP_THRESHOLD = 3
_DISK_PRESSURE_THRESHOLD_PCT = 80


def _parse_df(output: str) -> tuple[int | None, int | None]:
    """Parse `df -k` output. Returns (used_pct, available_bytes) or (None, None)."""
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Filesystem"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            return int(parts[4].rstrip("%")), int(parts[3]) * 1024
        except (ValueError, IndexError):
            continue
    return None, None


def _resource_status() -> dict[str, Any]:
    """Inspect all team containers and return resource health and violations."""
    num_teams = ARENA_DEFAULTS.team_count
    names = [
        name
        for i in range(1, num_teams + 1)
        for name in (f"team{i}-vuln", f"team{i}-ssh", f"team{i}-vuln-app")
    ]
    containers: list[dict[str, Any]] = []
    for name in names:
        rc, out = _run(["docker", "inspect", name])
        if rc != 0:
            continue
        try:
            data_list = json.loads(out)
            if not data_list:
                continue
            d = data_list[0]
            state = d.get("State", {})
            mem = (d.get("HostConfig") or {}).get("Memory", 0)
            running = bool(state.get("Running"))
            entry: dict[str, Any] = {
                "name": name,
                "running": running,
                "oom_killed": bool(state.get("OOMKilled")),
                "restart_count": int(d.get("RestartCount", 0)),
                "exit_code": state.get("ExitCode"),
                "mem_limit_bytes": int(mem) if mem else None,
                "disk_used_pct": None,
                "disk_available_bytes": None,
            }
            if running:
                df_rc, df_out = _run(["docker", "exec", name, "df", "-k", "/"])
                if df_rc == 0:
                    used_pct, avail = _parse_df(df_out)
                    entry["disk_used_pct"] = used_pct
                    entry["disk_available_bytes"] = avail
            containers.append(entry)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    flagged: list[dict[str, Any]] = []
    for c in containers:
        if c["oom_killed"]:
            flagged.append(
                {
                    "container": c["name"],
                    "type": "resource.oom_kill",
                    "restart_count": c["restart_count"],
                    "mem_limit_bytes": c["mem_limit_bytes"],
                }
            )
        elif c["restart_count"] >= _RESTART_LOOP_THRESHOLD:
            flagged.append(
                {
                    "container": c["name"],
                    "type": "resource.restart_loop",
                    "restart_count": c["restart_count"],
                }
            )
        if c["disk_used_pct"] is not None and c["disk_used_pct"] >= _DISK_PRESSURE_THRESHOLD_PCT:
            flagged.append(
                {
                    "container": c["name"],
                    "type": "resource.disk_pressure",
                    "disk_used_pct": c["disk_used_pct"],
                    "disk_available_bytes": c["disk_available_bytes"],
                }
            )
    return {"containers": containers, "violations": flagged}


def _parse_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


class DeploymentStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    team_id INTEGER NOT NULL,
                    bot_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    stopped_at TEXT,
                    pid INTEGER,
                    error TEXT,
                    archived_log TEXT NOT NULL DEFAULT '',
                    archived_events TEXT NOT NULL DEFAULT '',
                    agent_type TEXT NOT NULL DEFAULT 'scripted',
                    agent_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'scripted',
                    model_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS deployments_team_created "
                "ON deployments(team_id, created_at DESC)"
            )
            # Migrate: add identity columns if missing (safe on existing DBs)
            # NOTE: the agent_type index is created inside _migrate after the column exists.
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add AI-006 identity columns to existing databases without data loss."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(deployments)").fetchall()}
        migrations = [
            ("agent_type", "TEXT NOT NULL DEFAULT 'scripted'"),
            ("agent_id", "TEXT NOT NULL DEFAULT ''"),
            ("run_id", "TEXT NOT NULL DEFAULT ''"),
            ("provider", "TEXT NOT NULL DEFAULT 'scripted'"),
            ("model_id", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_def in migrations:
            if col_name not in columns:
                conn.execute(f"ALTER TABLE deployments ADD COLUMN {col_name} {col_def}")
        # Back-fill agent_id and run_id from id for legacy rows
        conn.execute(
            """
            UPDATE deployments SET agent_id = id, run_id = id
            WHERE agent_id = '' OR agent_id IS NULL
            """
        )
        # Create the agent_type index now that the column is guaranteed to exist
        conn.execute(
            "CREATE INDEX IF NOT EXISTS deployments_agent_type ON deployments(agent_type, status)"
        )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def insert(
        self,
        deployment_id: str,
        team_id: int,
        config: dict[str, Any],
        *,
        agent_type: str = "scripted",
        agent_id: str = "",
        run_id: str = "",
        provider: str = "scripted",
        model_id: str = "",
    ) -> None:
        timestamp = _now()
        # Default agent_id and run_id to deployment_id for backward compat
        effective_agent_id = agent_id or deployment_id
        effective_run_id = run_id or deployment_id
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deployments (
                    id, team_id, bot_name, status, config_json, created_at, updated_at,
                    agent_type, agent_id, run_id, provider, model_id
                ) VALUES (?, ?, ?, 'DEPLOYING', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deployment_id,
                    team_id,
                    str(config.get("bot_name", "Bot")),
                    json.dumps(config, sort_keys=True),
                    timestamp,
                    timestamp,
                    agent_type,
                    effective_agent_id,
                    effective_run_id,
                    provider,
                    model_id,
                ),
            )

    def update(self, deployment_id: str, **values: object) -> None:
        if not values:
            return
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE deployments SET {assignments} WHERE id = ?",
                (*values.values(), deployment_id),
            )

    def get(self, deployment_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()

    def list(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM deployments ORDER BY created_at DESC").fetchall()

    def list_by_agent_type(self, agent_type: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM deployments WHERE agent_type = ? ORDER BY created_at DESC",
                (agent_type,),
            ).fetchall()

    def active_for_team(self, team_id: int) -> list[sqlite3.Row]:
        """Return active deployments for a team (any agent type)."""
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM deployments
                WHERE team_id = ? AND status IN ('DEPLOYING', 'RUNNING')
                ORDER BY created_at DESC
                """,
                (team_id,),
            ).fetchall()

    def active_by_scope(
        self,
        agent_type: str,
        team_id: int,
    ) -> list[sqlite3.Row]:
        """Return active deployments matching the given agent_type and team scope.

        challenge_generator uses team_id=0 (organizer scope) and is unique globally.
        attack_defense is unique per team.
        scripted bots are unique per team.
        """
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM deployments
                WHERE agent_type = ? AND team_id = ? AND status IN ('DEPLOYING', 'RUNNING')
                ORDER BY created_at DESC
                """,
                (agent_type, team_id),
            ).fetchall()


class _UnavailableProvider:
    charges_budget = False

    def __init__(self, provider: ModelProvider, message: str) -> None:
        self.provider = provider
        self.message = message

    def complete(self, request, timeout):
        del request, timeout
        raise ModelGatewayError(self.message)


def _build_planning_service(memory: "AgentMemoryStore | None" = None) -> AgentPlanningService:
    primary = ModelProvider(ARENA_DEFAULTS.agent_provider)
    fallback = ModelProvider(ARENA_DEFAULTS.agent_fallback_provider)
    adapters: dict[ModelProvider, ModelProviderAdapter] = {
        ModelProvider.FAKE: DeterministicPlanningFakeProvider()
    }
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        adapters[ModelProvider.OPENAI] = OpenAIProvider(
            api_key=openai_key,
            model_id=ARENA_DEFAULTS.agent_model or "gpt-5.4-mini",
            input_cost_per_million=ARENA_DEFAULTS.openai_input_cost_per_million,
            output_cost_per_million=ARENA_DEFAULTS.openai_output_cost_per_million,
        )
    else:
        adapters[ModelProvider.OPENAI] = _UnavailableProvider(
            ModelProvider.OPENAI,
            "OpenAI provider is selected but OPENAI_API_KEY is not configured",
        )
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        adapters[ModelProvider.GEMINI] = GeminiProvider(
            api_key=gemini_key,
            model_id=ARENA_DEFAULTS.agent_model or "gemini-2.0-flash-lite",
            input_cost_per_million=0.075,
            output_cost_per_million=0.30,
        )
    else:
        adapters[ModelProvider.GEMINI] = _UnavailableProvider(
            ModelProvider.GEMINI,
            "Gemini provider is selected but GEMINI_API_KEY is not configured",
        )
    adapters[ModelProvider.OLLAMA] = _UnavailableProvider(
        ModelProvider.OLLAMA,
        "Ollama provider is not implemented",
    )

    policy = BudgetPolicy(
        max_actions_per_round=ARENA_DEFAULTS.agent_max_calls_per_round,
        max_calls_per_round=ARENA_DEFAULTS.agent_max_calls_per_round,
        max_calls_per_match=ARENA_DEFAULTS.agent_max_calls_per_match,
        max_input_chars=ARENA_DEFAULTS.agent_max_input_chars,
        max_output_tokens=ARENA_DEFAULTS.agent_max_output_tokens,
        max_cost_usd_per_call=ARENA_DEFAULTS.agent_max_cost_usd_per_call,
        max_cost_usd_per_match=ARENA_DEFAULTS.agent_max_cost_usd_per_match,
        max_cost_usd_per_day=ARENA_DEFAULTS.agent_max_cost_usd_per_day,
        timeout_seconds=ARENA_DEFAULTS.agent_timeout_seconds,
        max_retries=ARENA_DEFAULTS.agent_max_retries,
    )
    gateway = ModelGateway(
        adapters,
        primary_provider=primary,
        fallback_provider=fallback,
        max_retries=policy.max_retries,
    )
    return AgentPlanningService(
        BudgetedModelGateway(gateway, BUDGET_LEDGER),
        num_teams=ARENA_DEFAULTS.team_count,
        model_id=(ARENA_DEFAULTS.agent_model if primary is not ModelProvider.FAKE else "fake-v1"),
        budget=policy,
        memory=memory,
    )


STORE = DeploymentStore(DATABASE)
BUDGET_LEDGER = ModelBudgetLedger(DATABASE)
PLAN_CREDENTIALS = PlanningCredentialStore(DATABASE)
AGENT_MEMORY = AgentMemoryStore(DATABASE)
CHALLENGE_STORE = ChallengeRunStore(DATABASE)
PLANNING_SERVICE = _build_planning_service(memory=AGENT_MEMORY)

# ---------------------------------------------------------------------------
# Challenge options (driven by agent_contracts constants)
# ---------------------------------------------------------------------------

_CHALLENGE_OPTIONS: dict[str, Any] = {
    "vulnerabilities": [
        {
            "id": "path_traversal",
            "label": "Path Traversal",
            "icon": "🗂️",
            "description": "Reads arbitrary files via /export?file= — classic directory traversal.",
        },
        {
            "id": "sql_injection",
            "label": "SQL Injection",
            "icon": "🗄️",
            "description": "Bypass login via SQLi and access all stored notes.",
        },
        {
            "id": "command_injection",
            "label": "Command Injection",
            "icon": "💻",
            "description": "Execute arbitrary OS commands through the diagnostics endpoint.",
        },
    ],
    "difficulties": [
        {
            "id": "easy",
            "label": "Easy",
            "description": "Vulnerability is obvious; minimal obfuscation.",
        },
        {
            "id": "medium",
            "label": "Medium",
            "description": "Requires inspection of multiple endpoints; some misdirection.",
        },
    ],
    "decoy_range": {"min": 0, "max": 3},
    "templates": ["flask-notes-v1"],
}


# ---------------------------------------------------------------------------
# Background challenge generation
# ---------------------------------------------------------------------------


def _generate_challenge_bg(
    run_id: str,
    vulnerability: str,
    difficulty: str,
    seed: int,
    decoy_endpoints: int,
    max_attempts: int,
) -> None:
    """Run ChallengeGeneratorAgent in a background thread.

    Updates CHALLENGE_STORE with status=published or status=failed on completion.
    All thinking steps are persisted to AGENT_MEMORY (same run_id) for the
    /challenges/<id>/log endpoint.
    """
    try:
        import sys as _sys

        _sys.path.insert(0, str(REPO_ROOT / "bot"))
        from challenge.agent import ChallengeGeneratorAgent
        from challenge.registry import ChallengeRegistry
        from challenge.validator import ChallengeValidator
        from bot_lib.agent_contracts import ToolCall

        staging = REPO_ROOT / "challenges" / "staging"
        published_root = REPO_ROOT / "challenges" / "published"
        staging.mkdir(parents=True, exist_ok=True)
        published_root.mkdir(parents=True, exist_ok=True)

        validator = ChallengeValidator(docker=False)
        registry = ChallengeRegistry(published_root)
        mem = AGENT_MEMORY  # shared store — run_id is the key

        agent = ChallengeGeneratorAgent(
            memory=mem,
            staging_root=staging,
            validator=validator,
            registry=registry,
            max_attempts=max_attempts,
        )

        spec_dict = {
            "vulnerability": vulnerability,
            "difficulty": difficulty,
            "seed": seed,
            "decoy_endpoints": decoy_endpoints,
            "max_attempts": max_attempts,
        }
        state = agent.start({**spec_dict, "run_id": run_id})

        # Step through the tool loop (spec.create → render → validate → publish)
        tools_sequence = [
            ToolCall(
                f"{run_id}-c1",
                "challenge.spec.create",
                {
                    "vulnerability": vulnerability,
                    "difficulty": difficulty,
                    "seed": seed,
                    "decoy_endpoints": decoy_endpoints,
                },
            ),
            ToolCall(f"{run_id}-c2", "challenge.render", {}),
            ToolCall(f"{run_id}-c3", "challenge.validate", {}),
            ToolCall(f"{run_id}-c4", "challenge.publish", {}),
        ]
        for tool in tools_sequence:
            if state.status != "running":
                break
            agent.execute_tool(state, tool)

        challenge_id = state.published_challenge_id
        if challenge_id:
            import json as _json

            CHALLENGE_STORE.update(
                run_id,
                status="published",
                challenge_id=challenge_id,
                spec_json=_json.dumps(spec_dict),
            )
        else:
            error = state.error or "generation did not complete"
            CHALLENGE_STORE.update(run_id, status="failed", error=error)

    except Exception as exc:  # noqa: BLE001
        CHALLENGE_STORE.update(run_id, status="failed", error=str(exc))


def _deploy_challenge_to_arena(challenge_id: str) -> tuple[bool, str]:
    """Inject a published challenge into all team containers.

    Runs:
      setup.sh --service-template challenges/published/<id> --overwrite-services
      arena.sh up

    Returns (ok, output).
    """
    challenge_path = REPO_ROOT / "challenges" / "published" / challenge_id
    if not challenge_path.is_dir():
        return False, f"challenge directory not found: {challenge_path}"

    setup_sh = REPO_ROOT / "scripts" / "setup.sh"
    arena_sh = REPO_ROOT / "scripts" / "arena.sh"

    rc1, out1 = _run(
        ["bash", str(setup_sh), "--service-template", str(challenge_path), "--overwrite-services"],
        timeout=120,
    )
    if rc1 != 0:
        return False, f"setup.sh failed:\n{out1}"

    rc2, out2 = _run(["bash", str(arena_sh), "up"], timeout=180)
    combined = f"setup.sh:\n{out1}\n\narena.sh up:\n{out2}"
    return rc2 == 0, combined


def _deployment_events(row: sqlite3.Row) -> list[dict[str, Any]]:
    raw = str(row["archived_events"] or "")
    if row["status"] in ACTIVE_STATUSES:
        raw = (
            _container_file(int(row["team_id"]), f"{_deployment_dir(row['id'])}/events.jsonl")
            or raw
        )
    return _parse_events(raw)


def _deployment_logs(row: sqlite3.Row, lines: int = 300) -> list[str]:
    raw = str(row["archived_log"] or "")
    if row["status"] in ACTIVE_STATUSES:
        raw = (
            _container_file(int(row["team_id"]), f"{_deployment_dir(row['id'])}/bot.log", lines)
            or raw
        )
    return raw.splitlines()[-lines:]


def _planning_identity(token: str) -> PlanningIdentity | None:
    credential = PLAN_CREDENTIALS.validate(token)
    if credential is None:
        return None
    row = STORE.get(credential.deployment_id)
    if row is None or row["status"] not in ACTIVE_STATUSES:
        return None
    config = json.loads(row["config_json"])
    configured_actions = frozenset(str(item) for item in config.get("actions", []))
    known_actions = frozenset(str(item["id"]) for item in action_catalog())
    allowed_actions = configured_actions & known_actions
    if config.get("target_policy") == "selected":
        candidates = {int(team) for team in config.get("target_teams", [])}
    else:
        candidates = set(range(1, ARENA_DEFAULTS.team_count + 1))
    candidates.discard(credential.team_id)
    # Resolve stable agent identity from the deployment record (AI-006)
    agent_id = str(row["agent_id"] or credential.deployment_id)
    run_id = str(row["run_id"] or credential.deployment_id)
    raw_agent_type = str(row["agent_type"] or "attack_defense")
    try:
        agent_type = AgentType(raw_agent_type)
    except ValueError:
        agent_type = AgentType.ATTACK_DEFENSE
    return PlanningIdentity(
        deployment_id=credential.deployment_id,
        team_id=credential.team_id,
        allowed_targets=frozenset(candidates),
        allowed_actions=allowed_actions,
        agent_id=agent_id,
        run_id=run_id,
        agent_type=agent_type,
    )


def _archive(row: sqlite3.Row) -> None:
    events = _container_file(int(row["team_id"]), f"{_deployment_dir(row['id'])}/events.jsonl")
    log = _container_file(int(row["team_id"]), f"{_deployment_dir(row['id'])}/bot.log")
    STORE.update(
        str(row["id"]),
        archived_events=events or str(row["archived_events"] or ""),
        archived_log=log or str(row["archived_log"] or ""),
    )


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    captures = sum(event.get("type") == "flag.captured" for event in events)
    submissions = [event for event in events if event.get("type") == "submission.completed"]
    accepted = sum(bool(event.get("accepted")) for event in submissions)
    failures = sum(event.get("type") in {"round.failed", "deployment.failed"} for event in events)
    current = next(
        (
            event
            for event in reversed(events)
            if event.get("type") in {"action.started", "deployment.sleeping", "round.started"}
        ),
        None,
    )
    return {
        "captures": captures,
        "submissions": len(submissions),
        "accepted": accepted,
        "failures": failures,
        "last_event": events[-1] if events else None,
        "current_activity": current,
    }


def _deployment_payload(row: sqlite3.Row, include_config: bool = False) -> dict[str, Any]:
    status = str(row["status"])
    pid = row["pid"]
    if status in ACTIVE_STATUSES:
        running, live_pid = _runtime_status(int(row["team_id"]), str(row["id"]))
        next_status = (
            "RUNNING" if running else ("DEPLOYING" if status == "DEPLOYING" else "STOPPED")
        )
        if next_status != status or live_pid != pid:
            STORE.update(str(row["id"]), status=next_status, pid=live_pid)
            status, pid = next_status, live_pid

    events = _deployment_events(row)
    payload: dict[str, Any] = {
        "id": row["id"],
        "team_id": row["team_id"],
        "bot_name": row["bot_name"],
        "status": status,
        "pid": pid,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stopped_at": row["stopped_at"],
        "error": row["error"],
        "container_up": bool((_docker_state(int(row["team_id"])) or {}).get("Running")),
        "summary": _event_summary(events),
        # AI-006: stable agent identity fields (no credentials)
        "agent_type": str(row["agent_type"] if row["agent_type"] else "scripted"),
        "agent_id": str(row["agent_id"] if row["agent_id"] else row["id"]),
        "run_id": str(row["run_id"] if row["run_id"] else row["id"]),
        "provider": str(row["provider"] if row["provider"] else "scripted"),
        "model_id": str(row["model_id"] if row["model_id"] else ""),
    }
    if include_config:
        payload["config"] = json.loads(row["config_json"])
    return payload


def _validate_teams(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("teams must be a list")
    teams = sorted({int(item) for item in value})
    if not teams:
        raise ValueError("select at least one team")
    if any(team < 1 or team > ARENA_DEFAULTS.team_count for team in teams):
        raise ValueError("team id is outside the configured arena")
    return teams


def _public_config(body: dict[str, Any]) -> dict[str, Any]:
    actions = body.get("actions", ["recon.health"])
    if not isinstance(actions, list) or not actions:
        raise ValueError("select at least one action")
    config = {
        "bot_name": str(body.get("bot_name", "Sweep Bot")).strip()[:80] or "Sweep Bot",
        "planner": str(body.get("planner", "scripted")).strip()[:120],
        "target_policy": str(body.get("target_policy", "all_opponents")),
        "target_teams": [int(team) for team in body.get("target_teams", [])],
        "actions": [str(action)[:120] for action in actions],
        "loop_interval": max(0, min(86400, int(body.get("loop_interval", 60)))),
        "watchdog": bool(body.get("watchdog", False)),
        "flag_re": str(body.get("flag_re", r"FLAG\{[a-f0-9]{32}\}"))[:300],
        "stop_on_success": bool(body.get("stop_on_success", True)),
        "timeout": max(1, min(120, int(body.get("timeout", 6)))),
        "service_port": ARENA_DEFAULTS.service_port,
        "ip_pattern": ARENA_DEFAULTS.service_ip_pattern,
    }
    if config["target_policy"] not in {"all_opponents", "selected"}:
        raise ValueError("unsupported target policy")
    if config["target_policy"] == "selected" and not config["target_teams"]:
        raise ValueError("select at least one target team")
    if any(team < 1 or team > ARENA_DEFAULTS.team_count for team in config["target_teams"]):
        raise ValueError("target team is outside the configured arena")
    action_ids = {action["id"] for action in action_catalog()}
    if any(action not in action_ids for action in config["actions"]):
        raise ValueError("deployment contains an unknown action")
    return config


def _stop_deployment(row: sqlite3.Row, final_status: str = "STOPPED") -> tuple[bool, str]:
    if row["status"] not in ACTIVE_STATUSES:
        return True, "deployment is already inactive"
    _archive(row)
    rc, output = _run(["bash", str(DEPLOY_SH), "--stop", str(row["team_id"])])
    STORE.update(
        str(row["id"]),
        status=final_status,
        stopped_at=_now(),
        pid=None,
        error=None if rc == 0 else output,
    )
    PLAN_CREDENTIALS.deactivate(str(row["id"]))
    # Emit agent run_stopped telemetry (AI-007)
    try:
        from bot_lib.agent_contracts import AgentType
        from bot_lib.agent_telemetry import AgentTelemetry

        raw_type = str(row["agent_type"] or "scripted")
        try:
            atype = AgentType(raw_type)
        except ValueError:
            atype = AgentType.ATTACK_DEFENSE
        telem = AgentTelemetry(
            memory=AGENT_MEMORY,
            agent_id=str(row["agent_id"] or row["id"]),
            agent_type=atype,
            run_id=str(row["run_id"] or row["id"]),
        )
        telem.run_stopped(reason="stopped", final_status=final_status)
    except Exception:  # noqa: BLE001 — telemetry must not block stop
        pass
    return rc == 0, output


def _deploy_one(team_id: int, public_config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    # AI-006: enforce scoped uniqueness — stop conflicting active runs of the same type.
    planner = public_config.get("planner", "scripted")
    is_model_agent = planner == "model"
    agent_type_str = "attack_defense" if is_model_agent else "scripted"
    agent_id = f"team{team_id}-attack-defense" if is_model_agent else ""
    provider_str = str(ARENA_DEFAULTS.agent_provider) if is_model_agent else "scripted"
    model_id_str = str(ARENA_DEFAULTS.agent_model or "fake-v1") if is_model_agent else ""

    # Stop same-scope conflicting active runs (does NOT stop other agent types)
    conflict_scope = STORE.active_by_scope(agent_type_str, team_id)
    for active in conflict_scope:
        _stop_deployment(active, "SUPERSEDED")
    # Also stop any legacy scripted bots on this team (all-opponents compat)
    if not is_model_agent:
        for active in STORE.active_for_team(team_id):
            _stop_deployment(active, "SUPERSEDED")

    deployment_id = uuid.uuid4().hex[:12]
    run_id = deployment_id  # run_id == deployment_id for backward compat
    effective_agent_id = agent_id or deployment_id
    STORE.insert(
        deployment_id,
        team_id,
        public_config,
        agent_type=agent_type_str,
        agent_id=effective_agent_id,
        run_id=run_id,
        provider=provider_str,
        model_id=model_id_str,
    )
    runtime_config = {
        **public_config,
        "deployment_id": deployment_id,
        "gameserver_url": _gameserver_url(),
        "submission_token": _team_token(team_id),
    }
    runtime_config.pop("loop_interval", None)
    runtime_config.pop("watchdog", None)
    plan_token = ""
    if is_model_agent:
        plan_token = PLAN_CREDENTIALS.issue(deployment_id, team_id)

    # Emit agent.run_started telemetry (AI-007)
    try:
        from bot_lib.agent_contracts import AgentType
        from bot_lib.agent_telemetry import AgentTelemetry

        try:
            atype = AgentType(agent_type_str)
        except ValueError:
            atype = AgentType.ATTACK_DEFENSE
        telem = AgentTelemetry(
            memory=AGENT_MEMORY,
            agent_id=effective_agent_id,
            agent_type=atype,
            run_id=run_id,
        )
        telem.run_started(team_id=team_id, provider=provider_str, model_id=model_id_str)
    except Exception:  # noqa: BLE001 — telemetry must not block deploy
        pass

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="sandcastle_bot_", delete=False
        ) as handle:
            json.dump(runtime_config, handle)
            temp_path = handle.name
        env = os.environ.copy()
        env["LOOP_INTERVAL"] = str(public_config["loop_interval"])
        env["WATCHDOG"] = "true" if public_config["watchdog"] else "false"
        if plan_token:
            env["PLAN_ENDPOINT"] = _planning_url()
            env["PLAN_TOKEN"] = plan_token
        rc, output = _run(
            [
                "bash",
                str(DEPLOY_SH),
                "--deployment-id",
                deployment_id,
                "--config",
                temp_path,
                str(team_id),
            ],
            timeout=75,
            env=env,
        )
        running, pid = _runtime_status(team_id, deployment_id)
        status = "RUNNING" if rc == 0 and running else "FAILED"
        STORE.update(
            deployment_id,
            status=status,
            pid=pid,
            error=None if status == "RUNNING" else output,
        )
        if status != "RUNNING":
            PLAN_CREDENTIALS.deactivate(deployment_id)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    row = STORE.get(deployment_id)
    assert row is not None
    return _deployment_payload(row, include_config=True), output


def _available_providers() -> list[dict[str, Any]]:
    """Return which providers are configured (no keys exposed)."""
    providers = [
        {"id": "fake", "label": "Fake (no cost, offline)", "available": True},
        {
            "id": "openai",
            "label": "OpenAI",
            "available": bool(os.environ.get("OPENAI_API_KEY")),
            "models": ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"],
        },
        {
            "id": "gemini",
            "label": "Google Gemini",
            "available": bool(os.environ.get("GEMINI_API_KEY")),
            "models": ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-1.5-pro"],
        },
    ]
    return providers


def _agent_log_markdown(run_id: str, limit: int = 100) -> str:
    """Render agent memory entries as a clean Markdown thinking log."""
    entries = AGENT_MEMORY.recent_as_dicts(run_id, limit=limit)
    if not entries:
        return f"# Agent Log\n\n*No entries yet for run `{run_id}`.*\n"

    lines: list[str] = [f"# Agent Log — `{run_id}`\n"]
    icon_map = {
        "tool_call": "⚙️",
        "tool_result": "✅",
        "error": "❌",
        "run_started": "🚀",
        "run_stopped": "🛑",
        "observation": "👁️",
        "plan": "🧠",
    }

    for e in entries:
        kind = e.get("kind", "event")
        icon = icon_map.get(kind, "📝")
        ts = e.get("created_at", "")[:19].replace("T", " ")
        summary = e.get("summary", "")
        data = e.get("data") or {}

        tool_id = data.get("tool_id") or data.get("action_id") or ""
        title_parts = [icon, kind]
        if tool_id:
            title_parts.append(f"`{tool_id}`")
        title_parts.append(f"• {ts} UTC")
        lines.append(f"## {' '.join(title_parts)}\n")

        if summary:
            lines.append(f"**Summary:** {summary}\n")

        status = data.get("status") or e.get("status", "")
        if status:
            status_icon = (
                "✅"
                if status in ("ok", "committed", "planted")
                else "❌"
                if status in ("error", "failed")
                else "ℹ️"
            )
            lines.append(f"**Status:** {status_icon} `{status}`\n")

        display_data = {
            k: v
            for k, v in data.items()
            if k not in ("flag", "password", "token", "secret", "api_key")
        }
        if display_data:
            try:
                json_str = json.dumps(display_data, indent=2, ensure_ascii=False)
                lines.append(f"```json\n{json_str[:2000]}\n```\n")
            except Exception:  # noqa: BLE001
                pass

        lines.append("---\n")

    return "\n".join(lines)


class BotAPIHandler(BaseHTTPRequestHandler):
    server_version = "SandcastleBotController/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[bot-controller] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        self.send_header(
            "Access-Control-Allow-Origin",
            origin if ALLOWED_ORIGINS.match(origin) else "http://localhost:5173",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Vary", "Origin")

    def _json(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _plan(self) -> None:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or not hmac.compare_digest(scheme.lower(), "bearer"):
            self._json(401, {"error": "missing planning bearer token"})
            return
        identity = _planning_identity(token)
        if identity is None:
            self._json(401, {"error": "invalid or expired planning token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid content length"})
            return
        if length <= 0 or length > MAX_PLAN_REQUEST_BYTES:
            self._json(413, {"error": "planning request size is invalid"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise PlanningRequestError("planning request must be an object")
            response = PLANNING_SERVICE.plan(identity, payload)
        except (json.JSONDecodeError, PlanningRequestError, TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        except ModelBudgetExceeded as exc:
            self._json(
                429,
                {
                    "error": "agent budget exhausted",
                    "budget": exc.rejection.as_dict(),
                },
            )
            return
        except ModelGatewayError as exc:
            self._json(503, {"error": str(exc)})
            return
        self._json(200, response)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            self._json(200, {"ok": True, "service": "bot-controller"})
            return
        if path == "/catalog":
            self._json(200, {"actions": action_catalog(), "planners": planner_catalog()})
            return
        if path in {"/arena", "/teams"}:
            body = {
                "num_teams": ARENA_DEFAULTS.team_count,
                "service_port": ARENA_DEFAULTS.service_port,
                "ip_pattern": ARENA_DEFAULTS.service_ip_pattern,
                "ssh_base_port": ARENA_DEFAULTS.ssh_base_port,
            }
            self._json(200, body)
            return
        if path == "/deployments":
            deployments = [_deployment_payload(row) for row in STORE.list()]
            self._json(200, {"deployments": deployments})
            return
        if path == "/status":
            deployments = [_deployment_payload(row) for row in STORE.list()]
            latest: dict[int, dict[str, Any]] = {}
            for item in deployments:
                latest.setdefault(int(item["team_id"]), item)
            teams = []
            for team_id in range(1, ARENA_DEFAULTS.team_count + 1):
                deployment = latest.get(team_id)
                state = _docker_state(team_id)
                teams.append(
                    {
                        "id": team_id,
                        "container_up": bool(state and state.get("Running")),
                        "running": bool(deployment and deployment["status"] == "RUNNING"),
                        "pid": deployment["pid"] if deployment else None,
                        "deployment_id": deployment["id"] if deployment else None,
                    }
                )
            self._json(200, {"teams": teams})
            return
        if path == "/resources":
            self._json(200, _resource_status())
            return
        if path == "/model/usage":
            query = parse_qs(parsed.query)
            run_id = query.get("run_id", [None])[0]
            match_raw = query.get("match_id", [None])[0]
            day = query.get("utc_day", [None])[0]
            agent_type_filter = query.get("agent_type", [None])[0]
            if match_raw is not None and not str(match_raw).isdigit():
                self._json(400, {"error": "match_id must be an integer"})
                return
            usage_summary = BUDGET_LEDGER.summary(
                run_id=run_id,
                match_id=int(match_raw) if match_raw is not None else None,
                utc_day=day,
            )
            # AI-007: per-agent-type aggregate if requested
            if agent_type_filter:
                rows = STORE.list_by_agent_type(agent_type_filter)
                run_ids = [str(r["run_id"] or r["id"]) for r in rows]
                type_cost = 0.0
                type_calls = 0
                for rid in run_ids:
                    s = BUDGET_LEDGER.summary(run_id=rid)
                    type_cost += s.get("total_cost_usd", 0.0)
                    type_calls += s.get("total_calls", 0)
                usage_summary["agent_type_filter"] = agent_type_filter
                usage_summary["agent_type_total_cost_usd"] = round(type_cost, 8)
                usage_summary["agent_type_total_calls"] = type_calls
            self._json(200, usage_summary)
            return

        match = re.fullmatch(r"/deployments/([a-zA-Z0-9._-]+)(?:/(events|logs))?", path)
        if match:
            row = STORE.get(match.group(1))
            if row is None:
                self._json(404, {"error": "deployment not found"})
                return
            resource = match.group(2)
            if resource == "events":
                limit = min(1000, max(1, int(parse_qs(parsed.query).get("limit", ["300"])[0])))
                self._json(200, {"events": _deployment_events(row)[-limit:]})
            elif resource == "logs":
                self._json(200, {"lines": _deployment_logs(row)})
            else:
                self._json(200, {"deployment": _deployment_payload(row, include_config=True)})
            return

        legacy_logs = re.fullmatch(r"/logs/(\d+)", path)
        if legacy_logs:
            team_id = int(legacy_logs.group(1))
            active = STORE.active_for_team(team_id)
            lines = _deployment_logs(active[0]) if active else []
            self._json(200, {"team_id": team_id, "lines": lines})
            return

        # Challenge Lab: options, list, detail, log
        if path == "/challenges/options":
            self._json(200, _CHALLENGE_OPTIONS)
            return

        if path == "/challenges":
            rows = CHALLENGE_STORE.list(limit=100)
            self._json(200, {"challenges": [ChallengeRunStore.payload(r) for r in rows]})
            return

        challenge_match = re.fullmatch(r"/challenges/([a-zA-Z0-9._-]+)(?:/(log|deploy))?", path)
        if challenge_match:
            run_id = challenge_match.group(1)
            resource = challenge_match.group(2)
            row = CHALLENGE_STORE.get(run_id)
            if row is None:
                self._json(404, {"error": "challenge run not found"})
                return
            if resource == "log":
                query = parse_qs(parsed.query)
                try:
                    limit = min(200, max(1, int(query.get("limit", ["100"])[0])))
                except (ValueError, TypeError):
                    limit = 100
                md = _agent_log_markdown(run_id, limit=limit)
                data = md.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
                return
            # GET /challenges/<id> — detail (no deploy on GET)
            self._json(200, {"challenge": ChallengeRunStore.payload(row)})
            return

        # AI-015: /providers — available model providers (no keys exposed)
        if path == "/providers":
            self._json(200, {"providers": _available_providers()})
            return

        # AI-006: /agent-runs endpoints
        if path == "/agent-runs":
            query = parse_qs(parsed.query)
            agent_type_filter = query.get("agent_type", [None])[0]
            if agent_type_filter:
                rows = STORE.list_by_agent_type(agent_type_filter)
            else:
                rows = STORE.list()
            self._json(200, {"agent_runs": [_deployment_payload(row) for row in rows]})
            return

        agent_run_match = re.fullmatch(r"/agent-runs/([a-zA-Z0-9._-]+)(?:/(memory|log))?", path)
        if agent_run_match:
            row = STORE.get(agent_run_match.group(1))
            if row is None:
                self._json(404, {"error": "agent run not found"})
                return
            resource = agent_run_match.group(2)
            if resource == "log":
                # AI-015: Markdown thinking log
                query = parse_qs(parsed.query)
                try:
                    limit = min(200, max(1, int(query.get("limit", ["100"])[0])))
                except (ValueError, TypeError):
                    limit = 100
                run_id_key = str(row["run_id"] or row["id"])
                md = _agent_log_markdown(run_id_key, limit=limit)
                data = md.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
                return

            if resource == "memory":
                # AI-007: bounded, redacted memory entries
                query = parse_qs(parsed.query)
                try:
                    limit = min(200, max(1, int(query.get("limit", ["50"])[0])))
                except (ValueError, TypeError):
                    limit = 50
                run_id_key = str(row["run_id"] or row["id"])
                entries = AGENT_MEMORY.recent_as_dicts(run_id_key, limit=limit)
                self._json(200, {"run_id": run_id_key, "entries": entries})
            else:
                self._json(
                    200,
                    {"agent_run": _deployment_payload(row, include_config=False)},
                )
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path == "/plan":
            self._plan()
            return
        body = self._body()
        if path in {"/deployments", "/deploy"}:
            try:
                teams = _validate_teams(body.get("teams"))
                config = _public_config(body)
                results = [_deploy_one(team, config) for team in teams]
            except (KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            deployments = [result[0] for result in results]
            output = "\n\n".join(result[1] for result in results if result[1])
            ok = all(item["status"] == "RUNNING" for item in deployments)
            self._json(
                201 if ok else 500,
                {"ok": ok, "deployments": deployments, "output": output},
            )
            return

        stop_match = re.fullmatch(r"/deployments/([a-zA-Z0-9._-]+)/stop", path)
        if stop_match:
            row = STORE.get(stop_match.group(1))
            if row is None:
                self._json(404, {"error": "deployment not found"})
                return
            ok, output = _stop_deployment(row)
            refreshed = STORE.get(str(row["id"]))
            assert refreshed is not None
            self._json(
                200 if ok else 500,
                {"ok": ok, "output": output, "deployment": _deployment_payload(refreshed)},
            )
            return

        if path == "/stop":
            try:
                teams = _validate_teams(body.get("teams"))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            outputs: list[str] = []
            ok = True
            for team_id in teams:
                for row in STORE.active_for_team(team_id):
                    stopped, output = _stop_deployment(row)
                    ok = ok and stopped
                    outputs.append(output)
            self._json(200 if ok else 500, {"ok": ok, "output": "\n".join(outputs)})
            return

        # Challenge Lab POST routes
        if path == "/challenges/generate":
            vuln = body.get("vulnerability", "path_traversal")
            diff = body.get("difficulty", "easy")
            raw_seed = body.get("seed")
            try:
                seed = int(raw_seed) if raw_seed is not None else int(uuid.uuid4().int % (2**31))
                decoy = max(0, min(3, int(body.get("decoy_endpoints", 0))))
                attempts = max(1, min(5, int(body.get("max_attempts", 3))))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            import json as _json

            spec_dict = {
                "vulnerability": vuln,
                "difficulty": diff,
                "seed": seed,
                "decoy_endpoints": decoy,
            }
            run_id = CHALLENGE_STORE.insert(
                vulnerability=vuln,
                difficulty=diff,
                seed=seed,
                decoy_endpoints=decoy,
                max_attempts=attempts,
                provider=body.get("provider", "fake"),
                model_id=body.get("model_id", ""),
                spec_json=_json.dumps(spec_dict),
            )
            t = threading.Thread(
                target=_generate_challenge_bg,
                args=(run_id, vuln, diff, seed, decoy, attempts),
                daemon=True,
            )
            t.start()
            row = CHALLENGE_STORE.get(run_id)
            assert row is not None
            self._json(201, {"ok": True, "challenge": ChallengeRunStore.payload(row)})
            return

        challenge_deploy_match = re.fullmatch(r"/challenges/([a-zA-Z0-9._-]+)/deploy", path)
        if challenge_deploy_match:
            run_id = challenge_deploy_match.group(1)
            row = CHALLENGE_STORE.get(run_id)
            if row is None:
                self._json(404, {"error": "challenge run not found"})
                return
            if row.get("status") != "published":
                self._json(
                    409, {"error": f"challenge is not published (status={row.get('status')})"}
                )
                return
            challenge_id = row.get("challenge_id")
            if not challenge_id:
                self._json(409, {"error": "no challenge_id — publish must complete first"})
                return
            ok, output = _deploy_challenge_to_arena(challenge_id)
            if ok:
                CHALLENGE_STORE.update(run_id, deployed_at=_now())
                row = CHALLENGE_STORE.get(run_id)
            self._json(
                200 if ok else 500,
                {
                    "ok": ok,
                    "challenge_id": challenge_id,
                    "output": output[-4000:],  # cap to avoid huge responses
                    "challenge": ChallengeRunStore.payload(row) if row else None,
                },
            )
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandcastle bot deployment controller")
    parser.add_argument("--port", type=int, default=ARENA_DEFAULTS.bot_api_port)
    parser.add_argument("--host", default=ARENA_DEFAULTS.bot_api_host)
    args = parser.parse_args()

    if not DEPLOY_SH.is_file():
        print(f"bot controller: missing {DEPLOY_SH}", file=sys.stderr)
        raise SystemExit(1)

    server = ThreadingHTTPServer((args.host, args.port), BotAPIHandler)
    print(f"[*] Bot controller listening on http://{args.host}:{args.port}")
    print(f"[*] State database: {DATABASE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Bot controller stopped")


if __name__ == "__main__":
    main()
