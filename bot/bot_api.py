#!/usr/bin/env python3
"""Local deployment controller for Sandcastle team bots."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot_lib import ARENA_DEFAULTS, action_catalog, planner_catalog
from bot_lib.agent_contracts import AgentMemoryEntry, AgentType, BudgetPolicy, ModelProvider, ModelRequest
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


class MatchPlanStore:
    """Persistent pre-match assignments for bots and attack/defense agents."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS match_agent_assignments (
                    team_id INTEGER PRIMARY KEY,
                    assignment_kind TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, team_id: int, assignment_kind: str, config: dict[str, Any]) -> None:
        timestamp = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO match_agent_assignments
                    (team_id, assignment_kind, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    assignment_kind = excluded.assignment_kind,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (team_id, assignment_kind, json.dumps(config, sort_keys=True), timestamp, timestamp),
            )

    def delete(self, team_ids: list[int] | None = None) -> int:
        with self.connect() as conn:
            if team_ids is None:
                cursor = conn.execute("DELETE FROM match_agent_assignments")
            else:
                cursor = conn.executemany(
                    "DELETE FROM match_agent_assignments WHERE team_id = ?",
                    [(team_id,) for team_id in team_ids],
                )
            return cursor.rowcount if cursor.rowcount is not None else 0

    def list(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM match_agent_assignments ORDER BY team_id ASC"
            ).fetchall()


class _UnavailableProvider:
    charges_budget = False

    def __init__(self, provider: ModelProvider, message: str) -> None:
        self.provider = provider
        self.message = message

    def complete(self, request, timeout):
        del request, timeout
        raise ModelGatewayError(self.message)


PROVIDER_MODELS: dict[ModelProvider, tuple[str, ...]] = {
    ModelProvider.FAKE: ("fake-v1",),
    ModelProvider.OPENAI: ("gpt-5.4-mini", "gpt-4o-mini"),
    ModelProvider.GEMINI: ("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-1.5-pro"),
}


def _default_model_for_provider(provider: ModelProvider) -> str:
    models = PROVIDER_MODELS.get(provider)
    if models:
        return models[0]
    return f"{provider.value}-default"


def _normalize_model_id(provider: ModelProvider, model_id: str | None = None) -> str:
    candidate = str(model_id or "").strip()
    if not candidate:
        return _default_model_for_provider(provider)
    known = PROVIDER_MODELS.get(provider, ())
    if known and candidate not in known:
        allowed = ", ".join(known)
        raise ValueError(
            f"model {candidate!r} is not valid for provider {provider.value}; choose one of: {allowed}"
        )
    return candidate


