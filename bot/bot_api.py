#!/usr/bin/env python3
"""
bot_api.py — Lightweight HTTP bridge between the Sandcastle visualizer and deploy.sh
======================================================================================

Listens on the host and port configured in config/arena.env. The visualizer's
Bot tab sends requests here to:
  - query bot status across all teams
  - deploy the bot to selected teams with a given configuration
  - stop the bot in selected teams
  - stream recent log output from a team

All endpoints accept and return JSON.  CORS headers are set so any localhost
origin (Vite dev server, file://, etc.) can reach this server.

Usage:
    python3 bot/bot_api.py              # use config/arena.env
    python3 bot/bot_api.py --port 9090  # custom port

Run this from the REPO ROOT (not from inside the bot/ directory) so that the
path to deploy.sh resolves correctly.  The visualizer README will note this.

Endpoints
---------
GET  /health                       → {"ok": true}
GET  /catalog                      → {"actions": [...], "planners": [...]}
GET  /status                       → {"teams": [{id, running, pid, container_up}, ...]}
GET  /arena                        → canonical bot-facing arena configuration
GET  /teams                        → {"num_teams": N}
GET  /logs/<team_id>               → {"lines": ["...", ...]}
POST /deploy   body: DeployRequest → {"ok": true, "output": "..."}
POST /stop     body: {teams:[N]}   → {"ok": true, "output": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bot_lib import ARENA_DEFAULTS, action_catalog, planner_catalog

# ── paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO_ROOT / "bot" / "deploy.sh"


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command, return (returncode, combined stdout+stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "command timed out"
    except Exception as exc:
        return -1, str(exc)


def _docker_inspect(container: str) -> dict | None:
    rc, out = _run(["docker", "inspect", "--format", "{{json .State}}", container])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _num_teams() -> int:
    """Return the configured topology size, including offline teams."""
    return ARENA_DEFAULTS.team_count


def _team_status(team_id: int) -> dict:
    cname = f"team{team_id}-ssh"
    state = _docker_inspect(cname)
    container_up = bool(state and state.get("Running"))

    running = False
    pid = None
    if container_up:
        rc, out = _run(["docker", "exec", cname, "pgrep", "-a", "-f", "/tmp/bot.py"])
        if rc == 0 and out.strip():
            running = True
            # pgrep -a output: "1234 python3 /tmp/bot.py ..."
            first_line = out.strip().splitlines()[0]
            pid_str = first_line.split()[0]
            if pid_str.isdigit():
                pid = int(pid_str)

    return {"id": team_id, "running": running, "pid": pid, "container_up": container_up}


def _get_logs(team_id: int, lines: int = 60) -> list[str]:
    cname = f"team{team_id}-ssh"
    rc, out = _run(["docker", "exec", cname, "tail", f"-n{lines}", "/tmp/bot.log"])
    if rc != 0:
        return []
    return out.splitlines()


# ── request handler ───────────────────────────────────────────────────────────

ALLOWED_ORIGINS = re.compile(r"^https?://localhost(:\d+)?$")


class BotAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
        pass

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if ALLOWED_ORIGINS.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "http://localhost:5173")
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

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ── OPTIONS (preflight) ────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            self._json(200, {"ok": True})
            return

        if path == "/catalog":
            self._json(200, {"actions": action_catalog(), "planners": planner_catalog()})
            return

        if path == "/arena":
            self._json(
                200,
                {
                    "num_teams": ARENA_DEFAULTS.team_count,
                    "service_port": ARENA_DEFAULTS.service_port,
                    "ip_pattern": ARENA_DEFAULTS.service_ip_pattern,
                    "ssh_base_port": ARENA_DEFAULTS.ssh_base_port,
                },
            )
            return

        if path == "/teams":
            n = _num_teams()
            self._json(200, {"num_teams": n})
            return

        if path == "/status":
            n = _num_teams()
            teams = [_team_status(i) for i in range(1, n + 1)]
            self._json(200, {"teams": teams})
            return

        m = re.match(r"^/logs/(\d+)$", path)
        if m:
            team_id = int(m.group(1))
            log_lines = _get_logs(team_id)
            self._json(200, {"team_id": team_id, "lines": log_lines})
            return

        self._json(404, {"error": "not found"})

    # ── POST ──────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/deploy":
            self._handle_deploy()
        elif path == "/stop":
            self._handle_stop()
        else:
            self._json(404, {"error": "not found"})

    def _handle_deploy(self) -> None:
        """
        Expected body (all optional except `teams`):
        {
          "teams": [2, 3, 4],
          "bot_name": "Sweep Bot",
          "planner": "scripted",
          "target_policy": "all_opponents",
          "target_teams": [],
          "actions": ["recon.health"],
          "loop_interval": 60,
          "watchdog": true,
          "num_teams": <from arena config>,
          "service_port": <from arena config>,
          "flag_re": "FLAG\\{[a-f0-9]{32}\\}",
          "ip_pattern": <from arena config>,
          "exploits": ["path_traversal", "cmdi", "sqli"],
          "stop_on_first": true,
          "timeout": 6
        }
        """
        body = self._read_body()
        teams: list[int] = [int(t) for t in body.get("teams", [])]
        if not teams:
            self._json(400, {"error": "teams list is required"})
            return

        # Build the config JSON that will be copied into the containers
        bot_config: dict = {}
        for key in (
            "bot_name",
            "planner",
            "target_policy",
            "target_teams",
            "actions",
            "flag_re",
            "exploits",
            "stop_on_first",
            "stop_on_success",
            "timeout",
        ):
            if key in body:
                bot_config[key] = body[key]
        bot_config["service_port"] = ARENA_DEFAULTS.service_port
        bot_config["ip_pattern"] = ARENA_DEFAULTS.service_ip_pattern

        # Write config to a temp file; deploy.sh copies it into the container
        tmp_cfg = None
        extra_args: list[str] = []
        if bot_config:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="bot_config_", delete=False
            )
            json.dump(bot_config, tmp)
            tmp.flush()
            tmp_cfg = tmp.name
            tmp.close()
            extra_args += ["--config", tmp_cfg]

        env = os.environ.copy()
        if "loop_interval" in body:
            env["LOOP_INTERVAL"] = str(int(body["loop_interval"]))
        if "watchdog" in body:
            env["WATCHDOG"] = "true" if body["watchdog"] else "false"
        cmd = ["bash", str(DEPLOY_SH)] + extra_args + [str(t) for t in teams]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(REPO_ROOT),
                env=env,
            )
            output = (result.stdout + result.stderr).strip()
            ok = result.returncode == 0
        except subprocess.TimeoutExpired:
            output = "deploy timed out after 60s"
            ok = False
        except Exception as exc:
            output = str(exc)
            ok = False
        finally:
            if tmp_cfg and os.path.exists(tmp_cfg):
                os.unlink(tmp_cfg)

        self._json(200 if ok else 500, {"ok": ok, "output": output})

    def _handle_stop(self) -> None:
        """Body: {"teams": [2, 3]} — stop bots in those teams."""
        body = self._read_body()
        teams: list[int] = [int(t) for t in body.get("teams", [])]
        if not teams:
            self._json(400, {"error": "teams list is required"})
            return

        cmd = ["bash", str(DEPLOY_SH), "--stop"] + [str(t) for t in teams]
        rc, output = _run(cmd)
        self._json(200, {"ok": rc == 0, "output": output})


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Sandcastle Bot API server")
    p.add_argument(
        "--port",
        type=int,
        default=ARENA_DEFAULTS.bot_api_port,
        metavar="PORT",
        help=f"port to listen on (default: {ARENA_DEFAULTS.bot_api_port})",
    )
    p.add_argument(
        "--host",
        type=str,
        default=ARENA_DEFAULTS.bot_api_host,
        metavar="HOST",
        help=f"interface to bind to (default: {ARENA_DEFAULTS.bot_api_host})",
    )
    args = p.parse_args()

    if not DEPLOY_SH.exists():
        print(f"[!] deploy.sh not found at {DEPLOY_SH}", file=sys.stderr)
        print("    Run bot_api.py from the repo root, or adjust REPO_ROOT.", file=sys.stderr)
        sys.exit(1)

    server = HTTPServer((args.host, args.port), BotAPIHandler)
    print(f"[*] Bot API listening on http://{args.host}:{args.port}")
    print(f"[*] Repo root: {REPO_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down")


if __name__ == "__main__":
    main()
