from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import db
from checkers.contract import (
    CheckRequest,
    CheckerOperation,
    CheckerPlugin,
    CheckerResult,
    CheckerStatus,
    GetRequest,
    OperationContext,
    PutRequest,
    ServiceTarget,
)
from checkers.credentials import derive_checker_credentials
from checkers.loader import load_checker
from checkers.runner import CheckerRunner
from models import FlagState, MatchState, RoundState
from scoring import reconcile_score_events


logger = logging.getLogger("sandcastle.tick_engine")
UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime:
        ...

    def sleep(self, seconds: float) -> None:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FlagGenerator(Protocol):
    def generate(self) -> str:
        ...


class SecureFlagGenerator:
    def generate(self) -> str:
        return f"FLAG{{{secrets.token_hex(16)}}}"


class RoundEngineError(RuntimeError):
    pass


class OperatorStateError(RoundEngineError):
    pass


@dataclass(frozen=True)
class RoundEngineConfig:
    duration_seconds: int
    flag_expiry_rounds: int
    max_concurrency: int = 8
    poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("round duration must be positive")
        if self.flag_expiry_rounds <= 0:
            raise ValueError("flag expiry rounds must be positive")
        if self.max_concurrency <= 0:
            raise ValueError("checker max concurrency must be positive")
        if self.poll_seconds <= 0:
            raise ValueError("round poll interval must be positive")


@dataclass(frozen=True)
class RoundRecord:
    id: int
    match_id: int
    round_number: int
    status: RoundState
    started_at: str
    deadline_at: str
    completed_at: str | None
    duration_seconds: int
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "match_id": self.match_id,
            "round_number": self.round_number,
            "status": self.status.value,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


@dataclass(frozen=True)
class _TargetFlag:
    target: ServiceTarget
    flag: str


class FilesystemCheckerProvider:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def __call__(self, service_name: str) -> CheckerPlugin:
        return load_checker(self.root / service_name / "checker.py")


PluginProvider = Callable[[str], CheckerPlugin]