def _model_budget_policy() -> BudgetPolicy:
    return BudgetPolicy(
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


def _build_model_adapters(
    model_id: str | None = None,
    *,
    primary_provider: ModelProvider | None = None,
) -> dict[ModelProvider, ModelProviderAdapter]:
    primary = primary_provider or ModelProvider(ARENA_DEFAULTS.agent_provider)
    requested_model = _normalize_model_id(primary, model_id)
    adapters: dict[ModelProvider, ModelProviderAdapter] = {
        ModelProvider.FAKE: DeterministicPlanningFakeProvider()
    }
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        adapters[ModelProvider.OPENAI] = OpenAIProvider(
            api_key=openai_key,
            model_id=(
                requested_model
                if primary is ModelProvider.OPENAI
                else _default_model_for_provider(ModelProvider.OPENAI)
            ),
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
            model_id=(
                requested_model
                if primary is ModelProvider.GEMINI
                else _default_model_for_provider(ModelProvider.GEMINI)
            ),
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
    return adapters


def _build_budgeted_gateway(
    *,
    provider: ModelProvider | None = None,
    fallback_provider: ModelProvider | None = None,
    model_id: str | None = None,
) -> tuple[BudgetedModelGateway, BudgetPolicy, str]:
    primary = provider or ModelProvider(ARENA_DEFAULTS.agent_provider)
    fallback = fallback_provider or ModelProvider(ARENA_DEFAULTS.agent_fallback_provider)
    policy = _model_budget_policy()
    gateway = ModelGateway(
        _build_model_adapters(model_id, primary_provider=primary),
        primary_provider=primary,
        fallback_provider=fallback,
        max_retries=policy.max_retries,
    )
    effective_model = _normalize_model_id(primary, model_id)
    return BudgetedModelGateway(gateway, BUDGET_LEDGER), policy, effective_model


def _build_planning_service(
    memory: "AgentMemoryStore | None" = None,
    *,
    provider: ModelProvider | None = None,
    model_id: str | None = None,
) -> AgentPlanningService:
    primary = provider or ModelProvider(ARENA_DEFAULTS.agent_provider)
    gateway, policy, effective_model = _build_budgeted_gateway(provider=primary, model_id=model_id)
    return AgentPlanningService(
        gateway,
        num_teams=ARENA_DEFAULTS.team_count,
        model_id=effective_model,
        budget=policy,
        memory=memory,
    )


STORE = DeploymentStore(DATABASE)
MATCH_PLAN = MatchPlanStore(DATABASE)
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


def _safe_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def _artifact_tree(root: Path, *, max_entries: int = 80) -> tuple[list[str], bool]:
    entries: list[str] = []
    truncated = False
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.name.endswith(".pyc"):
            continue
        rel = path.relative_to(root)
        depth = len(rel.parts) - 1
        prefix = "  " * depth + ("- " if path.is_file() else "+ ")
        entries.append(f"{prefix}{rel.name}")
        if len(entries) >= max_entries:
            truncated = True
            break
    return entries, truncated


def _challenge_artifact_summary(challenge_id: str | None) -> dict[str, Any] | None:
    if not challenge_id:
        return None
    root = REPO_ROOT / "challenges" / "published" / challenge_id
    if not root.is_dir():
        return None
    files = [
        _safe_rel(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    ]
    tree, truncated = _artifact_tree(root)
    manifest: dict[str, Any] = {}
    registry_manifest: dict[str, Any] = {}
    for name, target in (("manifest.json", manifest), ("registry_manifest.json", registry_manifest)):
        manifest_path = root / name
        if manifest_path.is_file():
            try:
                target.update(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
    return {
        "challenge_id": challenge_id,
        "path": _safe_rel(root),
        "file_count": len(files),
        "files": files[:80],
        "tree": "\n".join(tree),
        "tree_truncated": truncated,
        "service": manifest.get("service", {}),
        "spec": manifest.get("spec", {}),
        "registry": registry_manifest,
    }


def _challenge_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = ChallengeRunStore.payload(row)
    payload["artifact"] = _challenge_artifact_summary(payload.get("challenge_id"))
    return payload


def _record_challenge_artifact(run_id: str, challenge_id: str) -> None:
    artifact = _challenge_artifact_summary(challenge_id)
    if not artifact:
        return
    tree = str(artifact.get("tree") or "")
    summary = (
        f"Published challenge {challenge_id}: "
        f"{artifact.get('file_count', 0)} files under {artifact.get('path')}"
    )
    if tree:
        summary = f"{summary}\n\nCreated file tree:\n{tree}"
    AGENT_MEMORY.append(
        AgentMemoryEntry(
            agent_id="challenge-generator",
            agent_type=AgentType.CHALLENGE_GENERATOR,
            run_id=run_id,
            kind="artifact",
            summary=summary[:2000],
            data=artifact,
        )
    )


def _append_challenge_memory(
    run_id: str,
    *,
    kind: str,
    summary: str,
    data: dict[str, Any] | None = None,
) -> None:
    try:
        AGENT_MEMORY.append(
            AgentMemoryEntry(
                agent_id="challenge-generator",
                agent_type=AgentType.CHALLENGE_GENERATOR,
                run_id=run_id,
                kind=kind,
                summary=summary[:2000],
                data=data or {},
            )
        )
    except Exception:
        pass


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
    provider: str = "fake",
    model_id: str = "",
) -> None:
    """Run ChallengeGeneratorAgent in a background thread.

    Updates CHALLENGE_STORE with status=published or status=failed on completion.
    All thinking steps are persisted to AGENT_MEMORY (same run_id) for the
    /challenges/<id>/log endpoint.
    """
    try:
        import sys as _sys

        _sys.path.insert(0, str(REPO_ROOT / "bot"))
        from challenge.agent import TOOL_SCHEMAS, ChallengeGeneratorAgent, ToolRejectedError
        from challenge.registry import ChallengeRegistry
        from challenge.validator import ChallengeValidator

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

        selected_provider = ModelProvider(str(provider or "fake"))
        gateway, budget, effective_model = _build_budgeted_gateway(
            provider=selected_provider,
            model_id=model_id,
        )
        spec_dict = {
            "vulnerability": vulnerability,
            "difficulty": difficulty,
            "seed": seed,
            "decoy_endpoints": decoy_endpoints,
            "max_attempts": max_attempts,
        }
        _append_challenge_memory(
            run_id,
            kind="observation",
            summary=(
                f"challenge request accepted: {vulnerability} / {difficulty}, "
                f"provider={selected_provider.value}, model={effective_model}"
            ),
            data={
                **spec_dict,
                "provider": selected_provider.value,
                "model_id": effective_model,
                "staging_root": _safe_rel(staging),
                "published_root": _safe_rel(published_root),
            },
        )
        state = agent.start({**spec_dict, "run_id": run_id, "provider": selected_provider.value, "model_id": effective_model})
        max_steps = max(4, max_attempts * 4)
        system_prompt = (
            "You are Sandcastle's ChallengeGeneratorAgent. Create and publish one "
            "bounded Flask challenge by selecting exactly one registered tool per step. "
            "Use challenge.spec.create first, then render, validate, revise or inspect "
            "if validation fails, and publish only after validation passed. Never request "
            "unregistered tools and never write files directly."
        )

        for step in range(1, max_steps + 1):
            if state.status != "running":
                break
            observation = agent.build_observation(state)
            observation.update(
                {
                    "requested_vulnerability": vulnerability,
                    "requested_difficulty": difficulty,
                    "requested_seed": seed,
                    "requested_decoy_endpoints": decoy_endpoints,
                    "step": step,
                }
            )
            request = ModelRequest(
                agent_id=state.agent_id,
                agent_type=AgentType.CHALLENGE_GENERATOR,
                run_id=run_id,
                correlation_id=f"{run_id}-challenge-step-{step}",
                system_prompt=system_prompt,
                observation=observation,
                tool_schemas=TOOL_SCHEMAS,
                budget=budget,
                round_number=step,
                team_id=None,
            )
            _append_challenge_memory(
                run_id,
                kind="model_request",
                summary=f"requesting challenge step {step} from {selected_provider.value}/{effective_model}",
                data={
                    "step": step,
                    "provider": selected_provider.value,
                    "model_id": effective_model,
                    "current_spec": observation.get("current_spec"),
                    "last_render_id": observation.get("last_render_id"),
                    "last_validation": observation.get("last_validation"),
                },
            )
            try:
                gateway_result = gateway.call(
                    request,
                    model_id=effective_model,
                    estimated_cost_usd=budget.max_cost_usd_per_call,
                )
            except (ModelBudgetExceeded, ModelGatewayError) as exc:
                state.status = "failed"
                state.error = str(exc)
                _append_challenge_memory(
                    run_id,
                    kind="error",
                    summary=f"model call failed at step {step}: {exc}",
                    data={
                        "step": step,
                        "provider": selected_provider.value,
                        "model_id": effective_model,
                        "error_type": type(exc).__name__,
                    },
                )
                break
            calls = gateway_result.response.tool_calls[:1]
            _append_challenge_memory(
                run_id,
                kind="plan",
                summary=(
                    f"model selected {calls[0].tool_id if calls else 'no tool'} "
                    f"via {gateway_result.response.provider.value}/{gateway_result.response.model_id}"
                ),
                data={
                    "step": step,
                    "provider": gateway_result.response.provider.value,
                    "model_id": gateway_result.response.model_id,
                    "used_fallback": gateway_result.used_fallback,
                    "finish_reason": gateway_result.response.finish_reason,
                    "usage": gateway_result.response.usage.as_dict(),
                    "tool_calls": [call.as_dict() for call in calls],
                },
            )
            if not calls:
                state.status = "failed"
                state.error = "model returned no challenge tool call"
                _append_challenge_memory(
                    run_id,
                    kind="error",
                    summary=state.error,
                    data={
                        "step": step,
                        "provider": gateway_result.response.provider.value,
                        "model_id": gateway_result.response.model_id,
                        "finish_reason": gateway_result.response.finish_reason,
                    },
                )
                break
            try:
                result = agent.execute_tool(state, calls[0])
            except ToolRejectedError as exc:
                AGENT_MEMORY.append(
                    AgentMemoryEntry(
                        agent_id=state.agent_id,
                        agent_type=AgentType.CHALLENGE_GENERATOR,
                        run_id=run_id,
                        kind="error",
                        summary=f"rejected challenge tool: {exc}",
                        data={"step": step, "tool_call": calls[0].as_dict()},
                    )
                )
                state.error = str(exc)
                continue
            if result.status == "error":
                state.error = result.summary
            _append_challenge_memory(
                run_id,
                kind="observation",
                summary=f"state after {calls[0].tool_id}: {state.status}",
                data={
                    "step": step,
                    "status": state.status,
                    "attempt": state.attempt,
                    "last_render_id": state.last_render_id,
                    "published_challenge_id": state.published_challenge_id,
                    "last_validation": {
                        "status": (state.last_validation or {}).get("status"),
                        "exploit_succeeded": (state.last_validation or {}).get("vulnerable_exploit_succeeded"),
                        "checker_before": (state.last_validation or {}).get("checker_passed_before_patch"),
                    } if state.last_validation else None,
                },
            )

        if state.status == "running" and not state.published_challenge_id:
            state.status = "failed"
            state.error = state.error or f"maximum model steps exhausted ({max_steps})"
            _append_challenge_memory(
                run_id,
                kind="error",
                summary=state.error,
                data={"max_steps": max_steps, "attempt": state.attempt},
            )

        challenge_id = state.published_challenge_id
        if challenge_id:
            import json as _json

            CHALLENGE_STORE.update(
                run_id,
                status="published",
                challenge_id=challenge_id,
                spec_json=_json.dumps(spec_dict),
            )
            _record_challenge_artifact(run_id, challenge_id)
        else:
            error = state.error or "generation did not complete"
            CHALLENGE_STORE.update(run_id, status="failed", error=error)
            _append_challenge_memory(
                run_id,
                kind="error",
                summary=f"challenge generation failed: {error}",
                data={"status": state.status, "error": error},
            )

    except Exception as exc:  # noqa: BLE001
        CHALLENGE_STORE.update(run_id, status="failed", error=str(exc))
        _append_challenge_memory(
            run_id,
            kind="error",
            summary=f"challenge generator crashed: {exc}",
            data={"exception": type(exc).__name__, "traceback": traceback.format_exc(limit=8)},
        )


def _deploy_challenge_to_arena(challenge_id: str) -> tuple[bool, str]:
    """Inject a published challenge into all team containers.

    The bot controller runs inside a container with the host Docker socket.
    Running the host-level setup/arena scripts from there would make Docker
    Compose resolve bind paths against the controller filesystem. Instead,
    deploy directly through the already-running team vulnerable machines:

    - copy the published challenge into each team's mounted service workspace
    - replace docker-compose.yml with the arena-scoped app compose
    - rebuild/recreate teamN-vuln-app from inside teamN-vuln
    - update the gameserver checker to a generated-challenge compatible checker

    Returns (ok, output).
    """
    challenge_path = REPO_ROOT / "challenges" / "published" / challenge_id
    if not challenge_path.is_dir():
        return False, f"challenge directory not found: {challenge_path}"

    outputs: list[str] = []
    ok = True
    for team_id in range(1, ARENA_DEFAULTS.team_count + 1):
        team_ok, team_output = _deploy_challenge_to_team(team_id, challenge_path)
        outputs.append(f"team{team_id}:\n{team_output}")
        ok = ok and team_ok
        if not team_ok:
            break

    if ok:
        checker_ok, checker_output = _install_generated_checker(challenge_path)
        outputs.append(f"gameserver checker:\n{checker_output}")
        ok = checker_ok

    return ok, "\n\n".join(outputs)


def _team_username(team_id: int) -> str:
    pattern = _arena_values().get("ARENA_TEAM_USERNAME_PATTERN", "team{team}")
    return pattern.replace("{team}", str(team_id))


def _checker_credentials(team_id: int, service_name: str = "example-vuln") -> dict[str, str]:
    secret = _arena_values().get("ARENA_CHECKER_SECRET", "")
    if not secret:
        secret = "sandcastle-local-checker-secret-change-me"
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", service_name).strip("_") or "service"

    def derive(purpose: str) -> str:
        scope = f"sandcastle-checker-v1:{purpose}:{team_id}:{service_name}"
        return hmac.new(secret.encode("utf-8"), scope.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "username": f"checker_t{team_id}_{slug}"[:64],
        "password": derive("password"),
        "plant_token": derive("plant"),
    }


def _team_app_compose(team_id: int) -> str:
    values = _arena_values()
    credentials = _checker_credentials(team_id)
    service_port = str(ARENA_DEFAULTS.service_port)
    app_mem = values.get("ARENA_TEAM_APP_MEM_LIMIT", "256m")
    app_cpu = values.get("ARENA_TEAM_APP_CPU_LIMIT", "0.50")
    app_pids = values.get("ARENA_TEAM_APP_PIDS_LIMIT", "256")
    max_restarts = values.get("ARENA_TEAM_MAX_RESTARTS", "3")
    log_size = values.get("ARENA_LOG_MAX_SIZE", "50m")
    log_files = values.get("ARENA_LOG_MAX_FILES", "3")
    isolation = values.get("ARENA_ISOLATION_MODE", "trusted")
    network_part = (
        f'    ports:\n      - "{service_port}:{service_port}"\n'
        if isolation == "dind"
        else f'    network_mode: "container:team{team_id}-vuln"\n'
    )
    return f"""name: sandcastle-team{team_id}

services:
  team{team_id}-vuln-app:
    build:
      context: .
    image: sandcastle/team{team_id}-vuln-app:latest
    container_name: team{team_id}-vuln-app
{network_part}    environment:
      TEAM_ID: "{team_id}"
      TEAM_NAME: "Team {team_id}"
      SERVICE_PORT: "{service_port}"
      SECRET_KEY: "sandcastle-team{team_id}-dev-secret"
      CHECKER_USERNAME: "{credentials["username"]}"
      CHECKER_PASSWORD: "{credentials["password"]}"
      PLANT_TOKEN: "{credentials["plant_token"]}"
    volumes:
      - team-data:/app/data
    labels:
      sandcastle.role: "vuln-app"
      sandcastle.team: "team{team_id}"
    deploy:
      resources:
        limits:
          memory: {app_mem}
          cpus: '{app_cpu}'
          pids: {app_pids}
    logging:
      driver: json-file
      options:
        max-size: "{log_size}"
        max-file: "{log_files}"
    restart: "on-failure:{max_restarts}"

volumes:
  team-data:
    name: sandcastle_team{team_id}-data
    labels:
      sandcastle.role: "vuln-data"
      sandcastle.team: "team{team_id}"
"""


def _deploy_challenge_to_team(team_id: int, challenge_path: Path) -> tuple[bool, str]:
    user = _team_username(team_id)
    container = f"team{team_id}-vuln"
    service_dir = f"/home/{user}/example-vuln"
    quoted_dir = shlex.quote(service_dir)
    outputs: list[str] = []

    rc, inspect_out = _run(["docker", "inspect", "--format", "{{.State.Running}}", container])
    if rc != 0 or inspect_out.strip() != "true":
        return False, f"{container} is not running; start the arena before deploying a challenge"

    rc, out = _run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            f"mkdir -p {quoted_dir} && find {quoted_dir} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +",
        ],
        timeout=30,
    )
    outputs.append(out)
    if rc != 0:
        return False, "\n".join(part for part in outputs if part)

    rc, out = _run(["docker", "cp", f"{challenge_path}/.", f"{container}:{service_dir}/"], timeout=60)
    outputs.append(out)
    if rc != 0:
        return False, "\n".join(part for part in outputs if part)

    compose_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", prefix=f"sandcastle_team{team_id}_compose_", delete=False
        ) as handle:
            handle.write(_team_app_compose(team_id))
            compose_path = handle.name
        rc, out = _run(
            ["docker", "cp", compose_path, f"{container}:{service_dir}/docker-compose.yml"],
            timeout=30,
        )
        outputs.append(out)
        if rc != 0:
            return False, "\n".join(part for part in outputs if part)
    finally:
        if compose_path:
            Path(compose_path).unlink(missing_ok=True)

    rc, out = _run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            (
                f"chown -R {shlex.quote(user)}:{shlex.quote(user)} {quoted_dir} && "
                f"cd {quoted_dir} && "
                f"(docker rm -f team{team_id}-vuln-app >/dev/null 2>&1 || true) && "
                "docker compose up -d --build --force-recreate --remove-orphans"
            ),
        ],
        timeout=180,
    )
    outputs.append(out)
    if rc != 0:
        return False, "\n".join(part for part in outputs if part)

    deadline = time.monotonic() + min(60, ARENA_DEFAULTS.agent_timeout_seconds * 4)
    last = ""
    while time.monotonic() < deadline:
        rc, out = _run(
            [
                "docker",
                "exec",
                container,
                "curl",
                "-fsS",
                "--max-time",
                "2",
                f"http://127.0.0.1:{ARENA_DEFAULTS.service_port}/health",
            ],
            timeout=5,
        )
        last = out
        if rc == 0 and "ok" in out.lower():
            outputs.append("health ok")
            return True, "\n".join(part for part in outputs if part)
        time.sleep(1)

    outputs.append(f"health check failed: {last}")
    return False, "\n".join(part for part in outputs if part)


