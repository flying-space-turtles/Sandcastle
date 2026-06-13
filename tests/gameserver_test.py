#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

# Add gameserver directory to python path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gameserver"))

import db
import models
from models import MatchState, validate_state_transition
from security import hash_team_token, verify_team_token
from submissions import (
    SubmissionCode,
    TeamRateLimiter,
    authenticate_team,
    record_submission,
)


class GameserverTest(unittest.TestCase):
    def setUp(self):
        # Override database path to use in-memory SQLite
        self.db_path = ":memory:"
        os.environ["GAMESERVER_DB_PATH"] = self.db_path
        self.conn = db.get_db_connection(self.db_path)
        db.initialize_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_initialization(self):
        cursor = self.conn.cursor()
        
        # Verify all tables exist
        tables = [
            "matches", "teams", "services", "rounds", 
            "flags", "checker_results", "submissions", "score_events"
        ]
        for table in tables:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", 
                (table,)
            )
            self.assertIsNotNone(cursor.fetchone(), f"Table {table} was not created.")

        # Verify default match exists with status CREATED
        cursor.execute("SELECT status FROM matches WHERE id = 1;")
        row = cursor.fetchone()
        self.assertEqual(row[0], MatchState.CREATED.value)

    def test_match_state_transitions(self):
        # Valid: CREATED -> RUNNING
        new_state = validate_state_transition(MatchState.CREATED, MatchState.RUNNING)
        self.assertEqual(new_state, MatchState.RUNNING)

        # Idempotent: RUNNING -> RUNNING
        new_state = validate_state_transition(MatchState.RUNNING, MatchState.RUNNING)
        self.assertEqual(new_state, MatchState.RUNNING)

        # Valid: RUNNING -> PAUSED
        new_state = validate_state_transition(MatchState.RUNNING, MatchState.PAUSED)
        self.assertEqual(new_state, MatchState.PAUSED)

        # Valid: PAUSED -> FINISHED
        new_state = validate_state_transition(MatchState.PAUSED, MatchState.FINISHED)
        self.assertEqual(new_state, MatchState.FINISHED)

        # Invalid: FINISHED -> RUNNING
        with self.assertRaises(ValueError):
            validate_state_transition(MatchState.FINISHED, MatchState.RUNNING)

        # Invalid: CREATED -> PAUSED
        with self.assertRaises(ValueError):
            validate_state_transition(MatchState.CREATED, MatchState.PAUSED)

    def test_team_tokens_use_salted_slow_hashes(self):
        token = "team-submission-token-secret"
        first = hash_team_token(token)
        second = hash_team_token(token)

        self.assertNotEqual(first, second)
        self.assertNotIn(token, first)
        self.assertTrue(verify_team_token(token, first))
        self.assertFalse(verify_team_token("wrong-token", first))
        self.assertFalse(verify_team_token(token, "malformed-hash"))

    def test_registry_synchronization(self):
        # Write a dummy config file
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("ARENA_TEAM_COUNT=3\n")
            f.write("ARENA_SERVICE_PORT=9090\n")
            f.write("ARENA_CTF_SUBNET=172.16.0.0/16\n")
            f.write("ARENA_SERVICE_TEMPLATE=services/turtle-notes\n")
            f.write("ARENA_TEAM_TOKEN_PATTERN=test-team{team}-submission-token-secret\n")
            config_path = f.name

        try:
            db.sync_registry(self.conn, config_path)
            cursor = self.conn.cursor()

            # Check teams
            cursor.execute("SELECT id, name, token, ip_address FROM teams ORDER BY id ASC;")
            teams = cursor.fetchall()
            self.assertEqual(len(teams), 3)
            for team_id, name, token_hash, ip_address in teams:
                self.assertEqual(name, f"Team {team_id}")
                self.assertEqual(ip_address, f"172.16.{team_id}.3")
                token = f"test-team{team_id}-submission-token-secret"
                self.assertNotEqual(token_hash, token)
                self.assertTrue(verify_team_token(token, token_hash))

            # Check services
            cursor.execute("SELECT name, port FROM services;")
            services = cursor.fetchall()
            self.assertEqual(len(services), 1)
            self.assertEqual(services[0], ("turtle-notes", 9090))

            # Shrink registry: team count to 2
            with open(config_path, "w") as f:
                f.write("ARENA_TEAM_COUNT=2\n")
                f.write("ARENA_SERVICE_PORT=9090\n")
                f.write("ARENA_CTF_SUBNET=172.16.0.0/16\n")
                f.write("ARENA_SERVICE_TEMPLATE=services/turtle-notes\n")
                f.write("ARENA_TEAM_TOKEN_PATTERN=test-team{team}-submission-token-secret\n")

            db.sync_registry(self.conn, config_path)
            cursor.execute("SELECT id FROM teams ORDER BY id ASC;")
            teams_after = cursor.fetchall()
            self.assertEqual(len(teams_after), 2)
            self.assertEqual(teams_after[0][0], 1)
            self.assertEqual(teams_after[1][0], 2)

        finally:
            os.unlink(config_path)


