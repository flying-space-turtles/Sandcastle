#!/usr/bin/env python3
"""
Sandcastle team-local service-control API (SC-013).

Runs inside teamN-vuln where a team-scoped Docker socket is available.
Accepts requests ONLY from the team's own SSH container (10.10.N.2).

Endpoints:
  GET  /ping             — liveness probe (used by capability discovery)
  GET  /service/health   — JSON {"running": bool, "container": "teamN-vuln-app"}
  POST /service/restart  — restart teamN-vuln-app, JSON {"restarted": true}
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import signal
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import request as urllib_request

sys.path.insert(0, "/usr/local/lib/sandcastle-bot")

from bot_lib.defensive_patch import DefensivePatchWorkflow  # noqa: E402
from bot_lib.defensive_tools import (  # noqa: E402
    DefensiveToolError,
    SourceSnapshot,
    create_snapshot,
    list_allowed_files,
    read_file_range,
    restore_snapshot,
    run_checker,
    run_own_exploit,
    search_source,
)

TEAM_ID = int(os.environ["TEAM_ID"])
TEAM_NAME = f"team{TEAM_ID}"
TEAM_USER = os.environ.get("TEAM_USER", TEAM_NAME)
APP_CONTAINER = f"{TEAM_NAME}-vuln-app"
DOCKER_HOST = os.environ.get("DOCKER_HOST", "")
if DOCKER_HOST.startswith("unix://"):
    DOCKER_SOCK = DOCKER_HOST.removeprefix("unix://")
else:
    DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# IP of own SSH container on ctf-network: 10.10.{TEAM_ID}.2
_ALLOWED_IP = f"10.10.{TEAM_ID}.2"

PORT = int(os.environ.get("SERVICE_CONTROL_PORT", "7979"))
SERVICE_PORT = int(os.environ.get("ARENA_SERVICE_PORT", os.environ.get("SERVICE_PORT", "8080")))
SERVICE_ROOT = Path(os.environ.get("SERVICE_ROOT", f"/home/{TEAM_USER}/example-vuln")).resolve()
SNAPSHOTS_ROOT = Path(os.environ.get("DEFENSE_SNAPSHOTS_ROOT", "/tmp/sandcastle-defense-snapshots"))
TOKEN_FILE = Path(os.environ.get("DEFENSE_TOKEN_FILE", "/run/sandcastle-defense-token"))
MAX_BODY_BYTES = 128_000

logging.basicConfig(
    level=logging.INFO,
    format=f"[service-control/{TEAM_NAME}] %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("service_control")


# ── Docker helpers (inline — no bot_lib dependency) ───────────────────────────


class _UnixConn(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost")
        self._sock_path = DOCKER_SOCK

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._sock_path)
        self.sock = s


def _docker_get(path: str) -> dict | None:
    try:
        conn = _UnixConn()
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read())
    except Exception:
        pass
    return None


def _docker_post(path: str) -> bool:
    try:
        conn = _UnixConn()
        conn.request("POST", path, headers={"Host": "localhost", "Content-Length": "0"})
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 300
    except Exception:
        return False


# ── Request handler ───────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        log.info(fmt, *args)

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content length"})
            return {}
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return {}
        if length <= 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "request body must be JSON"})
            return {}
        if not isinstance(body, dict):
            self._send_json(400, {"error": "request body must be an object"})
            return {}
        return body

    def _check_source(self) -> bool:
        client_ip = self.client_address[0]
        if client_ip != _ALLOWED_IP:
            log.warning("rejected request from %s (allowed: %s)", client_ip, _ALLOWED_IP)
            self._send_json(403, {"error": "forbidden", "allowed": _ALLOWED_IP})
            return False
        return True

    def _check_defense_auth(self) -> bool:
        if not self._check_source():
            return False
        expected = ""
        try:
            expected = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            expected = os.environ.get("DEFENSE_TOKEN", "").strip()
        supplied = self.headers.get("X-Sandcastle-Defense-Token", "")
        if not expected or not supplied or not supplied == expected:
            log.warning("rejected unauthenticated defense request from %s", self.client_address[0])
            self._send_json(401, {"error": "missing or invalid defense token"})
            return False
        return True

    @staticmethod
    def _safe_error(exc: Exception) -> dict:
        return {"error": str(exc)[:500], "type": type(exc).__name__}

    def _checker_result(self) -> dict:
        checker = SERVICE_ROOT / "checker.py"
        ok, output = run_checker(checker, "127.0.0.1", SERVICE_PORT)
        if not ok:
            try:
                with urllib_request.urlopen(
                    f"http://127.0.0.1:{SERVICE_PORT}/health",
                    timeout=5,
                ) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                ok = resp.status == 200 and "ok" in raw.lower()
                output = f"health fallback: HTTP {resp.status} {raw[:200]}"
            except Exception as exc:  # noqa: BLE001
                output = f"{output}\nhealth fallback failed: {exc}"
        return {"passed": ok, "summary": output[:1000]}

    def _exploit_regression(self) -> dict:
        exploit_paths = sorted((SERVICE_ROOT / "exploits").glob("*.py"))
        outputs = []
        exploit_succeeded = False
        for exploit in exploit_paths:
            success, output = run_own_exploit(exploit, "127.0.0.1", SERVICE_PORT)
            outputs.append({"exploit": exploit.name, "succeeded": success, "output": output[:500]})
            exploit_succeeded = exploit_succeeded or success
        return {
            "exploit_blocked": bool(exploit_paths) and not exploit_succeeded,
            "summary": "no exploits found"
            if not exploit_paths
            else f"{len(exploit_paths)} exploit(s) run",
            "results": outputs,
        }

    def _reference_patch(self, vulnerability: str = "auto") -> str:
        patch_dir = SERVICE_ROOT / "patches"
        candidates: list[Path]
        if vulnerability and vulnerability != "auto":
            candidates = [patch_dir / f"patch_{vulnerability}.diff"]
        else:
            candidates = sorted(patch_dir.glob("patch_*.diff"))
        for candidate in candidates:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        raise DefensiveToolError("no patch diff provided and no reference patch exists")

    def _snapshot_from_id(self, snapshot_id: str | None = None) -> SourceSnapshot:
        SNAPSHOTS_ROOT.mkdir(parents=True, exist_ok=True)
        if snapshot_id:
            snapshot_dir = SNAPSHOTS_ROOT / snapshot_id
        else:
            candidates = [p for p in SNAPSHOTS_ROOT.iterdir() if p.is_dir()]
            if not candidates:
                raise DefensiveToolError("no source snapshot exists")
            snapshot_dir = max(candidates, key=lambda path: path.stat().st_mtime)
            snapshot_id = snapshot_dir.name
        if not snapshot_dir.exists():
            raise DefensiveToolError(f"snapshot not found: {snapshot_id}")
        files = [p for p in snapshot_dir.rglob("*") if p.is_file()]
        return SourceSnapshot(
            snapshot_id=str(snapshot_id),
            service_root=SERVICE_ROOT,
            snapshot_dir=snapshot_dir,
            file_count=len(files),
            digest=str(snapshot_id),
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            self._send_json(200, {"ok": True, "team": TEAM_NAME})
            return

        if self.path == "/defense/files":
            if not self._check_defense_auth():
                return
            try:
                self._send_json(200, {"files": list_allowed_files(SERVICE_ROOT)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(400, self._safe_error(exc))
            return

        if not self._check_source():
            return

        if self.path == "/service/health":
            data = _docker_get(f"/containers/{APP_CONTAINER}/json")
            running = bool(data and data.get("State", {}).get("Running", False))
            self._send_json(200, {"running": running, "container": APP_CONTAINER})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/defense/"):
            if not self._check_defense_auth():
                return
            body = self._body()
            try:
                if self.path == "/defense/read":
                    path = str(body.get("path", ""))
                    content = read_file_range(
                        SERVICE_ROOT,
                        path,
                        int(body.get("start_line", 1)),
                        int(body["end_line"]) if body.get("end_line") is not None else None,
                    )
                    self._send_json(200, {"path": path, "content": content})
                    return
                if self.path == "/defense/search":
                    matches = search_source(
                        SERVICE_ROOT,
                        str(body.get("pattern", "")),
                        literal=bool(body.get("literal", True)),
                    )
                    self._send_json(200, {"matches": matches})
                    return
                if self.path == "/defense/snapshot":
                    snapshot = create_snapshot(SERVICE_ROOT, SNAPSHOTS_ROOT)
                    self._send_json(200, snapshot.as_dict())
                    return
                if self.path == "/defense/checker":
                    self._send_json(200, self._checker_result())
                    return
                if self.path == "/defense/exploit-regression":
                    self._send_json(200, self._exploit_regression())
                    return
                if self.path == "/defense/rollback":
                    snapshot = self._snapshot_from_id(
                        str(body["snapshot_id"]) if body.get("snapshot_id") else None
                    )
                    restore_snapshot(snapshot)
                    self._send_json(
                        200,
                        {"restored": True, "snapshot_id": snapshot.snapshot_id},
                    )
                    return
                if self.path == "/defense/patch":
                    diff_text = str(body.get("diff") or "")
                    if not diff_text:
                        diff_text = self._reference_patch(str(body.get("vulnerability", "auto")))
                    workflow = DefensivePatchWorkflow(
                        service_root=SERVICE_ROOT,
                        snapshots_root=SNAPSHOTS_ROOT,
                        checker_path=SERVICE_ROOT / "checker.py",
                        exploit_paths=sorted((SERVICE_ROOT / "exploits").glob("*.py")),
                        compose_project=f"sandcastle-team{TEAM_ID}",
                        service_host="127.0.0.1",
                        service_port=SERVICE_PORT,
                        team_id=TEAM_ID,
                    )
                    tx = workflow.run(
                        str(body.get("correlation_id") or f"service-control-{TEAM_ID}"),
                        diff_text,
                    )
                    self._send_json(200 if tx.status == "committed" else 409, tx.as_dict())
                    return
            except Exception as exc:  # noqa: BLE001
                self._send_json(400, self._safe_error(exc))
                return
            self._send_json(404, {"error": "not found"})
            return

        if not self._check_source():
            return

        if self.path == "/service/restart":
            ok = _docker_post(f"/containers/{APP_CONTAINER}/restart")
            if ok:
                log.info("restarted %s", APP_CONTAINER)
                self._send_json(200, {"restarted": True, "container": APP_CONTAINER})
            else:
                log.error("failed to restart %s", APP_CONTAINER)
                self._send_json(
                    500,
                    {
                        "restarted": False,
                        "container": APP_CONTAINER,
                        "error": "docker restart failed",
                    },
                )
            return

        self._send_json(404, {"error": "not found"})


# ── Entrypoint ────────────────────────────────────────────────────────────────


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    log.info(
        "listening on 0.0.0.0:%d  team=%s  app=%s  allowed_ip=%s",
        PORT,
        TEAM_NAME,
        APP_CONTAINER,
        _ALLOWED_IP,
    )

    def _stop(signum: int, frame: object) -> None:
        log.info("shutting down")
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    server.serve_forever()


if __name__ == "__main__":
    main()