def _generated_checker_wrapper() -> str:
    return '''from __future__ import annotations

import json
import urllib.error
import urllib.request

from checkers.contract import (
    CheckRequest,
    CheckerMetadata,
    CheckerOutcome,
    CheckerStatus,
    GetRequest,
    PutRequest,
    Transport,
)


class GeneratedChallengeChecker:
    metadata = CheckerMetadata(
        name="generated-challenge",
        service_name="example-vuln",
        version="1.0.0",
        transport=Transport.HTTP,
        default_port=8080,
        timeout_seconds=5.0,
    )

    def put(self, request: PutRequest) -> CheckerOutcome:
        try:
            body = json.dumps({"flag": request.flag}).encode()
            req = urllib.request.Request(
                self._url(request, "/internal/plant"),
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Plant-Token": request.context.credentials.require("plant_token"),
                },
            )
            with urllib.request.urlopen(req, timeout=request.context.timeout_seconds) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
            if '"planted"' not in payload:
                return CheckerOutcome(CheckerStatus.MUMBLE, "plant endpoint response was malformed")
            return CheckerOutcome(CheckerStatus.UP, "flag planted through generated challenge endpoint")
        except urllib.error.HTTPError as exc:
            return CheckerOutcome(CheckerStatus.MUMBLE, f"flag plant returned HTTP {exc.code}")
        except Exception as exc:
            return CheckerOutcome(CheckerStatus.MUMBLE, f"flag plant failed: {type(exc).__name__}")

    def get(self, request: GetRequest) -> CheckerOutcome:
        health = self._health(request)
        if health.status is not CheckerStatus.UP:
            return health
        return CheckerOutcome(CheckerStatus.UP, "generated challenge is healthy after flag plant")

    def check(self, request: CheckRequest) -> CheckerOutcome:
        return self._health(request)

    def _health(self, request: PutRequest | GetRequest | CheckRequest) -> CheckerOutcome:
        try:
            with urllib.request.urlopen(self._url(request, "/health"), timeout=request.context.timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200 or "ok" not in body.lower():
                return CheckerOutcome(CheckerStatus.MUMBLE, f"health returned HTTP {resp.status}")
            return CheckerOutcome(CheckerStatus.UP, "generated challenge health check passed")
        except urllib.error.HTTPError as exc:
            return CheckerOutcome(CheckerStatus.MUMBLE, f"health returned HTTP {exc.code}")
        except Exception as exc:
            return CheckerOutcome(CheckerStatus.MUMBLE, f"health failed: {type(exc).__name__}")

    @staticmethod
    def _url(request: PutRequest | GetRequest | CheckRequest, path: str) -> str:
        target = request.context.target
        return f"http://{target.host}:{target.port}{path}"


CHECKER = GeneratedChallengeChecker()
'''


