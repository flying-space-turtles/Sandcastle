"""Compute per-round scores for each team.

Simplified scheme from LOCAL_AD_CTF_SIMULATION.md §7.4:

    +1 attack point per stolen flag
    +1 defense point per round a flag is NOT stolen
    +1 SLA point if the service is UP, 0 otherwise
    total = (attack + defense) * sla_multiplier
        where sla_multiplier = 1.0 if UP, 0.5 if MUMBLE/CORRUPT, 0.0 if DOWN
"""

from __future__ import annotations

from .db import Database


SLA_MULTIPLIER = {
    "UP": 1.0,
    "MUMBLE": 0.5,
    "CORRUPT": 0.5,
    "DOWN": 0.0,
}


def calculate_scores(db: Database, round_no: int) -> None:
    teams = db.list_teams()
    if not teams:
        return

    for team in teams:
        with db.cursor() as cur:
            attack_row = cur.execute(
                """
                SELECT COUNT(*) AS c FROM submissions s
                JOIN flags f ON f.id = s.flag_id
                WHERE s.attacker_id = ? AND f.round = ?
                """,
                (team["id"], round_no),
            ).fetchone()
            attack_pts = float(attack_row["c"])

            stolen_row = cur.execute(
                """
                SELECT COUNT(*) AS c FROM flags f
                JOIN submissions s ON s.flag_id = f.id
                WHERE f.team_id = ? AND f.round = ?
                """,
                (team["id"], round_no),
            ).fetchone()
            stolen = int(stolen_row["c"])
            defense_pts = 0.0 if stolen > 0 else 1.0

            sla_row = cur.execute(
                "SELECT status FROM sla_checks "
                "WHERE team_id = ? AND round = ? "
                "ORDER BY checked_at DESC LIMIT 1",
                (team["id"], round_no),
            ).fetchone()
        sla_status = sla_row["status"] if sla_row else "DOWN"
        sla_pts = 1.0 if sla_status == "UP" else 0.0
        multiplier = SLA_MULTIPLIER.get(sla_status, 0.0)
        total = (attack_pts + defense_pts) * multiplier

        db.upsert_score(team["id"], round_no, attack_pts, defense_pts, sla_pts, total)
