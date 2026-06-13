from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable


SCORING_COMPONENTS = ("ATTACK", "DEFENSE", "SLA")
TIEBREAKER = (
    "total_desc",
    "attack_desc",
    "defense_desc",
    "sla_desc",
    "team_id_asc",
)


@dataclass(frozen=True)
class ScoringPolicy:
    version: str
    attack_points: float
    defense_points: float
    sla_points: float

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "attack_points": self.attack_points,
            "defense_points": self.defense_points,
            "sla_points": self.sla_points,
        }


@dataclass(frozen=True)
class ProjectedScoreEvent:
    match_id: int
    team_id: int
    round_number: int
    event_type: str
    points: float
    details: str
    submission_id: int | None = None
    checker_result_id: int | None = None


def get_scoring_policy(conn: sqlite3.Connection, match_id: int = 1) -> ScoringPolicy:
    row = conn.execute(
        """
        SELECT scoring_policy_version, attack_points, defense_points, sla_points
        FROM matches WHERE id = ?
        """,
        (match_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"match {match_id} does not exist")
    return ScoringPolicy(
        version=str(row[0]),
        attack_points=float(row[1]),
        defense_points=float(row[2]),
        sla_points=float(row[3]),
    )


def project_score_events(
    conn: sqlite3.Connection,
    match_id: int = 1,
) -> list[ProjectedScoreEvent]:
    """Replay authoritative submissions and completed checker results."""
    policy = get_scoring_policy(conn, match_id)
    events: list[ProjectedScoreEvent] = []

    submission_rows = conn.execute(
        """
        SELECT s.id, s.attacker_id, f.round_number, f.team_id, f.service_id
        FROM submissions s
        JOIN flags f ON f.flag = s.flag
        WHERE f.match_id = ? AND s.status = 'ACCEPTED'
        ORDER BY s.id
        """,
        (match_id,),
    ).fetchall()
    for submission_id, attacker_id, round_number, victim_id, service_id in submission_rows:
        details = json.dumps(
            {
                "submission_id": submission_id,
                "victim_team_id": victim_id,
                "service_id": service_id,
                "flag_round": round_number,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        events.append(
            ProjectedScoreEvent(
                match_id=match_id,
                team_id=int(attacker_id),
                round_number=int(round_number),
                event_type="ATTACK",
                points=policy.attack_points,
                details=details,
                submission_id=int(submission_id),
            )
        )

    checker_rows = conn.execute(
        """
        SELECT cr.id, cr.team_id, cr.service_id, cr.round_number, cr.operation
        FROM checker_results cr
        JOIN rounds r
          ON r.match_id = cr.match_id AND r.round_number = cr.round_number
        JOIN teams t ON t.id = cr.team_id
        WHERE cr.match_id = ? AND r.status = 'COMPLETED' AND cr.status = 'UP'
          AND cr.operation IN ('GET', 'CHECK')
        ORDER BY cr.id
        """,
        (match_id,),
    ).fetchall()
    for checker_id, team_id, service_id, round_number, operation in checker_rows:
        event_type = "DEFENSE" if operation == "GET" else "SLA"
        points = policy.defense_points if event_type == "DEFENSE" else policy.sla_points
        details = json.dumps(
            {
                "checker_result_id": checker_id,
                "service_id": service_id,
                "operation": operation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        events.append(
            ProjectedScoreEvent(
                match_id=match_id,
                team_id=int(team_id),
                round_number=int(round_number),
                event_type=event_type,
                points=points,
                details=details,
                checker_result_id=int(checker_id),
            )
        )

    return events


def reconcile_score_events(conn: sqlite3.Connection, match_id: int = 1) -> int:
    """Append missing immutable events and return the number inserted."""
    projected = project_score_events(conn, match_id)
    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for event in projected:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO score_events (
                    match_id, team_id, round_number, event_type, points, details,
                    submission_id, checker_result_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.match_id,
                    event.team_id,
                    event.round_number,
                    event.event_type,
                    event.points,
                    event.details,
                    event.submission_id,
                    event.checker_result_id,
                ),
            )
            inserted += cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return inserted


def standings_from_events(
    conn: sqlite3.Connection,
    match_id: int = 1,
    round_number: int | None = None,
) -> list[dict[str, object]]:
    teams = conn.execute("SELECT id, name FROM teams ORDER BY id").fetchall()
    scores = _empty_scores(teams)
    parameters: list[object] = [match_id]
    round_filter = ""
    if round_number is not None:
        round_filter = " AND round_number = ?"
        parameters.append(round_number)
    rows = conn.execute(
        f"""
        SELECT team_id, event_type, SUM(points), COUNT(*)
        FROM score_events
        WHERE match_id = ?{round_filter}
        GROUP BY team_id, event_type
        """,
        parameters,
    ).fetchall()
    for team_id, event_type, points, event_count in rows:
        if team_id not in scores or event_type not in SCORING_COMPONENTS:
            continue
        component = event_type.lower()
        scores[team_id][component] = float(points)
        scores[team_id][f"{component}_events"] = int(event_count)
    return _rank_scores(scores.values())


def standings_from_sources(
    conn: sqlite3.Connection,
    match_id: int = 1,
    round_number: int | None = None,
) -> list[dict[str, object]]:
    """Calculate standings directly from source records for replay verification."""
    teams = conn.execute("SELECT id, name FROM teams ORDER BY id").fetchall()
    scores = _empty_scores(teams)
    for event in project_score_events(conn, match_id):
        if round_number is not None and event.round_number != round_number:
            continue
        component = event.event_type.lower()
        scores[event.team_id][component] += event.points
        scores[event.team_id][f"{component}_events"] += 1
    return _rank_scores(scores.values())


def _empty_scores(teams: Iterable[tuple[int, str]]) -> dict[int, dict[str, object]]:
    return {
        int(team_id): {
            "team_id": int(team_id),
            "team_name": str(team_name),
            "attack": 0.0,
            "defense": 0.0,
            "sla": 0.0,
            "attack_events": 0,
            "defense_events": 0,
            "sla_events": 0,
        }
        for team_id, team_name in teams
    }


def _rank_scores(scores: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    normalized = []
    for score in scores:
        item = dict(score)
        item["total"] = float(item["attack"]) + float(item["defense"]) + float(item["sla"])
        normalized.append(item)
    normalized.sort(
        key=lambda item: (
            -float(item["total"]),
            -float(item["attack"]),
            -float(item["defense"]),
            -float(item["sla"]),
            int(item["team_id"]),
        )
    )
    for rank, item in enumerate(normalized, start=1):
        item["rank"] = rank
    return normalized
