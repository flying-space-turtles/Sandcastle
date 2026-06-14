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

TEAM_ID = int(os.environ["TEAM_ID"])
TEAM_NAME = f"team{TEAM_ID}"
APP_CONTAINER = f"{TEAM_NAME}-vuln-app"
DOCKER_HOST = os.environ.get("DOCKER_HOST", "")
if DOCKER_HOST.startswith("unix://"):
    DOCKER_SOCK = DOCKER_HOST.removeprefix("unix://")
else:
    DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# IP of own SSH container on ctf-network: 10.10.{TEAM_ID}.2
_ALLOWED_IP = f"10.10.{TEAM_ID}.2"

PORT = int(os.environ.get("SERVICE_CONTROL_PORT", "7979"))

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

    def _check_source(self) -> bool:
        client_ip = self.client_address[0]
        if client_ip != _ALLOWED_IP:
            log.warning("rejected request from %s (allowed: %s)", client_ip, _ALLOWED_IP)
            self._send_json(403, {"error": "forbidden", "allowed": _ALLOWED_IP})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            self._send_json(200, {"ok": True, "team": TEAM_NAME})
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
        if not self._check_source():
            return

        if self.path == "/service/restart":
            ok = _docker_post(f"/containers/{APP_CONTAINER}/restart")
            if ok:
                log.info("restarted %s", APP_CONTAINER)
                self._send_json(200, {"restarted": True, "container": APP_CONTAINER})
            else:
                log.error("failed to restart %s", APP_CONTAINER)
                self._send_json(500, {"restarted": False, "container": APP_CONTAINER,
                                      "error": "docker restart failed"})
            return

        self._send_json(404, {"error": "not found"})


# ── Entrypoint ────────────────────────────────────────────────────────────────


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    log.info("listening on 0.0.0.0:%d  team=%s  app=%s  allowed_ip=%s",
             PORT, TEAM_NAME, APP_CONTAINER, _ALLOWED_IP)

    def _stop(signum: int, frame: object) -> None:
        log.info("shutting down")
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    server.serve_forever()


if __name__ == "__main__":
    main()
