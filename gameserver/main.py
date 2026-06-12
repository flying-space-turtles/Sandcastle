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
  - GET  /rounds/current → Read the latest persisted round
  - POST /match/pause → Pause automatic round creation
  - POST /match/resume → Resume automatic round creation
  - POST /rounds/step → Run one round while paused
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

import db
from models import Match, MatchState, Service, Team, validate_state_transition
from tick_engine import OperatorStateError, RoundEngineError, TickEngine, build_tick_engine


ALLOWED_ORIGINS = re.compile(r"^https?://localhost(:\d+)?$")


class GameserverAPIHandler(BaseHTTPRequestHandler):
    tick_engine: TickEngine | None = None

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

        # ── GET /rounds/current ──
        if path == "/rounds/current" or path == "/api/rounds/current":
            conn = None
            try:
                conn = db.get_db_connection()
                row = conn.execute(
                    """
                    SELECT id, match_id, round_number, status, started_at,
                           deadline_at, completed_at, duration_seconds, error
                    FROM rounds WHERE match_id = 1
                    ORDER BY round_number DESC LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    self._json(404, {"error": "no rounds have started"})
                else:
                    self._json(200, {
                        "id": row[0],
                        "match_id": row[1],
                        "round_number": row[2],
                        "status": row[3],
                        "started_at": row[4],
                        "deadline_at": row[5],
                        "completed_at": row[6],
                        "duration_seconds": row[7],
                        "error": row[8],
                    })
            except Exception as exc:
                self._json(500, {"error": f"Database error: {str(exc)}"})
            finally:
                if conn:
                    conn.close()
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # ── POST /match/state, /match/pause, /match/resume ──
        if path in {
            "/match/state",
            "/api/match/state",
            "/match/pause",
            "/api/match/pause",
            "/match/resume",
            "/api/match/resume",
        }:
            body = self._read_body()
            if path.endswith("/pause"):
                target_status = MatchState.PAUSED.value
            elif path.endswith("/resume"):
                target_status = MatchState.RUNNING.value
            else:
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

        # ── POST /rounds/step ──
        if path == "/rounds/step" or path == "/api/rounds/step":
            if self.tick_engine is None:
                self._json(503, {"error": "round engine is not configured"})
                return
            try:
                record = self.tick_engine.single_step()
                self._json(200, {"round": record.as_dict()})
            except OperatorStateError as exc:
                self._json(409, {"error": str(exc)})
            except RoundEngineError as exc:
                self._json(500, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - HTTP boundary
                self._json(500, {"error": f"round step failed: {type(exc).__name__}"})
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

    # 3. Start the persisted round scheduler.
    try:
        tick_engine = build_tick_engine()
    except Exception as e:
        print(f"[!] Round engine initialization failed: {e}", file=sys.stderr)
        sys.exit(1)
    GameserverAPIHandler.tick_engine = tick_engine
    scheduler_stop = threading.Event()
    scheduler = threading.Thread(
        target=tick_engine.run_forever,
        args=(scheduler_stop,),
        name="round-scheduler",
        daemon=True,
    )
    scheduler.start()

    # 4. Start HTTPServer
    server = HTTPServer((args.host, args.port), GameserverAPIHandler)
    print(f"[*] Gameserver API listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down gameserver")
    finally:
        scheduler_stop.set()
        scheduler.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
