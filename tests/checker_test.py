#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gameserver"))

import db
from checkers.contract import (
    CheckRequest,
    CheckerMetadata,
    CheckerOutcome,
    CheckerStatus,
    GetRequest,
    OperationContext,
    PutRequest,
    ServiceTarget,
    Transport,
)
from checkers.credentials import derive_checker_credentials
from checkers.loader import load_checker
from checkers.runner import CheckerRunner


class StubChecker:
    metadata = CheckerMetadata(
        name="stub",
        service_name="example-vuln",
        version="1.0",
        transport=Transport.TCP,
        default_port=9000,
        timeout_seconds=0.05,
    )

    def __init__(
        self,
        outcome: CheckerOutcome | None = None,
        error: Exception | None = None,
        delay: float = 0,
    ) -> None:
        self.outcome = outcome or CheckerOutcome(CheckerStatus.UP, "ok")
        self.error = error
        self.delay = delay

    def _run(self) -> CheckerOutcome:
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.outcome

    def put(self, request: PutRequest) -> CheckerOutcome:
        return self._run()

    def get(self, request: GetRequest) -> CheckerOutcome:
        return self._run()

    def check(self, request: CheckRequest) -> CheckerOutcome:
        return self._run()


class FlaskSession:
    def __init__(self, client) -> None:
        self.client = client

    @staticmethod
    def _response(response) -> tuple[int, str]:
        return response.status_code, response.get_data(as_text=True)

    def get(self, path: str) -> tuple[int, str]:
        return self._response(self.client.get(path, follow_redirects=True))

    def post_form(self, path: str, values: dict[str, str]) -> tuple[int, str]:
        return self._response(
            self.client.post(path, data=values, follow_redirects=True)
        )

    def post_json(
        self,
        path: str,
        values: dict[str, str],
        headers: dict[str, str],
    ) -> tuple[int, str]:
        return self._response(
            self.client.post(path, json=values, headers=headers, follow_redirects=True)
        )


class CheckerRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.get_db_connection(":memory:")
        db.initialize_schema(self.conn)
        self.conn.execute(
            "INSERT INTO teams (id, name, token, ip_address) VALUES (1, 'Team 1', 't1', '127.0.0.1')"
        )
        self.conn.execute(
            "INSERT INTO services (id, name, port) VALUES (1, 'example-vuln', 8080)"
        )
        self.conn.commit()
        self.target = ServiceTarget(1, 1, "example-vuln", "127.0.0.1", 8080)
        self.credentials = derive_checker_credentials("test-master-secret", 1, "example-vuln")
        self.context = OperationContext(self.target, self.credentials, 0.05)
        self.runner = CheckerRunner()

    def tearDown(self) -> None:
        self.conn.close()

    def _persisted_status(self, round_number: int) -> str:
        row = self.conn.execute(
            "SELECT status FROM checker_results WHERE round_number = ?",
            (round_number,),
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_every_checker_status_is_structured_and_persisted(self) -> None:
        cases = (
            (1, StubChecker(CheckerOutcome(CheckerStatus.UP, "healthy")), CheckerStatus.UP),
            (2, StubChecker(error=ConnectionRefusedError()), CheckerStatus.DOWN),
            (3, StubChecker(error=ValueError("bad payload")), CheckerStatus.MUMBLE),
            (
                4,
                StubChecker(CheckerOutcome(CheckerStatus.CORRUPT, "flag missing")),
                CheckerStatus.CORRUPT,
            ),
        )
        for round_number, plugin, expected in cases:
            with self.subTest(status=expected):
                result = self.runner.run(
                    self.conn,
                    plugin,
                    CheckRequest(self.context),
                    round_number,
                )
                self.assertEqual(result.status, expected)
                self.assertEqual(self._persisted_status(round_number), expected.value)

    def test_timeout_maps_to_down(self) -> None:
        result = self.runner.run(
            self.conn,
            StubChecker(delay=0.2),
            CheckRequest(self.context),
            5,
        )
        self.assertEqual(result.status, CheckerStatus.DOWN)
        self.assertEqual(result.data["failure"], "timeout")

    def test_put_get_and_check_are_persisted_independently(self) -> None:
        plugin = StubChecker()
        requests = (
            PutRequest(self.context, "FLAG{00000000000000000000000000000000}"),
            GetRequest(self.context, "FLAG{00000000000000000000000000000000}"),
            CheckRequest(self.context),
        )
        for request in requests:
            self.runner.run(self.conn, plugin, request, 6)

        rows = self.conn.execute(
            "SELECT operation, status, data_json FROM checker_results "
            "WHERE round_number = 6 ORDER BY operation"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["CHECK", "GET", "PUT"])
        self.assertTrue(all(row[1] == "UP" for row in rows))
        self.assertTrue(all(row[2] == "{}" for row in rows))

    def test_credentials_are_team_and_service_scoped(self) -> None:
        other_team = derive_checker_credentials("test-master-secret", 2, "example-vuln")
        other_service = derive_checker_credentials("test-master-secret", 1, "other-service")
        self.assertNotEqual(
            self.credentials.require("plant_token"),
            other_team.require("plant_token"),
        )
        self.assertNotEqual(
            self.credentials.require("password"),
            other_service.require("password"),
        )

        wrong_context = OperationContext(self.target, other_team, 0.05)
        result = self.runner.run(
            self.conn,
            StubChecker(),
            CheckRequest(wrong_context),
            7,
        )
        self.assertEqual(result.status, CheckerStatus.MUMBLE)

    def test_legacy_checker_results_are_migrated(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE services (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO teams VALUES (1)")
        conn.execute("INSERT INTO services VALUES (1)")
        conn.execute(
            """
            CREATE TABLE checker_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, service_id, round_number)
            )
            """
        )
        conn.execute(
            "INSERT INTO checker_results (team_id, service_id, round_number, status, details) "
            "VALUES (1, 1, 1, 'UP', 'legacy result')"
        )
        db.initialize_schema(conn)
        row = conn.execute(
            "SELECT operation, plugin_name, message FROM checker_results"
        ).fetchone()
        self.assertEqual(row, ("CHECK", "legacy", "legacy result"))
        conn.close()


class TurtleNotesCheckerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.master_secret = "integration-checker-master-secret"
        self.credentials = derive_checker_credentials(
            self.master_secret,
            1,
            "example-vuln",
        )
        self.old_environment = os.environ.copy()
        os.environ.update(
            {
                "DATA_DIR": str(self.data_dir),
                "DB_PATH": str(self.data_dir / "app.db"),
                "FLAG_FILE": str(self.data_dir / "flag.txt"),
                "NOTES_DIR": str(self.data_dir / "notes"),
                "TEAM_ID": "1",
                "TEAM_NAME": "Team 1",
                "CHECKER_USERNAME": self.credentials.require("username"),
                "CHECKER_PASSWORD": self.credentials.require("password"),
                "PLANT_TOKEN": self.credentials.require("plant_token"),
            }
        )

        module_path = ROOT / "services" / "example-vuln" / "app" / "app.py"
        spec = importlib.util.spec_from_file_location("checker_test_turtlenotes_app", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load TurtleNotes app")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.app_module = module

        self.conn = db.get_db_connection(":memory:")
        db.initialize_schema(self.conn)
        self.conn.execute(
            "INSERT INTO teams (id, name, token, ip_address) VALUES (1, 'Team 1', 't1', '127.0.0.1')"
        )
        self.conn.execute(
            "INSERT INTO services (id, name, port) VALUES (1, 'example-vuln', ?)",
            (8080,),
        )
        self.conn.commit()

        self.checker = load_checker(ROOT / "services" / "example-vuln" / "checker.py")
        self.checker._session_factory = lambda _base_url, _timeout: FlaskSession(
            module.app.test_client()
        )
        target = ServiceTarget(
            1,
            1,
            "example-vuln",
            "127.0.0.1",
            8080,
        )
        self.context = OperationContext(target, self.credentials, 2.0)
        self.runner = CheckerRunner()

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_environment)
        sys.modules.pop("checker_test_turtlenotes_app", None)
        self.temp_dir.cleanup()

    def test_real_put_get_check_and_corrupt_workflow(self) -> None:
        flag = "FLAG{1234567890abcdef1234567890abcdef}"
        put_result = self.runner.run(
            self.conn,
            self.checker,
            PutRequest(self.context, flag),
            1,
        )
        self.assertEqual(put_result.status, CheckerStatus.UP)

        get_result = self.runner.run(
            self.conn,
            self.checker,
            GetRequest(self.context, flag, put_result.data),
            1,
        )
        self.assertEqual(get_result.status, CheckerStatus.UP)

        check_result = self.runner.run(
            self.conn,
            self.checker,
            CheckRequest(self.context),
            1,
        )
        self.assertEqual(check_result.status, CheckerStatus.UP)
        self.assertIn("create-note", check_result.data["checks"])

        service_db = sqlite3.connect(self.data_dir / "app.db")
        checker_username = self.credentials.require("username")
        public_notes = service_db.execute(
            "SELECT COUNT(*) FROM notes n JOIN users u ON u.id = n.owner_id "
            "WHERE u.username = ? AND n.is_secret = 0",
            (checker_username,),
        ).fetchone()[0]
        self.assertGreaterEqual(public_notes, 1)
        service_db.execute(
            "UPDATE notes SET body = 'flag lost' WHERE owner_id = "
            "(SELECT id FROM users WHERE username = ?) AND is_secret = 1",
            (checker_username,),
        )
        service_db.commit()
        service_db.close()

        corrupt_result = self.runner.run(
            self.conn,
            self.checker,
            GetRequest(self.context, flag, put_result.data),
            2,
        )
        self.assertEqual(corrupt_result.status, CheckerStatus.CORRUPT)

        rows = self.conn.execute(
            "SELECT operation, status FROM checker_results ORDER BY round_number, operation"
        ).fetchall()
        self.assertEqual(
            rows,
            [("CHECK", "UP"), ("GET", "UP"), ("PUT", "UP"), ("GET", "CORRUPT")],
        )


if __name__ == "__main__":
    unittest.main()
