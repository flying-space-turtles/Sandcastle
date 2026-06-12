#!/usr/bin/env python3
"""
main.py — Minimal gameserver HTTP API for the Sandcastle Attack & Defense CTF.
================================================================================

Exposes:
  - GET  /health      → Liveness and database connectivity checks
  - GET  /match       → Read-only current match status (and state info)
  - POST /match/state → Transition the match state (idempotent, validated)
  - GET  /teams       → List registered teams and details
  - GET  /services    → List registered services and details
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

import db
from models import Match, MatchState, Service, Team, validate_state_transition


ALLOWED_ORIGINS = re.compile(r"^https?://localhost(:\d+)?$")


class GameserverAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence default access logging to keep stdout clean
        pass

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if ALLOWED_ORIGINS.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")

    def _json(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
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
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # ── GET /health ──
        if path == "/health":
            db_ok = db.check_db_readiness()
            if db_ok:
                self._json(200, {"status": "UP", "database": "connected"})
            else:
                self._json(503, {"status": "DOWN", "database": "disconnected"})
            return

        # ── GET /match ──
        if path == "/match" or path == "/api/match":
            conn = None
            try:
                conn = db.get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT id, status, created_at, updated_at FROM matches WHERE id = 1;")
                row = cursor.fetchone()
                if row:
                    match_obj = Match.from_row(row)
                    self._json(200, {
                        "match_id": match_obj.id,
                        "status": match_obj.status.value,
                        "created_at": match_obj.created_at,
                        "updated_at": match_obj.updated_at
                    })
                else:
                    self._json(404, {"error": "match not found"})
            except Exception as e:
                self._json(500, {"error": f"Database error: {str(e)}"})
            finally:
                if conn:
                    conn.close()
            return

        # ── GET /teams ──
        if path == "/teams":
            conn = None
            try:
                conn = db.get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, token, ip_address FROM teams ORDER BY id ASC;")
                rows = cursor.fetchall()
                teams_list = [Match.from_row((0, "CREATED", "", "")) if False else {
                    "id": r[0], "name": r[1], "token": r[2], "ip_address": r[3]
                } for r in rows]
                self._json(200, {"teams": teams_list})
            except Exception as e:
                self._json(500, {"error": f"Database error: {str(e)}"})
            finally:
                if conn:
                    conn.close()
            return

        # ── GET /services ──
        if path == "/services":
            conn = None
            try:
                conn = db.get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, port FROM services ORDER BY id ASC;")
                rows = cursor.fetchall()
                services_list = [{"id": r[0], "name": r[1], "port": r[2]} for r in rows]
                self._json(200, {"services": services_list})
            except Exception as e:
                self._json(500, {"error": f"Database error: {str(e)}"})
            finally:
                if conn:
                    conn.close()
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # ── POST /match/state ──
        if path == "/match/state" or path == "/api/match/state":
            body = self._read_body()
            target_status = body.get("status")
            if not target_status:
                self._json(400, {"error": "status field is required"})
                return

            conn = None
            try:
                conn = db.get_db_connection()
                cursor = conn.cursor()

                # Get current match state
                cursor.execute("SELECT status FROM matches WHERE id = 1;")
                row = cursor.fetchone()
                if not row:
                    self._json(404, {"error": "Match with ID 1 not found."})
                    return

                current_status = row[0]

                # Validate transition (raises ValueError on failure)
                try:
                    new_state = validate_state_transition(current_status, target_status)
                except ValueError as err:
                    self._json(400, {"error": str(err)})
                    return

                # If transition is valid and not a no-op, update db
                if current_status != new_state.value:
                    cursor.execute(
                        "UPDATE matches SET status = ? WHERE id = 1;",
                        (new_state.value,)
                    )
                    conn.commit()

                # Return fresh status
                cursor.execute("SELECT id, status, created_at, updated_at FROM matches WHERE id = 1;")
                match_row = cursor.fetchone()
                match_obj = Match.from_row(match_row)
                self._json(200, {
                    "match_id": match_obj.id,
                    "status": match_obj.status.value,
                    "created_at": match_obj.created_at,
                    "updated_at": match_obj.updated_at
                })

            except Exception as e:
                self._json(500, {"error": f"Database error: {str(e)}"})
            finally:
                if conn:
                    conn.close()
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandcastle Gameserver API")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Interface to bind to (default: 0.0.0.0)"
    )
    args = parser.parse_args()

    # 1. Initialize DB schema
    print("[*] Initializing database...")
    try:
        conn = db.get_db_connection()
        db.initialize_schema(conn)
        conn.close()
    except Exception as e:
        print(f"[!] Database initialization failed: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Sync team/service registry with current configuration
    config_file = db.get_config_path()
    print(f"[*] Synchronizing registry from config path: {config_file}")
    try:
        conn = db.get_db_connection()
        db.sync_registry(conn, config_file)
        conn.close()
        print("[+] Registry synchronized successfully.")
    except Exception as e:
        print(f"[!] Registry synchronization failed: {e}", file=sys.stderr)
        # We don't fail-hard on registry sync in case file isn't mounted during tests/local startup

    # 3. Start HTTPServer
    server = HTTPServer((args.host, args.port), GameserverAPIHandler)
    print(f"[*] Gameserver API listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down gameserver")


if __name__ == "__main__":
    main()
