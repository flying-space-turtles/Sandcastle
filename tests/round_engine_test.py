#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
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
from checkers.runner import CheckerRunner
from models import FlagState, MatchState, RoundState
from tick_engine import (
    OperatorStateError,
    RoundEngineConfig,
    RoundEngineError,
    SecureFlagGenerator,
    TickEngine,
)


UTC = timezone.utc


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class SequenceFlagGenerator:
    def __init__(self) -> None:
        self.counter = 0

    def generate(self) -> str:
        self.counter += 1
        return f"FLAG{{{self.counter:032x}}}"


class FakeChecker:
    metadata = CheckerMetadata(
        name="fake-service",
        service_name="example-vuln",
        version="1.0",
        transport=Transport.TCP,
        default_port=8080,
        timeout_seconds=1.0,
    )

    def __init__(
        self,
        put_fail_teams: set[int] | None = None,
        check_fail_teams: set[int] | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.put_fail_teams = put_fail_teams or set()
        self.check_fail_teams = check_fail_teams or set()
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, int, str | None]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def put(self, request: PutRequest) -> CheckerOutcome:
        return self._call("PUT", request.context.target.team_id, request.flag)

    def get(self, request: GetRequest) -> CheckerOutcome:
        return self._call("GET", request.context.target.team_id, request.flag)

    def check(self, request: CheckRequest) -> CheckerOutcome:
        return self._call("CHECK", request.context.target.team_id, None)

    def _call(self, operation: str, team_id: int, flag: str | None) -> CheckerOutcome:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            with self._lock:
                self.calls.append((operation, team_id, flag))
            if operation == "PUT" and team_id in self.put_fail_teams:
                return CheckerOutcome(CheckerStatus.DOWN, "plant unavailable")
            if operation == "CHECK" and team_id in self.check_fail_teams:
                return CheckerOutcome(CheckerStatus.MUMBLE, "workflow failed")
            return CheckerOutcome(CheckerStatus.UP, f"{operation.lower()} ok")
        finally:
            with self._lock:
                self.active -= 1


class RoundEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.flag_generator = SequenceFlagGenerator()
        self.checker = FakeChecker()
        self._initialize_registry(team_count=2)

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def _initialize_registry(self, team_count: int) -> None:
        conn = db.get_db_connection(self.db_path)
        db.initialize_schema(conn)
        conn.execute("UPDATE matches SET status = ? WHERE id = 1", (MatchState.RUNNING.value,))
        for team_id in range(1, team_count + 1):
            conn.execute(
                "INSERT INTO teams (id, name, token, ip_address) VALUES (?, ?, ?, ?)",
                (team_id, f"Team {team_id}", f"token-{team_id}", f"10.10.{team_id}.3"),
            )
        conn.execute(
            "INSERT INTO services (id, name, port) VALUES (1, 'example-vuln', 8080)"
        )
        conn.commit()
        conn.close()

    def _engine(
        self,
        checker: FakeChecker | None = None,
        expiry_rounds: int = 2,
        max_concurrency: int = 2,
    ) -> TickEngine:
        plugin = checker or self.checker
        return TickEngine(
            db_path=self.db_path,
            config=RoundEngineConfig(
                duration_seconds=60,
                flag_expiry_rounds=expiry_rounds,
                max_concurrency=max_concurrency,
            ),
            plugin_provider=lambda _service_name: plugin,
            checker_master_secret="round-engine-test-secret",
            clock=self.clock,
            flag_generator=self.flag_generator,
        )

    def test_rounds_are_monotonic_unique_and_retry_idempotent(self) -> None:
        engine = self._engine()
        first = engine.tick()
        self.assertIsNotNone(first)
        self.assertEqual(first.round_number, 1)
        self.assertEqual(first.status, RoundState.COMPLETED)

        self.assertIsNone(engine.tick())
        retried = engine.resume_round(first.id)
        self.assertEqual(retried.id, first.id)
        self.assertEqual(len(self.checker.calls), 6)

        self.clock.advance(60)
        second = engine.tick()
        self.assertEqual(second.round_number, 2)

        conn = db.get_db_connection(self.db_path)
        rounds = conn.execute(
            "SELECT round_number FROM rounds WHERE match_id = 1 ORDER BY round_number"
        ).fetchall()
        flags = conn.execute(
            "SELECT flag FROM flags WHERE match_id = 1 ORDER BY id"
        ).fetchall()
        results = conn.execute("SELECT COUNT(*) FROM checker_results").fetchone()[0]
        conn.close()
        self.assertEqual(rounds, [(1,), (2,)])
        self.assertEqual(len(flags), 4)
        self.assertEqual(len({row[0] for row in flags}), 4)
        self.assertEqual(results, 12)

    def test_round_number_uniqueness_is_scoped_per_match(self) -> None:
        first = self._engine().tick()
        conn = db.get_db_connection(self.db_path)
        conn.execute("INSERT INTO matches (id, status) VALUES (2, ?)", (MatchState.CREATED.value,))
        conn.execute(
            """
            INSERT INTO rounds (
                match_id, round_number, status, started_at, deadline_at,
                completed_at, duration_seconds
            ) VALUES (2, 1, 'COMPLETED', ?, ?, ?, 60)
            """,
            (first.started_at, first.deadline_at, first.completed_at),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO rounds (
                    match_id, round_number, status, started_at, deadline_at,
                    completed_at, duration_seconds
                ) VALUES (1, 1, 'COMPLETED', ?, ?, ?, 60)
                """,
                (first.started_at, first.deadline_at, first.completed_at),
            )
        conn.close()

    def test_checker_failures_are_persisted_without_failing_round(self) -> None:
        checker = FakeChecker(put_fail_teams={1}, check_fail_teams={2})
        record = self._engine(checker=checker).tick()
        self.assertEqual(record.status, RoundState.COMPLETED)

        conn = db.get_db_connection(self.db_path)
        statuses = conn.execute(
            "SELECT team_id, operation, status FROM checker_results ORDER BY team_id, operation"
        ).fetchall()
        round_status = conn.execute("SELECT status FROM rounds WHERE id = ?", (record.id,)).fetchone()[0]
        flag_count = conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
        conn.close()
        self.assertIn((1, "PUT", "DOWN"), statuses)
        self.assertIn((2, "CHECK", "MUMBLE"), statuses)
        self.assertEqual(len(statuses), 6)
        self.assertEqual(round_status, RoundState.COMPLETED.value)
        self.assertEqual(flag_count, 2)

    def test_expiry_uses_persisted_round_numbers(self) -> None:
        engine = self._engine(expiry_rounds=2)
        engine.tick()
        self.clock.advance(60)
        engine.tick()
        self.clock.advance(60)
        engine.tick()

        conn = db.get_db_connection(self.db_path)
        lifecycle = conn.execute(
            """
            SELECT round_number, status, expires_after_round, expired_at
            FROM flags ORDER BY round_number, team_id
            """
        ).fetchall()
        conn.close()
        first_round = lifecycle[:2]
        later_rounds = lifecycle[2:]
        self.assertTrue(all(row[1] == FlagState.EXPIRED.value for row in first_round))
        self.assertTrue(all(row[2] == 3 for row in first_round))
        self.assertTrue(all(row[3] is not None for row in first_round))
        self.assertTrue(all(row[1] == FlagState.ACTIVE.value for row in later_rounds))

    def test_restart_resumes_same_persisted_round_and_flags(self) -> None:
        first_engine = self._engine()
        started = first_engine.start_round()
        started_again = first_engine.start_round()
        self.assertEqual(started_again.id, started.id)

        conn = db.get_db_connection(self.db_path)
        flag_row = conn.execute(
            """
            SELECT f.flag, t.id, t.ip_address, s.id, s.name, s.port
            FROM flags f JOIN teams t ON t.id = f.team_id
            JOIN services s ON s.id = f.service_id
            WHERE f.round_number = 1 ORDER BY t.id LIMIT 1
            """
        ).fetchone()
        target = ServiceTarget(flag_row[1], flag_row[3], flag_row[4], flag_row[2], flag_row[5])
        credentials = derive_checker_credentials(
            "round-engine-test-secret",
            target.team_id,
            target.service_name,
        )
        request = PutRequest(OperationContext(target, credentials, 1.0), flag_row[0])
        CheckerRunner().run(conn, self.checker, request, round_number=1, match_id=1)
        original_flags = conn.execute("SELECT flag FROM flags ORDER BY id").fetchall()
        # Recovery uses the persisted target snapshot, not a potentially changed
        # registry loaded during the restart.
        conn.execute("DELETE FROM teams")
        conn.execute("DELETE FROM services")
        conn.commit()
        conn.close()

        restarted_engine = self._engine()
        resumed = restarted_engine.tick()
        self.assertEqual(resumed.id, started.id)
        self.assertEqual(resumed.status, RoundState.COMPLETED)

        conn = db.get_db_connection(self.db_path)
        resumed_flags = conn.execute("SELECT flag FROM flags ORDER BY id").fetchall()
        result_count = conn.execute("SELECT COUNT(*) FROM checker_results").fetchone()[0]
        round_count = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        conn.close()
        self.assertEqual(resumed_flags, original_flags)
        self.assertEqual(result_count, 6)
        self.assertEqual(round_count, 1)
        put_calls_team1 = [
            call for call in self.checker.calls if call[0] == "PUT" and call[1] == 1
        ]
        self.assertEqual(len(put_calls_team1), 1)

    def test_checker_execution_is_bounded(self) -> None:
        os.unlink(self.db_path)
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._initialize_registry(team_count=6)
        checker = FakeChecker(delay_seconds=0.03)
        record = self._engine(checker=checker, max_concurrency=2).tick()
        self.assertEqual(record.status, RoundState.COMPLETED)
        self.assertGreater(checker.max_active, 1)
        self.assertLessEqual(checker.max_active, 2)

    def test_corrupt_running_snapshot_fails_deterministically(self) -> None:
        engine = self._engine()
        started = engine.start_round()
        conn = db.get_db_connection(self.db_path)
        conn.execute("DELETE FROM flags WHERE match_id = 1 AND round_number = 1")
        conn.commit()
        conn.close()

        with self.assertRaises(RoundEngineError):
            engine.tick()

        conn = db.get_db_connection(self.db_path)
        round_row = conn.execute(
            "SELECT status, error FROM rounds WHERE id = ?",
            (started.id,),
        ).fetchone()
        match_status = conn.execute("SELECT status FROM matches WHERE id = 1").fetchone()[0]
        round_count = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        conn.close()
        self.assertEqual(round_row[0], RoundState.FAILED.value)
        self.assertIn("no persisted target flags", round_row[1])
        self.assertEqual(match_status, MatchState.FAILED.value)
        self.assertEqual(round_count, 1)

    def test_single_step_requires_pause_and_does_not_resume_match(self) -> None:
        engine = self._engine()
        with self.assertRaises(OperatorStateError):
            engine.single_step()

        conn = db.get_db_connection(self.db_path)
        conn.execute("UPDATE matches SET status = ? WHERE id = 1", (MatchState.PAUSED.value,))
        conn.commit()
        conn.close()

        first = engine.single_step()
        second = engine.single_step()
        self.assertEqual((first.round_number, second.round_number), (1, 2))
        self.assertIsNone(engine.tick())
        conn = db.get_db_connection(self.db_path)
        state = conn.execute("SELECT status FROM matches WHERE id = 1").fetchone()[0]
        conn.close()
        self.assertEqual(state, MatchState.PAUSED.value)

    def test_secure_generator_shape_and_uniqueness(self) -> None:
        generator = SecureFlagGenerator()
        flags = {generator.generate() for _ in range(128)}
        self.assertEqual(len(flags), 128)
        self.assertTrue(all(re.fullmatch(r"FLAG\{[a-f0-9]{32}\}", flag) for flag in flags))


if __name__ == "__main__":
    unittest.main()
