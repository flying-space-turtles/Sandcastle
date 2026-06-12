#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
from pathlib import Path

# Add gameserver directory to python path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gameserver"))

import db
import models
from models import MatchState, validate_state_transition


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

    def test_registry_synchronization(self):
        # Write a dummy config file
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("ARENA_TEAM_COUNT=3\n")
            f.write("ARENA_SERVICE_PORT=9090\n")
            f.write("ARENA_CTF_SUBNET=172.16.0.0/16\n")
            f.write("ARENA_SERVICE_TEMPLATE=services/turtle-notes\n")
            config_path = f.name

        try:
            db.sync_registry(self.conn, config_path)
            cursor = self.conn.cursor()

            # Check teams
            cursor.execute("SELECT id, name, token, ip_address FROM teams ORDER BY id ASC;")
            teams = cursor.fetchall()
            self.assertEqual(len(teams), 3)
            self.assertEqual(teams[0], (1, "Team 1", "team1-secret-token", "172.16.1.3"))
            self.assertEqual(teams[1], (2, "Team 2", "team2-secret-token", "172.16.2.3"))
            self.assertEqual(teams[2], (3, "Team 3", "team3-secret-token", "172.16.3.3"))

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

            db.sync_registry(self.conn, config_path)
            cursor.execute("SELECT id FROM teams ORDER BY id ASC;")
            teams_after = cursor.fetchall()
            self.assertEqual(len(teams_after), 2)
            self.assertEqual(teams_after[0][0], 1)
            self.assertEqual(teams_after[1][0], 2)

        finally:
            os.unlink(config_path)


class GameserverHTTPTest(unittest.TestCase):
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
        conn.close()

        # Start HTTPServer in background thread
        import main
        cls.main_module = main
        cls.server = HTTPServer((cls.host, cls.port), main.GameserverAPIHandler)
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
        conn = db.get_db_connection(self.db_path)
        conn.execute("DELETE FROM checker_results")
        conn.execute("DELETE FROM flags")
        conn.execute("DELETE FROM rounds")
        conn.execute(
            "UPDATE matches SET status = ? WHERE id = 1",
            (MatchState.CREATED.value,),
        )
        conn.commit()
        conn.close()

    def _post(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request)

    def test_get_health(self):
        url = f"{self.base_url}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["status"], "UP")
            self.assertEqual(body["database"], "connected")

    def test_get_match_state(self):
        url = f"{self.base_url}/match"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["match_id"], 1)
            self.assertEqual(body["status"], MatchState.CREATED.value)

    def test_post_match_state_transition(self):
        url = f"{self.base_url}/match/state"
        
        # 1. Valid Transition: CREATED -> RUNNING
        data = json.dumps({"status": "RUNNING"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["status"], "RUNNING")

        # 2. Idempotent Transition: RUNNING -> RUNNING
        data = json.dumps({"status": "RUNNING"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["status"], "RUNNING")

        # 3. Invalid Transition: RUNNING -> CREATED (should fail with 400)
        data = json.dumps({"status": "CREATED"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
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
        with urllib.request.urlopen(f"{self.base_url}/rounds/current") as resp:
            body = json.loads(resp.read().decode())
            self.assertEqual(body["round_number"], 1)
            self.assertEqual(body["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