class SubmissionServiceTest(unittest.TestCase):
    def setUp(self):
        db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = db_file.name
        db_file.close()
        conn = db.get_db_connection(self.db_path)
        db.initialize_schema(conn)
        conn.executemany(
            "INSERT INTO teams (id, name, token, ip_address) VALUES (?, ?, ?, ?)",
            [
                (1, "Team 1", hash_team_token("token-1"), "10.10.1.3"),
                (2, "Team 2", hash_team_token("token-2"), "10.10.2.3"),
                (3, "Team 3", hash_team_token("token-3"), "10.10.3.3"),
            ],
        )
        conn.execute("INSERT INTO services (id, name, port) VALUES (1, 'notes', 8080)")
        conn.execute(
            """
            INSERT INTO rounds (
                match_id, round_number, status, started_at, deadline_at,
                completed_at, duration_seconds
            ) VALUES (1, 1, 'COMPLETED', '2026-01-01T00:00:00Z',
                      '2026-01-01T00:02:00Z', '2026-01-01T00:00:01Z', 120)
            """
        )
        self.flag = "FLAG{aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
        self.self_flag = "FLAG{bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}"
        self.expired_flag = "FLAG{cccccccccccccccccccccccccccccccc}"
        conn.executemany(
            """
            INSERT INTO flags (
                flag, match_id, team_id, service_id, round_number,
                target_host, service_name, service_port, status,
                expires_after_round, created_at, expired_at
            ) VALUES (?, 1, ?, 1, ?, ?, 'notes', 8080, ?, ?, ?, ?)
            """,
            [
                (
                    self.flag, 2, 1, "10.10.2.3", "ACTIVE", 3,
                    "2026-01-01T00:00:00Z", None,
                ),
                (
                    self.self_flag, 1, 1, "10.10.1.3", "ACTIVE", 3,
                    "2026-01-01T00:00:00Z", None,
                ),
                (
                    self.expired_flag, 2, 0, "10.10.2.3", "EXPIRED", 1,
                    "2025-12-31T23:58:00Z", "2026-01-01T00:00:00Z",
                ),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_concurrent_duplicate_is_atomic(self):
        def submit(_index):
            return record_submission(1, self.flag, self.db_path).code

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(submit, range(8)))

        self.assertEqual(outcomes.count(SubmissionCode.ACCEPTED), 1)
        self.assertEqual(outcomes.count(SubmissionCode.DUPLICATE), 7)
        conn = db.get_db_connection(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0], 1)
        conn.close()

    def test_submission_outcomes_are_distinct(self):
        self.assertEqual(record_submission(1, "invalid", self.db_path).code, SubmissionCode.MALFORMED)
        self.assertEqual(
            record_submission(
                1,
                "FLAG{dddddddddddddddddddddddddddddddd}",
                self.db_path,
            ).code,
            SubmissionCode.UNKNOWN,
        )
        self.assertEqual(
            record_submission(1, self.self_flag, self.db_path).code,
            SubmissionCode.SELF_OWNED,
        )
        self.assertEqual(
            record_submission(1, self.expired_flag, self.db_path).code,
            SubmissionCode.EXPIRED,
        )
        self.assertEqual(
            record_submission(1, self.flag, self.db_path).code,
            SubmissionCode.ACCEPTED,
        )
        self.assertEqual(
            record_submission(1, self.flag, self.db_path).code,
            SubmissionCode.DUPLICATE,
        )

    def test_authentication_uses_the_declared_team_scope(self):
        self.assertTrue(authenticate_team(1, "token-1", self.db_path))
        self.assertFalse(authenticate_team(1, "token-2", self.db_path))
        self.assertFalse(authenticate_team(2, "token-1", self.db_path))
        self.assertFalse(authenticate_team("1", "token-1", self.db_path))

    def test_opponent_flag_is_accepted_once_per_attacker(self):
        self.assertEqual(
            record_submission(1, self.flag, self.db_path).code,
            SubmissionCode.ACCEPTED,
        )
        self.assertEqual(
            record_submission(3, self.flag, self.db_path).code,
            SubmissionCode.ACCEPTED,
        )
        self.assertEqual(
            record_submission(3, self.flag, self.db_path).code,
            SubmissionCode.DUPLICATE,
        )
        conn = db.get_db_connection(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0], 2)
        conn.close()

    def test_rate_limiter_is_team_scoped_and_recovers_after_window(self):
        now = [100.0]
        limiter = TeamRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])

        self.assertTrue(limiter.check(1).allowed)
        self.assertTrue(limiter.check(1).allowed)
        limited = limiter.check(1)
        self.assertFalse(limited.allowed)
        self.assertEqual(limited.retry_after_seconds, 10)
        self.assertTrue(limiter.check(2).allowed)

        now[0] = 110.0
        self.assertTrue(limiter.check(1).allowed)