def _install_generated_checker(challenge_path: Path) -> tuple[bool, str]:
    del challenge_path
    container = "sandcastle-gameserver"
    rc, out = _run(["docker", "inspect", container], timeout=10)
    if rc != 0:
        return False, "sandcastle-gameserver is not running; start the arena before deploying a challenge"

    checker_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="sandcastle_generated_checker_", delete=False
        ) as handle:
            handle.write(_generated_checker_wrapper())
            checker_path = handle.name
        rc, out = _run(
            ["docker", "exec", container, "mkdir", "-p", "/app/services/example-vuln"],
            timeout=10,
        )
        if rc != 0:
            return False, out
        rc, out = _run(
            ["docker", "cp", checker_path, f"{container}:/app/services/example-vuln/checker.py"],
            timeout=30,
        )
        if rc != 0:
            return False, out
    finally:
        if checker_path:
            Path(checker_path).unlink(missing_ok=True)
    return True, "generated challenge checker installed"


def _latest_published_challenge() -> dict[str, Any] | None:
    return next(
        (row for row in CHALLENGE_STORE.list(limit=100) if row.get("status") == "published"),
        None,
    )


def _latest_deployed_challenge() -> dict[str, Any] | None:
    return next((row for row in CHALLENGE_STORE.list(limit=100) if row.get("deployed_at")), None)


