#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gameserver"))

import db
import main as gameserver_main
from scoring import (
    get_scoring_policy,
    reconcile_score_events,
    standings_from_events,
    standings_from_sources,
)
from security import hash_team_token
from submissions import SubmissionCode, record_submission


class ScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = db_file.name
        db_file.close()
        self.conn = db.get_db_connection(self.db_path)
        db.initialize_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO teams (id, name, token, ip_address) VALUES (?, ?, ?, ?)",
            [
                (team_id, f"Team {team_id}", hash_team_token(f"token-{team_id}"), f"10.10.{team_id}.3")
                for team_id in range(1, 6)
            ],
        )
        self.conn.execute("INSERT INTO services (id, name, port) VALUES (1, 'notes', 8080)")
        self._insert_round(1)
        self._insert_round(2)
        self.flags = {
            "team1": "FLAG{11111111111111111111111111111111}",
            "team2": "FLAG{22222222222222222222222222222222}",
            "expired": "FLAG{33333333333333333333333333333333}",
        }
        self._insert_flag(self.flags["team1"], team_id=1, round_number=1)
        self._insert_flag(self.flags["team2"], team_id=2, round_number=1)
        self._insert_flag(
            self.flags["expired"],
            team_id=2,
            round_number=2,
            status="EXPIRED",
            expires_after_round=2,
            expired_at="2026-01-01T00:04:00Z",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.db_path)

    def _insert_round(self, round_number: int) -> None:
        minute = round_number * 2
        self.conn.execute(
            """
            INSERT INTO rounds (
                match_id, round_number, status, started_at, deadline_at,
                completed_at, duration_seconds
            ) VALUES (1, ?, 'COMPLETED', ?, ?, ?, 120)
            """,
            (
                round_number,
                f"2026-01-01T00:{minute:02d}:00Z",
                f"2026-01-01T00:{minute + 2:02d}:00Z",
                f"2026-01-01T00:{minute:02d}:01Z",
            ),
        )

    def _insert_flag(
        self,
        flag: str,
        team_id: int,
        round_number: int,
        status: str = "ACTIVE",
        expires_after_round: int = 5,
        expired_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO flags (
                flag, match_id, team_id, service_id, round_number,
                target_host, service_name, service_port, status,
                expires_after_round, created_at, expired_at
            ) VALUES (?, 1, ?, 1, ?, ?, 'notes', 8080, ?, ?, ?, ?)
            """,
            (
                flag,
                team_id,
                round_number,
                f"10.10.{team_id}.3",
                status,
                expires_after_round,
                f"2026-01-01T00:{round_number * 2:02d}:00Z",
                expired_at,
            ),
        )

    def _insert_checker(
        self,
        team_id: int,
        round_number: int,
        operation: str,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO checker_results (
                match_id, team_id, service_id, round_number, operation,
                plugin_name, plugin_version, status, message, duration_ms, data_json
            ) VALUES (1, ?, 1, ?, ?, 'notes', '1', ?, 'test', 1, '{}')
            """,
            (team_id, round_number, operation, status),
        )

    def _insert_accepted_submission(self, attacker_id: int, flag: str) -> None:
        self.conn.execute(
            "INSERT INTO submissions (flag, attacker_id, status) VALUES (?, ?, 'ACCEPTED')",
            (flag, attacker_id),
        )

    def test_replay_matches_stored_standings_and_is_idempotent(self) -> None:
        self._insert_accepted_submission(1, self.flags["team2"])
        self._insert_checker(1, 1, "GET", "UP")
        self._insert_checker(1, 1, "CHECK", "UP")
        self.conn.commit()

        self.assertEqual(reconcile_score_events(self.conn), 3)
        self.assertEqual(reconcile_score_events(self.conn), 0)
        self.assertEqual(
            standings_from_events(self.conn),
            standings_from_sources(self.conn),
        )

        events = self.conn.execute(
            "SELECT event_type, points FROM score_events ORDER BY event_type"
        ).fetchall()
        self.assertEqual(events, [("ATTACK", 10.0), ("DEFENSE", 2.0), ("SLA", 1.0)])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute("UPDATE score_events SET points = 999")

    def test_multi_team_components_and_deterministic_ties(self) -> None:
        self._insert_accepted_submission(1, self.flags["team2"])
        self._insert_accepted_submission(3, self.flags["team2"])
        self._insert_checker(1, 1, "GET", "UP")
        self._insert_checker(1, 1, "CHECK", "UP")
        self._insert_checker(2, 1, "GET", "UP")
        self._insert_checker(2, 1, "CHECK", "DOWN")
        self._insert_checker(3, 1, "GET", "DOWN")
        self._insert_checker(3, 1, "CHECK", "UP")
        self.conn.commit()
        reconcile_score_events(self.conn)

        standings = standings_from_events(self.conn)
        self.assertEqual([row["team_id"] for row in standings], [1, 3, 2, 4, 5])
        by_team = {row["team_id"]: row for row in standings}
        self.assertEqual(
            (by_team[1]["attack"], by_team[1]["defense"], by_team[1]["sla"], by_team[1]["total"]),
            (10.0, 2.0, 1.0, 13.0),
        )
        self.assertEqual(by_team[2]["sla"], 0.0)
        self.assertEqual(by_team[3]["defense"], 0.0)
        self.assertLess(by_team[4]["rank"], by_team[5]["rank"])

    def test_duplicate_and_expired_submissions_do_not_duplicate_attack_score(self) -> None:
        self.conn.close()
        accepted = record_submission(1, self.flags["team2"], self.db_path)
        duplicate = record_submission(1, self.flags["team2"], self.db_path)
        expired = record_submission(1, self.flags["expired"], self.db_path)
        self.conn = db.get_db_connection(self.db_path)

        self.assertEqual(accepted.code, SubmissionCode.ACCEPTED)
        self.assertEqual(duplicate.code, SubmissionCode.DUPLICATE)
        self.assertEqual(expired.code, SubmissionCode.EXPIRED)
        self.assertEqual(reconcile_score_events(self.conn), 0)
        attack_events = self.conn.execute(
            "SELECT COUNT(*), SUM(points) FROM score_events WHERE event_type = 'ATTACK'"
        ).fetchone()
        self.assertEqual(attack_events, (1, 10.0))

    def test_failed_sla_does_not_remove_defense_component(self) -> None:
        self._insert_checker(2, 2, "PUT", "UP")
        self._insert_checker(2, 2, "GET", "UP")
        self._insert_checker(2, 2, "CHECK", "DOWN")
        self.conn.commit()
        reconcile_score_events(self.conn)

        round_scores = standings_from_events(self.conn, round_number=2)
        team2 = next(row for row in round_scores if row["team_id"] == 2)
        self.assertEqual(team2["defense"], 2.0)
        self.assertEqual(team2["sla"], 0.0)
        self.assertEqual(team2["total"], 2.0)
        self.assertEqual(team2["defense_events"], 1)

    def test_policy_is_stored_with_match_from_arena_configuration(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as config:
            config.write("ARENA_TEAM_COUNT=5\n")
            config.write("ARENA_SERVICE_PORT=8080\n")
            config.write("ARENA_CTF_SUBNET=10.10.0.0/16\n")
            config.write("ARENA_SERVICE_TEMPLATE=services/notes\n")
            config.write("ARENA_TEAM_TOKEN_PATTERN=test-team{team}-submission-token-secret\n")
            config.write("ARENA_SCORE_ATTACK_POINTS=7\n")
            config.write("ARENA_SCORE_DEFENSE_POINTS=3\n")
            config.write("ARENA_SCORE_SLA_POINTS=2\n")
            config_path = config.name
        try:
            db.sync_registry(self.conn, config_path)
        finally:
            os.unlink(config_path)

        policy = get_scoring_policy(self.conn)
        self.assertEqual(policy.version, "sandcastle-v1")
        self.assertEqual(policy.attack_points, 7.0)
        self.assertEqual(policy.defense_points, 3.0)
        self.assertEqual(policy.sla_points, 2.0)

        self._insert_accepted_submission(1, self.flags["team2"])
        self.conn.commit()
        reconcile_score_events(self.conn)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as changed:
            changed.write("ARENA_TEAM_COUNT=5\n")
            changed.write("ARENA_SERVICE_PORT=8080\n")
            changed.write("ARENA_CTF_SUBNET=10.10.0.0/16\n")
            changed.write("ARENA_SERVICE_TEMPLATE=services/notes\n")
            changed.write("ARENA_TEAM_TOKEN_PATTERN=test-team{team}-submission-token-secret\n")
            changed.write("ARENA_SCORE_ATTACK_POINTS=99\n")
            changed.write("ARENA_SCORE_DEFENSE_POINTS=99\n")
            changed.write("ARENA_SCORE_SLA_POINTS=99\n")
            changed_path = changed.name
        try:
            db.sync_registry(self.conn, changed_path)
        finally:
            os.unlink(changed_path)
        self.assertEqual(get_scoring_policy(self.conn), policy)

    def test_score_api_payloads_include_standings_policy_and_round_breakdown(self) -> None:
        self._insert_accepted_submission(1, self.flags["team2"])
        self._insert_checker(1, 1, "GET", "UP")
        self._insert_checker(1, 1, "CHECK", "UP")
        self.conn.commit()
        previous_path = os.environ.get("GAMESERVER_DB_PATH")
        os.environ["GAMESERVER_DB_PATH"] = self.db_path
        responses: list[tuple[int, dict[str, object]]] = []
        handler = object.__new__(gameserver_main.GameserverAPIHandler)
        handler._json = lambda code, body, headers=None: responses.append((code, body))
        try:
            handler._scores()
            handler._scores(round_number=1)
            handler._scores(round_number=99)
        finally:
            if previous_path is None:
                os.environ.pop("GAMESERVER_DB_PATH", None)
            else:
                os.environ["GAMESERVER_DB_PATH"] = previous_path

        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["policy"]["version"], "sandcastle-v1")
        self.assertEqual(responses[0][1]["standings"][0]["total"], 13.0)
        self.assertEqual(responses[1][0], 200)
        self.assertEqual(responses[1][1]["round_number"], 1)
        self.assertEqual(responses[1][1]["standings"][0]["defense"], 2.0)
        self.assertEqual(responses[2], (404, {"code": "ROUND_NOT_FOUND", "round_number": 99}))


class ScoringMigrationTest(unittest.TestCase):
    def test_legacy_schema_adds_policy_and_event_source_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("INSERT INTO matches (id, status) VALUES (1, 'CREATED')")
        conn.execute(
            """
            CREATE TABLE teams (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                ip_address TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            "INSERT INTO teams VALUES (1, 'Team 1', 'legacy-token', '10.10.1.3')"
        )
        conn.execute(
            """
            CREATE TABLE score_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                points REAL NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO score_events (team_id, round_number, event_type, points)
            VALUES (1, 1, 'ATTACK', 1.0)
            """
        )

        db.initialize_schema(conn)

        match_columns = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(score_events)")}
        self.assertTrue(
            {"scoring_policy_version", "attack_points", "defense_points", "sla_points"}
            <= match_columns
        )
        self.assertTrue(
            {"match_id", "submission_id", "checker_result_id"} <= event_columns
        )
        self.assertEqual(get_scoring_policy(conn).attack_points, 1.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
