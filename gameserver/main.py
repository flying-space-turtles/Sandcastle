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
  - POST /match/start → Start a created match
  - POST /match/finish → Finish a running or paused match
  - POST /match/restart → Reset a finished or failed match to CREATED
  - POST /rounds/step → Run one round while paused
  - POST /flags/submit → Authenticated flag submission
  - GET  /standings → Current deterministic standings
  - GET  /rounds/{number}/scores → Per-round score breakdown
  - GET  /dashboard → Authoritative operator-console snapshot
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import db
import telemetry
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


def _emit_submission(result: Any, team_id: int) -> None:
    from submissions import SubmissionCode

    event = (
        telemetry.SUBMISSION_ACCEPTED
        if result.code is SubmissionCode.ACCEPTED
        else telemetry.SUBMISSION_REJECTED
    )
    telemetry.emit_safe(
        db.get_db_path(),
        event,
        "gameserver",
        team_id=team_id,
        payload={"code": result.code.value, "submission_id": result.submission_id},
    )


class GameserverAPIHandler(BaseHTTPRequestHandler):
    tick_engine: TickEngine | None = None
    operator_token = ""
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

    def _require_operator(self) -> bool:
        token = self._bearer_token()
        if (
            not self.operator_token
            or token is None
            or not hmac.compare_digest(token, self.operator_token)
        ):
            self._json(
                401,
                {
                    "code": "OPERATOR_UNAUTHORIZED",
                    "error": "operator credential required",
                },
                {"WWW-Authenticate": "Bearer"},
            )
            return False
        return True

    def _dashboard(self) -> None:
        conn = None
        try:
            conn = db.get_db_connection()
            match_row = conn.execute(
                """
                SELECT id, status, created_at, updated_at
                FROM matches WHERE id = 1
                """
            ).fetchone()
            if match_row is None:
                self._json(404, {"code": "MATCH_NOT_FOUND"})
                return

            round_row = conn.execute(
                """
                SELECT id, match_id, round_number, status, started_at,
                       deadline_at, completed_at, duration_seconds, error
                FROM rounds WHERE match_id = 1
                ORDER BY round_number DESC LIMIT 1
                """
            ).fetchone()
            round_number = int(round_row[2]) if round_row is not None else None

            reconcile_score_events(conn, match_id=1)
            policy = get_scoring_policy(conn, match_id=1)
            standings = standings_from_events(conn, match_id=1)
            service_rows = conn.execute(
                """
                SELECT
                    t.id, t.name, s.id, s.name, s.port,
                    cr.operation, cr.status, cr.message, cr.duration_ms,
                    cr.created_at
                FROM teams t
                CROSS JOIN services s
                LEFT JOIN checker_results cr
                  ON cr.team_id = t.id
                 AND cr.service_id = s.id
                 AND cr.match_id = 1
                 AND cr.round_number = ?
                ORDER BY t.id, s.id, cr.operation
                """,
                (round_number if round_number is not None else -1,),
            ).fetchall()

            services: dict[tuple[int, int], dict[str, object]] = {}
            for row in service_rows:
                key = (int(row[0]), int(row[2]))
                service = services.setdefault(
                    key,
                    {
                        "team_id": int(row[0]),
                        "team_name": str(row[1]),
                        "service_id": int(row[2]),
                        "service_name": str(row[3]),
                        "port": int(row[4]),
                        "round_number": round_number,
                        "status": "PENDING",
                        "operations": {},
                        "last_checked_at": None,
                    },
                )
                if row[5] is None:
                    continue
                operations = service["operations"]
                assert isinstance(operations, dict)
                operations[str(row[5])] = {
                    "status": str(row[6]),
                    "message": str(row[7]),
                    "duration_ms": int(row[8]),
                    "created_at": str(row[9]),
                }
                service["last_checked_at"] = str(row[9])

            status_priority = {"DOWN": 4, "CORRUPT": 3, "MUMBLE": 2, "UP": 1}
            for service in services.values():
                operations = service["operations"]
                assert isinstance(operations, dict)
                statuses = [
                    str(operation["status"])
                    for operation in operations.values()
                    if isinstance(operation, dict)
                ]
                if statuses:
                    service["status"] = max(
                        statuses,
                        key=lambda status: status_priority.get(status, 0),
                    )

            match_obj = Match.from_row(match_row)
            current_round = None
            if round_row is not None:
                current_round = {
                    "id": round_row[0],
                    "match_id": round_row[1],
                    "round_number": round_row[2],
                    "status": round_row[3],
                    "started_at": round_row[4],
                    "deadline_at": round_row[5],
                    "completed_at": round_row[6],
                    "duration_seconds": round_row[7],
                    "error": round_row[8],
                }
            self._json(
                200,
                {
                    "match": {
                        "match_id": match_obj.id,
                        "status": match_obj.status.value,
                        "created_at": match_obj.created_at,
                        "updated_at": match_obj.updated_at,
                    },
                    "round": current_round,
                    "policy": policy.as_dict(),
                    "standings": standings,
                    "services": list(services.values()),
                },
            )
        except Exception:  # noqa: BLE001 - do not expose persistence internals
            self._json(500, {"code": "DASHBOARD_ERROR"})
        finally:
            if conn:
                conn.close()

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
                cursor.execute(
                    "SELECT id, status, created_at, updated_at FROM matches WHERE id = 1;"
                )
                row = cursor.fetchone()
                if row:
                    match_obj = Match.from_row(row)
                    self._json(
                        200,
                        {
                            "match_id": match_obj.id,
                            "status": match_obj.status.value,
                            "created_at": match_obj.created_at,
                            "updated_at": match_obj.updated_at,
                        },
                    )
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
                teams_list = [{"id": row[0], "name": row[1], "ip_address": row[2]} for row in rows]
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
                    self._json(
                        200,
                        {
                            "id": row[0],
                            "match_id": row[1],
                            "round_number": row[2],
                            "status": row[3],
                            "started_at": row[4],
                            "deadline_at": row[5],
                            "completed_at": row[6],
                            "duration_seconds": row[7],
                            "error": row[8],
                        },
                    )
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

        # ── GET /dashboard ──
        if path == "/dashboard" or path == "/api/dashboard":
            self._dashboard()
            return

        # ── GET /rounds/{number}/scores ──
        round_scores_match = re.fullmatch(r"(?:/api)?/rounds/(\d+)/scores", path)
        if round_scores_match:
            self._scores(int(round_scores_match.group(1)))
            return

        # ── GET /telemetry/export?match_id=N ──
        if path in {"/telemetry/export", "/api/telemetry/export"}:
            if not self._require_operator():
                return
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query)
            match_id_strs = qs.get("match_id", [])
            if not match_id_strs or not match_id_strs[0].isdigit():
                self._json(400, {"error": "match_id query param required"})
                return
            match_id = int(match_id_strs[0])
            conn = None
            try:
                conn = db.get_db_connection()
                events = telemetry.export_match(conn, match_id)
                self._json(
                    200, {"match_id": match_id, "event_count": len(events), "events": events}
                )
            except Exception:
                self._json(500, {"error": "export failed"})
            finally:
                if conn:
                    conn.close()
            return

        # ── GET /telemetry/metrics?match_id=N ──
        if path in {"/telemetry/metrics", "/api/telemetry/metrics"}:
            if not self._require_operator():
                return
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query)
            match_id_strs = qs.get("match_id", [])
            if not match_id_strs or not match_id_strs[0].isdigit():
                self._json(400, {"error": "match_id query param required"})
                return
            match_id = int(match_id_strs[0])
            conn = None
            try:
                conn = db.get_db_connection()
                metrics = telemetry.compute_metrics(conn, match_id)
                self._json(200, metrics)
            except Exception:
                self._json(500, {"error": "metrics failed"})
            finally:
                if conn:
                    conn.close()
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
            _emit_submission(result, team_id)
            self._json(status_by_code[result.code], result.as_dict())
            return

        # ── POST /match/state, /match/pause, /match/resume ──
        if path in {"/match/restart", "/api/match/restart"}:
            if not self._require_operator():
                return
            conn = None
            try:
                if self.tick_engine is not None:
                    match_row = self.tick_engine.restart_match()
                else:
                    conn = db.get_db_connection()
                    match_row = db.restart_match(conn)
                self.submission_rate_limiter.reset()
                match_obj = Match.from_row(match_row)
                self._json(
                    200,
                    {
                        "match_id": match_obj.id,
                        "status": match_obj.status.value,
                        "created_at": match_obj.created_at,
                        "updated_at": match_obj.updated_at,
                    },
                )
            except LookupError as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:
                self._json(409, {"error": str(exc)})
            except Exception:  # noqa: BLE001 - do not expose persistence internals
                self._json(500, {"error": "match restart failed"})
            finally:
                if conn:
                    conn.close()
            return

        if path in {
            "/match/state",
            "/api/match/state",
            "/match/start",
            "/api/match/start",
            "/match/pause",
            "/api/match/pause",
            "/match/resume",
            "/api/match/resume",
            "/match/finish",
            "/api/match/finish",
        }:
            if not self._require_operator():
                return
            body = self._read_body()
            if path.endswith("/start") or path.endswith("/resume"):
                target_status = MatchState.RUNNING.value
            elif path.endswith("/pause"):
                target_status = MatchState.PAUSED.value
            elif path.endswith("/finish"):
                target_status = MatchState.FINISHED.value
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
                        "UPDATE matches SET status = ? WHERE id = 1;", (new_state.value,)
                    )
                    conn.commit()
                    telemetry.emit_safe(
                        db.get_db_path(),
                        telemetry.MATCH_STATE_CHANGED,
                        "gameserver",
                        match_id=1,
                        payload={"from": current_status, "to": new_state.value},
                    )

                # Return fresh status
                cursor.execute(
                    "SELECT id, status, created_at, updated_at FROM matches WHERE id = 1;"
                )
                match_row = cursor.fetchone()
                match_obj = Match.from_row(match_row)
                self._json(
                    200,
                    {
                        "match_id": match_obj.id,
                        "status": match_obj.status.value,
                        "created_at": match_obj.created_at,
                        "updated_at": match_obj.updated_at,
                    },
                )

            except Exception as e:
                self._json(500, {"error": f"Database error: {str(e)}"})
            finally:
                if conn:
                    conn.close()
            return

        # ── POST /rounds/step ──
        if path == "/rounds/step" or path == "/api/rounds/step":
            if not self._require_operator():
                return
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

        # ── POST /telemetry/ingest ──
        if path in {"/telemetry/ingest", "/api/telemetry/ingest"}:
            if not self._require_operator():
                return
            body = self._read_body()
            events = body.get("events")
            if not isinstance(events, list):
                self._json(400, {"error": "body must contain an 'events' list"})
                return
            conn = None
            ingested = 0
            try:
                conn = db.get_db_connection()
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    event_type = ev.get("event_type")
                    source = ev.get("source", "external")
                    if not isinstance(event_type, str) or not event_type:
                        continue
                    match_id = ev.get("match_id")
                    round_number = ev.get("round_number")
                    team_id = ev.get("team_id")
                    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                    correlation_id = ev.get("correlation_id")
                    telemetry.emit(
                        conn,
                        event_type,
                        source,
                        match_id=match_id if isinstance(match_id, int) else None,
                        round_number=round_number if isinstance(round_number, int) else None,
                        team_id=team_id if isinstance(team_id, int) else None,
                        payload=payload,
                        correlation_id=str(correlation_id) if correlation_id else None,
                    )
                    ingested += 1
                conn.commit()
                self._json(200, {"ingested": ingested})
            except Exception:
                self._json(500, {"error": "ingest failed"})
            finally:
                if conn:
                    conn.close()
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandcastle Gameserver API")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="Interface to bind to (default: 0.0.0.0)"
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
    GameserverAPIHandler.operator_token = os.environ.get("GAMESERVER_OPERATOR_TOKEN") or config.get(
        "ARENA_OPERATOR_TOKEN", ""
    )
    if len(GameserverAPIHandler.operator_token) < 24:
        print(
            "[!] Gameserver operator token must contain at least 24 characters",
            file=sys.stderr,
        )
        sys.exit(1)

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
