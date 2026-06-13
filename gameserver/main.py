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
  - POST /flags/submit → Authenticated flag submission
  - GET  /standings → Current deterministic standings
  - GET  /rounds/{number}/scores → Per-round score breakdown
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import db
from models import Match, MatchState, validate_state_transition
from scoring import (
    TIEBREAKER,
    get_scoring_policy,
    reconcile_score_events,
    standings_from_events,
)
from submissions import (
    SubmissionCode,
    TeamRateLimiter,
    authenticate_team,
    record_submission,
)
from tick_engine import OperatorStateError, RoundEngineError, TickEngine, build_tick_engine


ALLOWED_ORIGINS = re.compile(r"^https?://localhost(:\d+)?$")


class GameserverAPIHandler(BaseHTTPRequestHandler):
    tick_engine: TickEngine | None = None
    submission_rate_limiter = TeamRateLimiter(limit=60, window_seconds=60)
    max_request_body_bytes = 64 * 1024

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
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Vary", "Origin")

    def _json(
        self,
        code: int,
        body: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {}
        if length <= 0 or length > self.max_request_body_bytes:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
            return body if isinstance(body, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _bearer_token(self) -> str | None:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and token and " " not in token:
            return token
        return None

    def _scores(self, round_number: int | None = None) -> None:
        conn = None
        try:
            conn = db.get_db_connection()
            if round_number is not None:
                exists = conn.execute(
                    "SELECT 1 FROM rounds WHERE match_id = 1 AND round_number = ?",
                    (round_number,),
                ).fetchone()
                if exists is None:
                    self._json(
                        404,
                        {"code": "ROUND_NOT_FOUND", "round_number": round_number},
                    )
                    return
            reconcile_score_events(conn, match_id=1)
            policy = get_scoring_policy(conn, match_id=1)
            standings = standings_from_events(
                conn,
                match_id=1,
                round_number=round_number,
            )
            body: dict[str, object] = {
                "match_id": 1,
                "policy": policy.as_dict(),
                "tiebreaker": list(TIEBREAKER),
                "standings": standings,
            }
            if round_number is not None:
                body["round_number"] = round_number
            self._json(200, body)
        except Exception:  # noqa: BLE001 - do not expose persistence internals
            self._json(500, {"code": "SCORING_ERROR"})
        finally:
            if conn:
                conn.close()

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
                cursor.execute("SELECT id, name, ip_address FROM teams ORDER BY id ASC;")
                rows = cursor.fetchall()
                teams_list = [
                    {"id": row[0], "name": row[1], "ip_address": row[2]}
                    for row in rows
                ]
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

        # ── GET /standings ──
        if path == "/standings" or path == "/api/standings":
            self._scores()
            return

        # ── GET /rounds/{number}/scores ──
        round_scores_match = re.fullmatch(r"(?:/api)?/rounds/(\d+)/scores", path)
        if round_scores_match:
            self._scores(int(round_scores_match.group(1)))
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # ── POST /flags/submit ──
        if path == "/flags/submit" or path == "/api/flags/submit":
            body = self._read_body()
            team_id = body.get("team_id")
            token = self._bearer_token()
            try:
                authenticated = token is not None and authenticate_team(team_id, token)
            except Exception:  # noqa: BLE001 - do not disclose authentication storage errors
                self._json(
                    500,
                    {"code": SubmissionCode.INTERNAL_ERROR.value, "accepted": False},
                )
                return
            if not authenticated:
                self._json(
                    401,
                    {"code": SubmissionCode.UNAUTHORIZED.value, "accepted": False},
                    {"WWW-Authenticate": "Bearer"},
                )
                return

            rate_limit = self.submission_rate_limiter.check(team_id)
            if not rate_limit.allowed:
                self._json(
                    429,
                    {"code": SubmissionCode.RATE_LIMITED.value, "accepted": False},
                    {"Retry-After": str(rate_limit.retry_after_seconds)},
                )
                return

            try:
                result = record_submission(team_id, body.get("flag"))
            except Exception:  # noqa: BLE001 - keep database details out of API responses
                self._json(
                    500,
                    {"code": SubmissionCode.INTERNAL_ERROR.value, "accepted": False},
                )
                return

            status_by_code = {
                SubmissionCode.ACCEPTED: 201,
                SubmissionCode.DUPLICATE: 409,
                SubmissionCode.SELF_OWNED: 403,
                SubmissionCode.EXPIRED: 410,
                SubmissionCode.MALFORMED: 400,
                SubmissionCode.UNKNOWN: 404,
            }
            self._json(status_by_code[result.code], result.as_dict())
            return

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

    config = db.parse_arena_config(config_file)
    GameserverAPIHandler.submission_rate_limiter = TeamRateLimiter(
        limit=int(config.get("ARENA_SUBMISSION_RATE_LIMIT", "60")),
        window_seconds=int(config.get("ARENA_SUBMISSION_RATE_WINDOW_SECONDS", "60")),
    )

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

    # 4. Start HTTP server
    server = ThreadingHTTPServer((args.host, args.port), GameserverAPIHandler)
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