class TickEngine:
    def __init__(
        self,
        db_path: str,
        config: RoundEngineConfig,
        plugin_provider: PluginProvider,
        checker_master_secret: str,
        clock: Clock | None = None,
        flag_generator: FlagGenerator | None = None,
        checker_runner: CheckerRunner | None = None,
        match_id: int = 1,
    ) -> None:
        if not checker_master_secret:
            raise ValueError("checker master secret must be non-empty")
        if match_id <= 0:
            raise ValueError("match_id must be positive")
        self.db_path = db_path
        self.config = config
        self.plugin_provider = plugin_provider
        self.checker_master_secret = checker_master_secret
        self.clock = clock or SystemClock()
        self.flag_generator = flag_generator or SecureFlagGenerator()
        self.checker_runner = checker_runner or CheckerRunner()
        self.match_id = match_id
        self._lock = threading.Lock()

    def tick(self) -> RoundRecord | None:
        """Resume an active round or start one when its persisted deadline is due."""
        with self._lock:
            state = self._match_state()
            active = self._running_round()
            if active is not None:
                if state is MatchState.RUNNING:
                    return self._process_round(active)
                return active
            if state is not MatchState.RUNNING:
                return None

            latest = self.current_round()
            if latest is not None:
                if latest.status is RoundState.FAILED:
                    raise RoundEngineError("latest round failed; operator intervention required")
                if self.clock.now() < _parse_timestamp(latest.deadline_at):
                    return None

            return self._process_round(self._start_round())

    def single_step(self) -> RoundRecord:
        """Run exactly one round while the match remains paused."""
        with self._lock:
            if self._match_state() is not MatchState.PAUSED:
                raise OperatorStateError("single-step requires a PAUSED match")
            active = self._running_round()
            return self._process_round(active or self._start_round())

    def start_round(self) -> RoundRecord:
        """Persist a round and its target/flag snapshot without running checkers."""
        with self._lock:
            state = self._match_state()
            if state not in {MatchState.RUNNING, MatchState.PAUSED}:
                raise OperatorStateError("round creation requires a RUNNING or PAUSED match")
            return self._running_round() or self._start_round()

    def resume_round(self, round_id: int) -> RoundRecord:
        with self._lock:
            record = self._round_by_id(round_id)
            if record is None:
                raise RoundEngineError(f"round {round_id} does not exist")
            return self._process_round(record)

    def current_round(self) -> RoundRecord | None:
        with closing(self._connection()) as conn:
            row = conn.execute(
                """
                SELECT id, match_id, round_number, status, started_at, deadline_at,
                       completed_at, duration_seconds, error
                FROM rounds WHERE match_id = ? ORDER BY round_number DESC LIMIT 1
                """,
                (self.match_id,),
            ).fetchone()
        return _round_from_row(row) if row else None

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - scheduler boundary
                logger.exception("round scheduler tick failed")
            self.clock.sleep(self.config.poll_seconds)

    def _start_round(self) -> RoundRecord:
        now = self.clock.now().astimezone(UTC)
        started_at = _format_timestamp(now)
        deadline_at = _format_timestamp(
            now + timedelta(seconds=self.config.duration_seconds)
        )
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            active_row = conn.execute(
                """
                SELECT id, match_id, round_number, status, started_at, deadline_at,
                       completed_at, duration_seconds, error
                FROM rounds WHERE match_id = ? AND status = ?
                ORDER BY round_number DESC LIMIT 1
                """,
                (self.match_id, RoundState.RUNNING.value),
            ).fetchone()
            if active_row is not None:
                conn.commit()
                return _round_from_row(active_row)

            targets = conn.execute(
                """
                SELECT t.id, t.ip_address, s.id, s.name, s.port
                FROM teams t CROSS JOIN services s
                ORDER BY t.id, s.id
                """
            ).fetchall()
            if not targets:
                raise RoundEngineError("cannot start a round without teams and services")

            next_number = conn.execute(
                "SELECT COALESCE(MAX(round_number), 0) + 1 FROM rounds WHERE match_id = ?",
                (self.match_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO rounds (
                    match_id, round_number, status, started_at, deadline_at,
                    duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.match_id,
                    next_number,
                    RoundState.RUNNING.value,
                    started_at,
                    deadline_at,
                    self.config.duration_seconds,
                ),
            )
            round_id = int(cursor.lastrowid)

            conn.execute(
                """
                UPDATE flags SET status = ?, expired_at = ?
                WHERE match_id = ? AND status = ? AND expires_after_round <= ?
                """,
                (
                    FlagState.EXPIRED.value,
                    started_at,
                    self.match_id,
                    FlagState.ACTIVE.value,
                    next_number,
                ),
            )
            expires_after_round = next_number + self.config.flag_expiry_rounds
            for team_id, host, service_id, service_name, port in targets:
                self._insert_unique_flag(
                    conn,
                    team_id=team_id,
                    service_id=service_id,
                    target_host=host,
                    service_name=service_name,
                    service_port=port,
                    round_number=next_number,
                    expires_after_round=expires_after_round,
                    created_at=started_at,
                )
            conn.commit()
            return RoundRecord(
                id=round_id,
                match_id=self.match_id,
                round_number=next_number,
                status=RoundState.RUNNING,
                started_at=started_at,
                deadline_at=deadline_at,
                completed_at=None,
                duration_seconds=self.config.duration_seconds,
                error=None,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _insert_unique_flag(
        self,
        conn: sqlite3.Connection,
        team_id: int,
        service_id: int,
        target_host: str,
        service_name: str,
        service_port: int,
        round_number: int,
        expires_after_round: int,
        created_at: str,
    ) -> None:
        for _attempt in range(32):
            flag = self.flag_generator.generate()
            if not flag:
                raise RoundEngineError("flag generator returned an empty value")
            try:
                conn.execute(
                    """
                    INSERT INTO flags (
                        flag, match_id, team_id, service_id, round_number,
                        target_host, service_name, service_port,
                        status, expires_after_round, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        flag,
                        self.match_id,
                        team_id,
                        service_id,
                        round_number,
                        target_host,
                        service_name,
                        service_port,
                        FlagState.ACTIVE.value,
                        expires_after_round,
                        created_at,
                    ),
                )
                return
            except sqlite3.IntegrityError as exc:
                if "flags.flag" not in str(exc):
                    raise
        raise RoundEngineError("could not generate a unique flag after 32 attempts")

    def _process_round(self, record: RoundRecord) -> RoundRecord:
        if record.status is RoundState.COMPLETED:
            return record
        if record.status is RoundState.FAILED:
            raise RoundEngineError(f"round {record.round_number} is failed")

        try:
            targets = self._round_targets(record)
            if not targets:
                raise RoundEngineError("running round has no persisted target flags")
            self._run_phase(record, targets, (CheckerOperation.PUT,))
            self._run_phase(
                record,
                targets,
                (CheckerOperation.CHECK, CheckerOperation.GET),
            )
            return self._complete_round(record, len(targets) * 3)
        except Exception as exc:
            self._fail_round(record, exc)
            raise

    def _run_phase(
        self,
        record: RoundRecord,
        targets: list[_TargetFlag],
        operations: tuple[CheckerOperation, ...],
    ) -> None:
        jobs: list[tuple[_TargetFlag, CheckerOperation, dict[str, object]]] = []
        with closing(self._connection()) as conn:
            for target_flag in targets:
                for operation in operations:
                    exists = conn.execute(
                        """
                        SELECT 1 FROM checker_results
                        WHERE match_id = ? AND team_id = ? AND service_id = ?
                          AND round_number = ? AND operation = ?
                        """,
                        (
                            record.match_id,
                            target_flag.target.team_id,
                            target_flag.target.service_id,
                            record.round_number,
                            operation.value,
                        ),
                    ).fetchone()
                    if exists is not None:
                        continue
                    state: dict[str, object] = {}
                    if operation is CheckerOperation.GET:
                        put_row = conn.execute(
                            """
                            SELECT data_json FROM checker_results
                            WHERE match_id = ? AND team_id = ? AND service_id = ?
                              AND round_number = ? AND operation = 'PUT'
                            """,
                            (
                                record.match_id,
                                target_flag.target.team_id,
                                target_flag.target.service_id,
                                record.round_number,
                            ),
                        ).fetchone()
                        if put_row is not None:
                            state = json.loads(put_row[0])
                    jobs.append((target_flag, operation, state))

        if not jobs:
            return

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.config.max_concurrency
        ) as executor:
            futures = {
                executor.submit(self._execute_job, target_flag, operation, state): (
                    target_flag,
                    operation,
                )
                for target_flag, operation, state in jobs
            }
            with closing(self._connection()) as conn:
                for future in concurrent.futures.as_completed(futures):
                    target_flag, _operation = futures[future]
                    result = future.result()
                    db.persist_checker_result(
                        conn,
                        target=target_flag.target,
                        round_number=record.round_number,
                        result=result,
                        match_id=record.match_id,
                    )

    def _execute_job(
        self,
        target_flag: _TargetFlag,
        operation: CheckerOperation,
        state: dict[str, object],
    ) -> CheckerResult:
        started = time.monotonic()
        try:
            plugin = self.plugin_provider(target_flag.target.service_name)
            credentials = derive_checker_credentials(
                self.checker_master_secret,
                target_flag.target.team_id,
                target_flag.target.service_name,
            )
            context = OperationContext(
                target=target_flag.target,
                credentials=credentials,
                timeout_seconds=plugin.metadata.timeout_seconds,
            )
            if operation is CheckerOperation.PUT:
                request = PutRequest(context, target_flag.flag)
            elif operation is CheckerOperation.GET:
                request = GetRequest(context, target_flag.flag, state)
            else:
                request = CheckRequest(context)
            return self.checker_runner.execute(plugin, request)
        except Exception as exc:  # noqa: BLE001 - plugin loading boundary
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            return CheckerResult(
                plugin_name=target_flag.target.service_name,
                plugin_version="unavailable",
                operation=operation,
                status=CheckerStatus.MUMBLE,
                message=f"checker setup failed: {type(exc).__name__}",
                duration_ms=duration_ms,
                data={"failure": "configuration"},
            )

    def _complete_round(self, record: RoundRecord, expected_results: int) -> RoundRecord:
        completed_at = _format_timestamp(self.clock.now())
        with closing(self._connection()) as conn:
            actual_results = conn.execute(
                """
                SELECT COUNT(*) FROM checker_results
                WHERE match_id = ? AND round_number = ?
                """,
                (record.match_id, record.round_number),
            ).fetchone()[0]
            if actual_results != expected_results:
                raise RoundEngineError(
                    f"round result journal is incomplete: {actual_results}/{expected_results}"
                )
            conn.execute(
                """
                UPDATE rounds SET status = ?, completed_at = ?, error = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    RoundState.COMPLETED.value,
                    completed_at,
                    record.id,
                    RoundState.RUNNING.value,
                ),
            )
            conn.commit()
        try:
            with closing(self._connection()) as conn:
                reconcile_score_events(conn, match_id=record.match_id)
        except Exception:  # noqa: BLE001 - scoring can be repaired by replay
            logger.exception("could not reconcile score events for completed round")
        completed = self._round_by_id(record.id)
        if completed is None:
            raise RoundEngineError("completed round disappeared")
        return completed

    def _fail_round(self, record: RoundRecord, error: Exception) -> None:
        failed_at = _format_timestamp(self.clock.now())
        message = f"{type(error).__name__}: {error}"[:1000]
        try:
            with closing(self._connection()) as conn:
                conn.execute(
                    """
                    UPDATE rounds SET status = ?, completed_at = ?, error = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        RoundState.FAILED.value,
                        failed_at,
                        message,
                        record.id,
                        RoundState.RUNNING.value,
                    ),
                )
                conn.execute(
                    "UPDATE matches SET status = ? WHERE id = ?",
                    (MatchState.FAILED.value, record.match_id),
                )
                conn.commit()
        except sqlite3.Error:
            logger.exception("could not persist failed round state")

    def _round_targets(self, record: RoundRecord) -> list[_TargetFlag]:
        with closing(self._connection()) as conn:
            rows = conn.execute(
                """
                SELECT flag, team_id, target_host, service_id, service_name, service_port
                FROM flags f
                WHERE f.match_id = ? AND f.round_number = ?
                ORDER BY team_id, service_id
                """,
                (record.match_id, record.round_number),
            ).fetchall()
        return [
            _TargetFlag(
                target=ServiceTarget(
                    team_id=row[1],
                    service_id=row[3],
                    service_name=row[4],
                    host=row[2],
                    port=row[5],
                ),
                flag=row[0],
            )
            for row in rows
        ]

    def _match_state(self) -> MatchState:
        with closing(self._connection()) as conn:
            row = conn.execute(
                "SELECT status FROM matches WHERE id = ?",
                (self.match_id,),
            ).fetchone()
        if row is None:
            raise RoundEngineError(f"match {self.match_id} does not exist")
        return MatchState(row[0])

    def _running_round(self) -> RoundRecord | None:
        with closing(self._connection()) as conn:
            row = conn.execute(
                """
                SELECT id, match_id, round_number, status, started_at, deadline_at,
                       completed_at, duration_seconds, error
                FROM rounds WHERE match_id = ? AND status = ?
                ORDER BY round_number DESC LIMIT 1
                """,
                (self.match_id, RoundState.RUNNING.value),
            ).fetchone()
        return _round_from_row(row) if row else None

    def _round_by_id(self, round_id: int) -> RoundRecord | None:
        with closing(self._connection()) as conn:
            row = conn.execute(
                """
                SELECT id, match_id, round_number, status, started_at, deadline_at,
                       completed_at, duration_seconds, error
                FROM rounds WHERE id = ? AND match_id = ?
                """,
                (round_id, self.match_id),
            ).fetchone()
        return _round_from_row(row) if row else None

    def _connection(self) -> sqlite3.Connection:
        return db.get_db_connection(self.db_path)


def build_tick_engine(
    db_path: str | None = None,
    config_path: str | None = None,
) -> TickEngine:
    values = db.parse_arena_config(config_path or db.get_config_path())
    config = RoundEngineConfig(
        duration_seconds=int(values.get("ARENA_ROUND_DURATION_SECONDS", "120")),
        flag_expiry_rounds=int(values.get("ARENA_FLAG_EXPIRY_ROUNDS", "5")),
        max_concurrency=int(values.get("ARENA_CHECKER_MAX_CONCURRENCY", "8")),
        poll_seconds=float(os.environ.get("ROUND_POLL_SECONDS", "1")),
    )
    master_secret = os.environ.get("CHECKER_MASTER_SECRET") or values.get(
        "ARENA_CHECKER_SECRET",
        "",
    )
    default_checker_root = Path(__file__).resolve().parents[1] / "services"
    checker_root = Path(os.environ.get("CHECKER_ROOT", str(default_checker_root)))
    return TickEngine(
        db_path=db_path or db.get_db_path(),
        config=config,
        plugin_provider=FilesystemCheckerProvider(checker_root),
        checker_master_secret=master_secret,
    )


def _round_from_row(row: tuple) -> RoundRecord:
    return RoundRecord(
        id=row[0],
        match_id=row[1],
        round_number=row[2],
        status=RoundState(row[3]),
        started_at=row[4],
        deadline_at=row[5],
        completed_at=row[6],
        duration_seconds=row[7],
        error=row[8],
    )


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