def _deploy_challenge_run(row: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    challenge_id = row.get("challenge_id")
    if not challenge_id:
        return False, "no challenge_id - publish must complete first", row
    ok, output = _deploy_challenge_to_arena(str(challenge_id))
    if ok:
        CHALLENGE_STORE.update(str(row["id"]), deployed_at=_now())
        row = CHALLENGE_STORE.get(str(row["id"]))
    return ok, output, row


def _ensure_latest_challenge_deployed() -> tuple[bool, str, dict[str, Any] | None, bool]:
    latest_published = _latest_published_challenge()
    if latest_published is None:
        return True, "No published challenge exists; keeping the current arena service.", None, False

    latest_deployed = _latest_deployed_challenge()
    if latest_deployed and latest_deployed.get("id") == latest_published.get("id"):
        return (
            True,
            f"Latest challenge already deployed: {latest_published.get('challenge_id')}",
            latest_published,
            False,
        )

    ok, output, deployed = _deploy_challenge_run(latest_published)
    return ok, output, deployed, ok


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
    try:
        provider = ModelProvider(str(row["provider"] or ARENA_DEFAULTS.agent_provider))
    except ValueError:
        provider = ModelProvider.FAKE
    return PlanningIdentity(
        deployment_id=credential.deployment_id,
        team_id=credential.team_id,
        allowed_targets=frozenset(candidates),
        allowed_actions=allowed_actions,
        agent_id=agent_id,
        run_id=run_id,
        agent_type=agent_type,
        provider=provider,
        model_id=str(row["model_id"] or ""),
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


def _assignment_payload(row: sqlite3.Row) -> dict[str, Any]:
    config = json.loads(row["config_json"])
    latest = next(
        (
            _deployment_payload(dep)
            for dep in STORE.list_by_agent_type(
                "attack_defense" if row["assignment_kind"] == "attack_defense" else "scripted"
            )
            if int(dep["team_id"]) == int(row["team_id"])
        ),
        None,
    )
    return {
        "team_id": int(row["team_id"]),
        "assignment_kind": str(row["assignment_kind"]),
        "config": config,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest_deployment": latest,
    }


def _match_plan_payload() -> dict[str, Any]:
    rows = MATCH_PLAN.list()
    latest_deployed = _latest_deployed_challenge()
    latest_published = _latest_published_challenge()
    return {
        "assignments": [_assignment_payload(row) for row in rows],
        "deployed_challenge": _challenge_payload(latest_deployed) if latest_deployed else None,
        "latest_published_challenge": _challenge_payload(latest_published) if latest_published else None,
        "instructions": [
            "Generate a challenge in Challenge Lab.",
            "Start the match from Match controls. The newest published challenge is copied into every team service workspace and the arena is restarted before round 1.",
            "Assign scripted bots or AI attack/defense agents to teams.",
            "Queued bots and AI attack/defense agents launch from the same Match controls flow.",
        ],
    }


def _assignment_config(body: dict[str, Any], assignment_kind: str) -> dict[str, Any]:
    if assignment_kind == "attack_defense":
        provider = ModelProvider(str(body.get("provider", "fake")))
        model_id = _normalize_model_id(provider, str(body.get("model_id") or ""))
        body = {
            **body,
            "planner": "model",
            "bot_name": str(body.get("bot_name", "AttackDefenseAgent")),
            "provider": provider.value,
            "model_id": model_id,
            "actions": body.get(
                "actions",
                [
                    "attack.recon",
                    "attack.exploit",
                    "defend.inspect_files",
                    "defend.run_checker",
                    "defend.apply_patch",
                    "defend.run_exploit_regression",
                ],
            ),
            "target_policy": body.get("target_policy", "all_opponents"),
            "loop_interval": body.get("loop_interval", 30),
            "stop_on_success": body.get("stop_on_success", False),
            "timeout": body.get("timeout", 10),
        }
    else:
        body = {
            **body,
            "planner": str(body.get("planner", "recon_first")),
            "bot_name": str(body.get("bot_name", "Scripted bot")),
            "actions": body.get("actions", ["recon.health"]),
            "target_policy": body.get("target_policy", "all_opponents"),
        }
    config = _public_config(body)
    if "provider" in body:
        config["provider"] = str(body.get("provider") or "fake")[:80]
    if "model_id" in body:
        config["model_id"] = str(body.get("model_id") or "")[:120]
    return config


def _start_match_plan() -> tuple[bool, list[dict[str, Any]], str]:
    rows = MATCH_PLAN.list()
    deployments: list[dict[str, Any]] = []
    outputs: list[str] = []
    ok = True
    for row in rows:
        config = json.loads(row["config_json"])
        deployment, output = _deploy_one(int(row["team_id"]), config)
        deployments.append(deployment)
        if output:
            outputs.append(output)
        ok = ok and deployment["status"] == "RUNNING"
    return ok, deployments, "\n\n".join(outputs)


def _prepare_match_plan(
    *, deploy_latest_challenge: bool = True, start_agents: bool = True
) -> tuple[bool, list[dict[str, Any]], str, dict[str, Any] | None, bool]:
    outputs: list[str] = []
    deployed_challenge: dict[str, Any] | None = None
    challenge_deployed = False

    if deploy_latest_challenge:
        ok, output, challenge_row, challenge_deployed = _ensure_latest_challenge_deployed()
        if output:
            outputs.append(f"challenge:\n{output}")
        if challenge_row is not None:
            deployed_challenge = _challenge_payload(challenge_row)
        if not ok:
            return False, [], "\n\n".join(outputs), deployed_challenge, challenge_deployed

    deployments: list[dict[str, Any]] = []
    if start_agents:
        ok, deployments, output = _start_match_plan()
        if output:
            outputs.append(f"agents:\n{output}")
        if not ok:
            return False, deployments, "\n\n".join(outputs), deployed_challenge, challenge_deployed

    return True, deployments, "\n\n".join(outputs), deployed_challenge, challenge_deployed


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
    provider_str = (
        str(public_config.get("provider") or ARENA_DEFAULTS.agent_provider)
        if is_model_agent
        else "scripted"
    )
    model_id_str = (
        str(public_config.get("model_id") or ARENA_DEFAULTS.agent_model or "fake-v1")
        if is_model_agent
        else ""
    )

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
            env["DEFENSE_TOKEN"] = plan_token
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
        {"id": "fake", "label": "Fake (no cost, offline)", "available": True, "models": ["fake-v1"]},
        {
            "id": "openai",
            "label": "OpenAI",
            "available": bool(os.environ.get("OPENAI_API_KEY")),
            "models": list(PROVIDER_MODELS[ModelProvider.OPENAI]),
        },
        {
            "id": "gemini",
            "label": "Google Gemini",
            "available": bool(os.environ.get("GEMINI_API_KEY")),
            "models": list(PROVIDER_MODELS[ModelProvider.GEMINI]),
        },
    ]
    return providers


def _provider_error(provider: str, model_id: str | None = None) -> str | None:
    try:
        selected = ModelProvider(provider)
    except ValueError:
        return f"unsupported provider: {provider}"
    try:
        _normalize_model_id(selected, model_id)
    except ValueError as exc:
        return str(exc)
    if selected is ModelProvider.FAKE:
        return None
    if selected is ModelProvider.OPENAI and not os.environ.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY is not configured in the bot-controller environment"
    if selected is ModelProvider.GEMINI and not os.environ.get("GEMINI_API_KEY"):
        return "GEMINI_API_KEY is not configured in the bot-controller environment"
    if selected is ModelProvider.OLLAMA:
        return "Ollama provider is not implemented"
    return None


def _agent_log_markdown(run_id: str, limit: int = 100, title: str = "Agent Log") -> str:
    """Render agent memory entries as a clean Markdown thinking log."""
    entries = AGENT_MEMORY.recent_as_dicts(run_id, limit=limit)
    if not entries:
        return f"# {title}\n\n*No entries yet for run `{run_id}`.*\n"

    lines: list[str] = [f"# {title} — `{run_id}`\n"]
    icon_map = {
        "tool_call": "⚙️",
        "tool_result": "✅",
        "error": "❌",
        "run_started": "🚀",
        "run_stopped": "🛑",
        "observation": "👁️",
        "model_request": "📡",
        "plan": "🧠",
        "artifact": "📦",
    }

    for e in entries:
        kind = e.get("kind", "event")
        icon = icon_map.get(kind, "📝")
        ts = e.get("created_at", "")[:19].replace("T", " ")
        summary = e.get("summary", "")
        data = e.get("data") or {}

        tool_id = data.get("tool_id") or data.get("action_id") or data.get("event_type") or ""
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
                tree = display_data.pop("tree", "")
                json_str = json.dumps(display_data, indent=2, ensure_ascii=False)
                lines.append(f"```json\n{json_str[:2000]}\n```\n")
                if tree:
                    lines.append("**Created file tree:**\n")
                    lines.append(f"```text\n{str(tree)[:4000]}\n```\n")
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
            planning_service = _build_planning_service(
                AGENT_MEMORY,
                provider=identity.provider,
                model_id=identity.model_id,
            )
            response = planning_service.plan(identity, payload)
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
            self._json(200, {"challenges": [_challenge_payload(r) for r in rows]})
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
                md = _agent_log_markdown(run_id, limit=limit, title="Challenge Generator Log")
                data = md.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
                return
            # GET /challenges/<id> — detail (no deploy on GET)
            self._json(200, {"challenge": _challenge_payload(row)})
            return

        if path == "/match-plan":
            self._json(200, _match_plan_payload())
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
                md = _agent_log_markdown(run_id_key, limit=limit, title="Agent Log")
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
                provider = ModelProvider(str(body.get("provider", "fake"))).value
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            requested_model = str(body.get("model_id") or "")
            provider_error = _provider_error(provider, requested_model)
            if provider_error:
                self._json(409, {"error": provider_error})
                return
            normalized_model = _normalize_model_id(ModelProvider(provider), requested_model)
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
                provider=provider,
                model_id=normalized_model,
                spec_json=_json.dumps(spec_dict),
            )
            t = threading.Thread(
                target=_generate_challenge_bg,
                args=(run_id, vuln, diff, seed, decoy, attempts, provider, normalized_model),
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
            ok, output, row = _deploy_challenge_run(row)
            self._json(
                200 if ok else 500,
                {
                    "ok": ok,
                    "challenge_id": challenge_id,
                    "output": output[-4000:],  # cap to avoid huge responses
                    "challenge": _challenge_payload(row) if row else None,
                },
            )
            return

        if path == "/match-plan/agents":
            try:
                teams = _validate_teams(body.get("teams"))
                assignment_kind = str(body.get("assignment_kind", body.get("kind", "attack_defense")))
                if assignment_kind not in {"attack_defense", "scripted"}:
                    raise ValueError("assignment_kind must be attack_defense or scripted")
                if assignment_kind == "attack_defense":
                    provider_error = _provider_error(
                        str(body.get("provider", "fake")),
                        str(body.get("model_id") or ""),
                    )
                    if provider_error:
                        self._json(409, {"error": provider_error})
                        return
                config = _assignment_config(body, assignment_kind)
                for team_id in teams:
                    MATCH_PLAN.upsert(team_id, assignment_kind, config)
            except (KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True, **_match_plan_payload()})
            return

        if path == "/match-plan/agents/clear":
            try:
                raw_teams = body.get("teams")
                teams = _validate_teams(raw_teams) if raw_teams is not None else None
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            deleted = MATCH_PLAN.delete(teams)
            self._json(200, {"ok": True, "deleted": deleted, **_match_plan_payload()})
            return

        if path == "/match-plan/start-agents":
            ok, deployments, output = _start_match_plan()
            self._json(
                200 if ok else 500,
                {
                    "ok": ok,
                    "deployments": deployments,
                    "output": output[-4000:],
                    **_match_plan_payload(),
                },
            )
            return

        if path == "/match-plan/prepare":
            ok, deployments, output, challenge, challenge_deployed = _prepare_match_plan(
                deploy_latest_challenge=bool(body.get("deploy_latest_challenge", True)),
                start_agents=bool(body.get("start_agents", True)),
            )
            self._json(
                200 if ok else 500,
                {
                    "ok": ok,
                    "deployments": deployments,
                    "challenge": challenge,
                    "challenge_deployed": challenge_deployed,
                    "output": output[-4000:],
                    **_match_plan_payload(),
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