class GameserverHTTPTest(unittest.TestCase):
    team_tokens = {
        1: "test-team1-submission-token-secret",
        2: "test-team2-submission-token-secret",
    }

    @classmethod
    def setUpClass(cls):
        # Ephemeral local port
        cls.host = "127.0.0.1"
        cls.port = 0  # system assigns free port
        
        # Override DB environment for the handler
        cls.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.db_path = cls.db_file.name
        cls.db_file.close()
        os.environ["GAMESERVER_DB_PATH"] = cls.db_path

        # Initialize schema
        conn = db.get_db_connection(cls.db_path)
        db.initialize_schema(conn)
        conn.executemany(
            "INSERT INTO teams (id, name, token, ip_address) VALUES (?, ?, ?, ?)",
            [
                (
                    team_id,
                    f"Team {team_id}",
                    hash_team_token(token),
                    f"10.10.{team_id}.3",
                )
                for team_id, token in cls.team_tokens.items()
            ],
        )
        conn.execute("INSERT INTO services (id, name, port) VALUES (1, 'notes', 8080)")
        conn.commit()
        conn.close()

        # Start HTTP server in background thread
        import main
        cls.main_module = main
        cls.server = ThreadingHTTPServer((cls.host, cls.port), main.GameserverAPIHandler)
        cls.assigned_port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.base_url = f"http://{cls.host}:{cls.assigned_port}"

    @classmethod
    def tearDownClass(cls):
        cls.main_module.GameserverAPIHandler.tick_engine = None
        cls.server.shutdown()
        cls.server.server_close()
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)

    def setUp(self):
        self.main_module.GameserverAPIHandler.tick_engine = None
        self.main_module.GameserverAPIHandler.submission_rate_limiter = TeamRateLimiter(
            limit=100,
            window_seconds=60,
        )
        conn = db.get_db_connection(self.db_path)
        conn.execute("DELETE FROM score_events")
        conn.execute("DELETE FROM submissions")
        conn.execute("DELETE FROM checker_results")
        conn.execute("DELETE FROM flags")
        conn.execute("DELETE FROM rounds")
        conn.execute(
            "UPDATE matches SET status = ? WHERE id = 1",
            (MatchState.CREATED.value,),
        )
        conn.commit()
        conn.close()

    def _post(self, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
        )
        return urllib.request.urlopen(request, timeout=5)

    def _post_result(self, path, body=None, token=None):
        try:
            with self._post(path, body, token) as response:
                return (
                    response.status,
                    json.loads(response.read().decode()),
                    response.headers,
                )
        except urllib.error.HTTPError as error:
            return (
                error.code,
                json.loads(error.read().decode()),
                error.headers,
            )

    def _insert_round_and_flags(self):
        conn = db.get_db_connection(self.db_path)
        conn.execute(
            """
            INSERT INTO rounds (
                match_id, round_number, status, started_at, deadline_at,
                completed_at, duration_seconds
            ) VALUES (1, 3, 'COMPLETED', '2026-01-01T00:00:00Z',
                      '2026-01-01T00:02:00Z', '2026-01-01T00:00:01Z', 120)
            """
        )
        flags = {
            "opponent": "FLAG{11111111111111111111111111111111}",
            "self": "FLAG{22222222222222222222222222222222}",
            "expired": "FLAG{33333333333333333333333333333333}",
        }
        conn.executemany(
            """
            INSERT INTO flags (
                flag, match_id, team_id, service_id, round_number,
                target_host, service_name, service_port, status,
                expires_after_round, created_at, expired_at
            ) VALUES (?, 1, ?, 1, ?, ?, 'notes', 8080, ?, ?, ?, ?)
            """,
            [
                (
                    flags["opponent"], 2, 3, "10.10.2.3", "ACTIVE", 5,
                    "2026-01-01T00:00:00Z", None,
                ),
                (
                    flags["self"], 1, 3, "10.10.1.3", "ACTIVE", 5,
                    "2026-01-01T00:00:00Z", None,
                ),
                (
                    flags["expired"], 2, 2, "10.10.2.3", "EXPIRED", 3,
                    "2025-12-31T23:58:00Z", "2026-01-01T00:00:00Z",
                ),
            ],
        )
        conn.commit()
        conn.close()
        return flags

    def test_get_health(self):
        url = f"{self.base_url}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["status"], "UP")
            self.assertEqual(body["database"], "connected")

    def test_get_match_state(self):
        url = f"{self.base_url}/match"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["match_id"], 1)
            self.assertEqual(body["status"], MatchState.CREATED.value)

    def test_post_match_state_transition(self):
        url = f"{self.base_url}/match/state"
        
        # 1. Valid Transition: CREATED -> RUNNING
        data = json.dumps({"status": "RUNNING"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["status"], "RUNNING")

        # 2. Idempotent Transition: RUNNING -> RUNNING
        data = json.dumps({"status": "RUNNING"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["status"], "RUNNING")

        # 3. Invalid Transition: RUNNING -> CREATED (should fail with 400)
        data = json.dumps({"status": "CREATED"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        err_body = json.loads(ctx.exception.read().decode())
        self.assertIn("error", err_body)
        self.assertIn("Invalid match state transition", err_body["error"])

    def test_pause_resume_and_single_step_controls(self):
        with self._post("/match/resume") as resp:
            self.assertEqual(json.loads(resp.read().decode())["status"], "RUNNING")
        with self._post("/match/pause") as resp:
            self.assertEqual(json.loads(resp.read().decode())["status"], "PAUSED")

        class FakeRound:
            def as_dict(self):
                return {"round_number": 7, "status": "COMPLETED"}

        class FakeEngine:
            def single_step(self):
                return FakeRound()

        self.main_module.GameserverAPIHandler.tick_engine = FakeEngine()
        with self._post("/rounds/step") as resp:
            body = json.loads(resp.read().decode())
            self.assertEqual(body["round"]["round_number"], 7)
            self.assertEqual(body["round"]["status"], "COMPLETED")

    def test_get_current_round(self):
        conn = db.get_db_connection(self.db_path)
        conn.execute(
            """
            INSERT INTO rounds (
                match_id, round_number, status, started_at, deadline_at,
                completed_at, duration_seconds
            ) VALUES (1, 1, 'COMPLETED', '2026-01-01T00:00:00Z',
                      '2026-01-01T00:02:00Z', '2026-01-01T00:00:01Z', 120)
            """
        )
        conn.commit()
        conn.close()
        with urllib.request.urlopen(f"{self.base_url}/rounds/current", timeout=5) as resp:
            body = json.loads(resp.read().decode())
            self.assertEqual(body["round_number"], 1)
            self.assertEqual(body["status"], "COMPLETED")

    def test_get_teams_does_not_expose_tokens(self):
        with urllib.request.urlopen(f"{self.base_url}/teams", timeout=5) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(len(body["teams"]), 2)
        self.assertNotIn("token", body["teams"][0])
        self.assertNotIn("token_hash", body["teams"][0])

    def test_flag_submission_requires_valid_team_authentication(self):
        flags = self._insert_round_and_flags()
        request_body = {"team_id": 1, "flag": flags["opponent"]}

        status, body, headers = self._post_result("/flags/submit", request_body)
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "UNAUTHORIZED")
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")

        status, body, _ = self._post_result(
            "/flags/submit",
            request_body,
            "wrong-team-token",
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "UNAUTHORIZED")

        conn = db.get_db_connection(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)
        conn.close()

    def test_flag_submission_returns_distinct_machine_outcomes(self):
        flags = self._insert_round_and_flags()
        token = self.team_tokens[1]

        status, body, _ = self._post_result(
            "/api/flags/submit",
            {"team_id": 1, "flag": flags["opponent"]},
            token,
        )
        self.assertEqual((status, body["code"], body["accepted"]), (201, "ACCEPTED", True))

        cases = [
            (flags["opponent"], 409, "DUPLICATE"),
            (flags["self"], 403, "SELF_OWNED"),
            (flags["expired"], 410, "EXPIRED"),
            ("not-a-flag", 400, "MALFORMED"),
            ("FLAG{44444444444444444444444444444444}", 404, "UNKNOWN"),
        ]
        for flag, expected_status, expected_code in cases:
            with self.subTest(code=expected_code):
                status, body, _ = self._post_result(
                    "/flags/submit",
                    {"team_id": 1, "flag": flag},
                    token,
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(body["code"], expected_code)
                self.assertFalse(body["accepted"])

        conn = db.get_db_connection(self.db_path)
        submissions = conn.execute(
            "SELECT id, status FROM submissions WHERE attacker_id = 1"
        ).fetchall()
        score_events = conn.execute(
            "SELECT points, details, submission_id FROM score_events WHERE team_id = 1"
        ).fetchall()
        conn.close()
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0][1], "ACCEPTED")
        self.assertEqual(len(score_events), 1)
        self.assertEqual(score_events[0][0], 1.0)
        self.assertEqual(score_events[0][2], submissions[0][0])
        self.assertNotIn(flags["opponent"], score_events[0][1])

    def test_flag_submission_rate_limit_is_per_team(self):
        self.main_module.GameserverAPIHandler.submission_rate_limiter = TeamRateLimiter(
            limit=2,
            window_seconds=60,
        )
        token = self.team_tokens[1]

        first = self._post_result(
            "/flags/submit",
            {"team_id": 1, "flag": "invalid"},
            token,
        )
        second = self._post_result(
            "/flags/submit",
            {"team_id": 1, "flag": "FLAG{55555555555555555555555555555555}"},
            token,
        )
        third = self._post_result(
            "/flags/submit",
            {"team_id": 1, "flag": "FLAG{66666666666666666666666666666666}"},
            token,
        )

        self.assertEqual(first[1]["code"], "MALFORMED")
        self.assertEqual(second[1]["code"], "UNKNOWN")
        self.assertEqual(third[0], 429)
        self.assertEqual(third[1]["code"], "RATE_LIMITED")
        self.assertGreaterEqual(int(third[2]["Retry-After"]), 1)

        other_team = self._post_result(
            "/flags/submit",
            {"team_id": 2, "flag": "FLAG{77777777777777777777777777777777}"},
            self.team_tokens[2],
        )
        self.assertEqual(other_team[1]["code"], "UNKNOWN")

    def test_concurrent_duplicate_submission_scores_once(self):
        flag = self._insert_round_and_flags()["opponent"]

        def submit_once(_index):
            status, body, _ = self._post_result(
                "/flags/submit",
                {"team_id": 1, "flag": flag},
                self.team_tokens[1],
            )
            return status, body["code"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(submit_once, range(8)))

        self.assertEqual(outcomes.count((201, "ACCEPTED")), 1)
        self.assertEqual(outcomes.count((409, "DUPLICATE")), 7)
        conn = db.get_db_connection(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
