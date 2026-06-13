"""Structured telemetry for Sandcastle match events.

Schema version: 1

Gameserver-emitted event types:
  round.started, round.completed, round.failed
  submission.received, submission.accepted, submission.rejected
  match.state_changed

Forwarded bot-controller event types (any prefix allowed):
  bot.*

Checker operation details are already stored in checker_results; those rows
are included in the match export without duplicating them as telemetry events.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

EVENT_SCHEMA_VERSION = 1

ROUND_STARTED = "round.started"
ROUND_COMPLETED = "round.completed"
ROUND_FAILED = "round.failed"
SUBMISSION_RECEIVED = "submission.received"
SUBMISSION_ACCEPTED = "submission.accepted"
SUBMISSION_REJECTED = "submission.rejected"
MATCH_STATE_CHANGED = "match.state_changed"

_FLAG_RE = re.compile(r"FLAG\{[a-f0-9]{32}\}", re.IGNORECASE)
_REDACT_KEYS = frozenset({
    "flag", "token", "password", "secret", "credential",
    "team_token", "submission_token", "operator_token",
    "master_secret", "checker_master_secret",
})


def redact(obj: Any) -> Any:
    """Recursively scrub sensitive values before persistence."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in _REDACT_KEYS else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _FLAG_RE.sub("FLAG{<redacted>}", obj)
    return obj


def emit(
    conn: sqlite3.Connection,
    event_type: str,
    source: str,
    *,
    match_id: int | None = None,
    round_number: int | None = None,
    team_id: int | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> int:
    """Insert one telemetry event; caller is responsible for commit."""
    payload_json = json.dumps(
        redact(payload or {}), sort_keys=True, separators=(",", ":")
    )
    cursor = conn.execute(
        """
        INSERT INTO telemetry_events (
            schema_version, event_type, source,
            match_id, round_number, team_id,
            payload_json, correlation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            EVENT_SCHEMA_VERSION,
            event_type,
            source,
            match_id,
            round_number,
            team_id,
            payload_json,
            correlation_id,
        ),
    )
    return int(cursor.lastrowid)


def emit_safe(
    db_path: str,
    event_type: str,
    source: str,
    **kwargs: Any,
) -> None:
    """Fire-and-forget telemetry emit. Opens its own connection; never raises."""
    try:
        import db as _db
        conn = _db.get_db_connection(db_path)
        try:
            emit(conn, event_type, source, **kwargs)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def export_match(conn: sqlite3.Connection, match_id: int) -> list[dict[str, Any]]:
    """Return all telemetry events for a match, ordered by insertion time."""
    rows = conn.execute(
        """
        SELECT id, schema_version, event_type, source,
               match_id, round_number, team_id,
               payload_json, correlation_id, created_at
        FROM telemetry_events
        WHERE match_id = ?
        ORDER BY id ASC
        """,
        (match_id,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "schema_version": row[1],
            "event_type": row[2],
            "source": row[3],
            "match_id": row[4],
            "round_number": row[5],
            "team_id": row[6],
            "payload": json.loads(row[7]),
            "correlation_id": row[8],
            "created_at": row[9],
        }
        for row in rows
    ]


def compute_metrics(conn: sqlite3.Connection, match_id: int) -> dict[str, Any]:
    """Per-team match metrics derived from game tables and telemetry events."""
    teams = conn.execute(
        "SELECT id, name FROM teams ORDER BY id"
    ).fetchall()

    teams_out: dict[str, Any] = {}
    for team_id, team_name in teams:
        flags_captured = conn.execute(
            """
            SELECT COUNT(*) FROM score_events
            WHERE match_id = ? AND team_id = ? AND event_type = 'ATTACK'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        attack_points = conn.execute(
            """
            SELECT COALESCE(SUM(points), 0.0) FROM score_events
            WHERE match_id = ? AND team_id = ? AND event_type = 'ATTACK'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        defense_points = conn.execute(
            """
            SELECT COALESCE(SUM(points), 0.0) FROM score_events
            WHERE match_id = ? AND team_id = ? AND event_type = 'DEFENSE'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        sla_points = conn.execute(
            """
            SELECT COALESCE(SUM(points), 0.0) FROM score_events
            WHERE match_id = ? AND team_id = ? AND event_type = 'SLA'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        checker_total = conn.execute(
            """
            SELECT COUNT(*) FROM checker_results
            WHERE match_id = ? AND team_id = ?
            """,
            (match_id, team_id),
        ).fetchone()[0]

        checker_up = conn.execute(
            """
            SELECT COUNT(*) FROM checker_results
            WHERE match_id = ? AND team_id = ? AND status = 'UP'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        bot_actions = conn.execute(
            """
            SELECT COUNT(*) FROM telemetry_events
            WHERE match_id = ? AND team_id = ? AND event_type = 'bot.action.completed'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        bot_errors = conn.execute(
            """
            SELECT COUNT(*) FROM telemetry_events
            WHERE match_id = ? AND team_id = ?
              AND json_extract(payload_json, '$.status') = 'error'
            """,
            (match_id, team_id),
        ).fetchone()[0]

        teams_out[str(team_id)] = {
            "team_id": team_id,
            "team_name": team_name,
            "total_points": float(attack_points + defense_points + sla_points),
            "attack_points": float(attack_points),
            "defense_points": float(defense_points),
            "sla_points": float(sla_points),
            "flags_captured": flags_captured,
            "checker_sla_rate": (
                round(checker_up / checker_total, 4) if checker_total > 0 else None
            ),
            "bot_actions_completed": bot_actions,
            "bot_errors": bot_errors,
        }

    return {"match_id": match_id, "teams": teams_out}
