#!/usr/bin/env python3
"""Local deployment controller for Sandcastle team bots."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bot_lib import ARENA_DEFAULTS, action_catalog, planner_catalog

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO_ROOT / "bot" / "deploy.sh"
ARENA_CONFIG = Path(os.environ.get("ARENA_CONFIG_FILE", REPO_ROOT / "config" / "arena.env"))
DATABASE = Path(os.environ.get("BOT_CONTROLLER_DB", REPO_ROOT / ".sandcastle" / "bot-controller.db"))
ALLOWED_ORIGINS = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
ACTIVE_STATUSES = ("DEPLOYING", "RUNNING")


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
            flagged.append({
                "container": c["name"],
                "type": "resource.oom_kill",
                "restart_count": c["restart_count"],
                "mem_limit_bytes": c["mem_limit_bytes"],
            })
        elif c["restart_count"] >= _RESTART_LOOP_THRESHOLD:
            flagged.append({
                "container": c["name"],
                "type": "resource.restart_loop",
                "restart_count": c["restart_count"],
            })
        if (c["disk_used_pct"] is not None
                and c["disk_used_pct"] >= _DISK_PRESSURE_THRESHOLD_PCT):
            flagged.append({
                "container": c["name"],
                "type": "resource.disk_pressure",
                "disk_used_pct": c["disk_used_pct"],
                "disk_available_bytes": c["disk_available_bytes"],
            })
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
                    archived_events TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS deployments_team_created "
                "ON deployments(team_id, created_at DESC)"
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def insert(self, deployment_id: str, team_id: int, config: dict[str, Any]) -> None:
        timestamp = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deployments (
                    id, team_id, bot_name, status, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'DEPLOYING', ?, ?, ?)
                """,
                (
                    deployment_id,
                    team_id,
                    str(config.get("bot_name", "Bot")),
                    json.dumps(config, sort_keys=True),
                    timestamp,
                    timestamp,
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
            return conn.execute(
                "SELECT * FROM deployments ORDER BY created_at DESC"
            ).fetchall()

    def active_for_team(self, team_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM deployments
                WHERE team_id = ? AND status IN ('DEPLOYING', 'RUNNING')
                ORDER BY created_at DESC
                """,
                (team_id,),
            ).fetchall()


STORE = DeploymentStore(DATABASE)


def _deployment_events(row: sqlite3.Row) -> list[dict[str, Any]]:
    raw = str(row["archived_events"] or "")
    if row["status"] in ACTIVE_STATUSES:
        raw = _container_file(
            int(row["team_id"]), f"{_deployment_dir(row['id'])}/events.jsonl"
        ) or raw
    return _parse_events(raw)


def _deployment_logs(row: sqlite3.Row, lines: int = 300) -> list[str]:
    raw = str(row["archived_log"] or "")
    if row["status"] in ACTIVE_STATUSES:
        raw = _container_file(
            int(row["team_id"]), f"{_deployment_dir(row['id'])}/bot.log", lines
        ) or raw
    return raw.splitlines()[-lines:]


def _archive(row: sqlite3.Row) -> None:
    events = _container_file(
        int(row["team_id"]), f"{_deployment_dir(row['id'])}/events.jsonl"
    )
    log = _container_file(
        int(row["team_id"]), f"{_deployment_dir(row['id'])}/bot.log"
    )
    STORE.update(
        str(row["id"]),
        archived_events=events or str(row["archived_events"] or ""),
        archived_log=log or str(row["archived_log"] or ""),
    )


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    captures = sum(event.get("type") == "flag.captured" for event in events)
    submissions = [
        event for event in events if event.get("type") == "submission.completed"
    ]
    accepted = sum(bool(event.get("accepted")) for event in submissions)
    failures = sum(
        event.get("type") in {"round.failed", "deployment.failed"} for event in events
    )
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
        next_status = "RUNNING" if running else ("DEPLOYING" if status == "DEPLOYING" else "STOPPED")
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
    if any(
        team < 1 or team > ARENA_DEFAULTS.team_count
        for team in config["target_teams"]
    ):
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
    return rc == 0, output


def _deploy_one(team_id: int, public_config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    for active in STORE.active_for_team(team_id):
        _stop_deployment(active, "SUPERSEDED")

    deployment_id = uuid.uuid4().hex[:12]
    STORE.insert(deployment_id, team_id, public_config)
    runtime_config = {
        **public_config,
        "deployment_id": deployment_id,
        "gameserver_url": _gameserver_url(),
        "submission_token": _team_token(team_id),
    }
    runtime_config.pop("loop_interval", None)
    runtime_config.pop("watchdog", None)

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
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    row = STORE.get(deployment_id)
    assert row is not None
    return _deployment_payload(row, include_config=True), output


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
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
