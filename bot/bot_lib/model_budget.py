"""Persistent hard-budget reservations for Sandcastle model calls."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent_contracts import BudgetRejection, ModelProvider, ModelRequest, ModelUsage
from .model_gateway import GatewayResult, ModelGateway

ACTIVE_COST_STATUSES = ("RESERVED", "COMPLETED", "FAILED")
COUNTED_CALL_STATUSES = ("RESERVED", "COMPLETED", "FAILED")


class ModelBudgetExceeded(RuntimeError):
    def __init__(self, rejection: BudgetRejection) -> None:
        super().__init__(
            f"{rejection.code}: {rejection.scope} budget would exceed "
            f"{rejection.current} + {rejection.requested} > {rejection.limit}"
        )
        self.rejection = rejection


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    reserved_cost_usd: float
    created_at_epoch: float


class ModelBudgetLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        stale_after_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.path = str(path)
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_usage_reservations (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    agent_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    match_id INTEGER,
                    round_number INTEGER,
                    team_id INTEGER,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_cost_usd REAL NOT NULL,
                    actual_cost_usd REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    provider_request_id TEXT,
                    created_at_epoch REAL NOT NULL,
                    updated_at_epoch REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS model_usage_run_idx
                ON model_usage_reservations(run_id, status);

                CREATE INDEX IF NOT EXISTS model_usage_match_idx
                ON model_usage_reservations(match_id, status);

                CREATE INDEX IF NOT EXISTS model_usage_day_idx
                ON model_usage_reservations(utc_day, status);
                """
            )

    @staticmethod
    def _day(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()

    @staticmethod
    def _effective_cost_sql() -> str:
        return "CASE WHEN actual_cost_usd IS NULL THEN reserved_cost_usd ELSE actual_cost_usd END"

    def _recover_stale(self, conn: sqlite3.Connection, now: float) -> int:
        cursor = conn.execute(
            """
            UPDATE model_usage_reservations
            SET status = 'RELEASED', updated_at_epoch = ?
            WHERE status = 'RESERVED' AND updated_at_epoch < ?
            """,
            (now, now - self.stale_after_seconds),
        )
        return cursor.rowcount

    def recover_stale(self) -> int:
        now = float(self.clock())
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = self._recover_stale(conn, now)
            conn.commit()
            return count

    def _count(self, conn: sqlite3.Connection, where: str, parameters: tuple[object, ...]) -> int:
        placeholders = ",".join("?" for _ in COUNTED_CALL_STATUSES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM model_usage_reservations
            WHERE {where} AND status IN ({placeholders})
            """,
            (*parameters, *COUNTED_CALL_STATUSES),
        ).fetchone()
        return int(row[0])

    def _cost(self, conn: sqlite3.Connection, where: str, parameters: tuple[object, ...]) -> float:
        placeholders = ",".join("?" for _ in ACTIVE_COST_STATUSES)
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM({self._effective_cost_sql()}), 0.0)
            FROM model_usage_reservations
            WHERE {where} AND status IN ({placeholders})
            """,
            (*parameters, *ACTIVE_COST_STATUSES),
        ).fetchone()
        return float(row[0])

    @staticmethod
    def _reject(code: str, scope: str, limit: float, current: float, requested: float) -> None:
        raise ModelBudgetExceeded(
            BudgetRejection(
                code=code,
                scope=scope,
                limit=limit,
                current=current,
                requested=requested,
            )
        )

    def reserve(
        self,
        request: ModelRequest,
        *,
        provider: ModelProvider,
        model_id: str,
        estimated_cost_usd: float | None = None,
    ) -> BudgetReservation:
        estimated = (
            request.budget.max_cost_usd_per_call
            if estimated_cost_usd is None
            else float(estimated_cost_usd)
        )
        if estimated < 0:
            raise ValueError("estimated_cost_usd must be non-negative")
        policy = request.budget
        if estimated > policy.max_cost_usd_per_call:
            self._reject(
                "CALL_COST_LIMIT",
                "call",
                policy.max_cost_usd_per_call,
                0,
                estimated,
            )

        now = float(self.clock())
        utc_day = self._day(now)
        reservation_id = uuid.uuid4().hex
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_stale(conn, now)

            round_calls = self._count(
                conn,
                "run_id = ? AND round_number IS ?",
                (request.run_id, request.round_number),
            )
            if round_calls >= policy.max_calls_per_round:
                self._reject(
                    "ROUND_CALL_LIMIT",
                    "round",
                    policy.max_calls_per_round,
                    round_calls,
                    1,
                )

            run_calls = self._count(conn, "run_id = ?", (request.run_id,))
            if run_calls >= policy.max_calls_per_match:
                self._reject(
                    "RUN_CALL_LIMIT",
                    "run",
                    policy.max_calls_per_match,
                    run_calls,
                    1,
                )

            run_cost = self._cost(conn, "run_id = ?", (request.run_id,))
            if run_cost + estimated > policy.max_cost_usd_per_match:
                self._reject(
                    "RUN_COST_LIMIT",
                    "run",
                    policy.max_cost_usd_per_match,
                    run_cost,
                    estimated,
                )

            if request.match_id is not None:
                match_calls = self._count(conn, "match_id = ?", (request.match_id,))
                if match_calls >= policy.max_calls_per_match:
                    self._reject(
                        "MATCH_CALL_LIMIT",
                        "match",
                        policy.max_calls_per_match,
                        match_calls,
                        1,
                    )
                match_cost = self._cost(conn, "match_id = ?", (request.match_id,))
                if match_cost + estimated > policy.max_cost_usd_per_match:
                    self._reject(
                        "MATCH_COST_LIMIT",
                        "match",
                        policy.max_cost_usd_per_match,
                        match_cost,
                        estimated,
                    )

            day_cost = self._cost(conn, "utc_day = ?", (utc_day,))
            if day_cost + estimated > policy.max_cost_usd_per_day:
                self._reject(
                    "DAY_COST_LIMIT",
                    "day",
                    policy.max_cost_usd_per_day,
                    day_cost,
                    estimated,
                )

            conn.execute(
                """
                INSERT INTO model_usage_reservations (
                    id, agent_id, agent_type, run_id, match_id, round_number,
                    team_id, provider, model_id, utc_day, status,
                    reserved_cost_usd, created_at_epoch, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?)
                """,
                (
                    reservation_id,
                    request.agent_id,
                    request.agent_type.value,
                    request.run_id,
                    request.match_id,
                    request.round_number,
                    request.team_id,
                    provider.value,
                    model_id,
                    utc_day,
                    estimated,
                    now,
                    now,
                ),
            )
            conn.commit()
        return BudgetReservation(reservation_id, estimated, now)

    def reconcile(self, reservation_id: str, usage: ModelUsage) -> None:
        now = float(self.clock())
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, reserved_cost_usd
                FROM model_usage_reservations WHERE id = ?
                """,
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown model budget reservation")
            if row["status"] != "RESERVED":
                raise ValueError("model budget reservation is not active")
            actual_cost = (
                float(usage.cost_usd)
                if usage.cost_usd is not None
                else float(row["reserved_cost_usd"])
            )
            conn.execute(
                """
                UPDATE model_usage_reservations
                SET status = 'COMPLETED', actual_cost_usd = ?,
                    input_tokens = ?, output_tokens = ?,
                    provider_request_id = ?, updated_at_epoch = ?
                WHERE id = ?
                """,
                (
                    actual_cost,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.provider_request_id,
                    now,
                    reservation_id,
                ),
            )
            conn.commit()

    def fail(self, reservation_id: str, *, charge_reserved: bool = True) -> None:
        now = float(self.clock())
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, reserved_cost_usd FROM model_usage_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown model budget reservation")
            if row["status"] != "RESERVED":
                raise ValueError("model budget reservation is not active")
            conn.execute(
                """
                UPDATE model_usage_reservations
                SET status = 'FAILED', actual_cost_usd = ?, updated_at_epoch = ?
                WHERE id = ?
                """,
                (
                    float(row["reserved_cost_usd"]) if charge_reserved else 0.0,
                    now,
                    reservation_id,
                ),
            )
            conn.commit()

    def summary(
        self,
        *,
        run_id: str | None = None,
        match_id: int | None = None,
        utc_day: str | None = None,
    ) -> dict[str, Any]:
        filters: list[str] = []
        parameters: list[object] = []
        if run_id is not None:
            filters.append("run_id = ?")
            parameters.append(run_id)
        if match_id is not None:
            filters.append("match_id = ?")
            parameters.append(match_id)
        if utc_day is not None:
            filters.append("utc_day = ?")
            parameters.append(utc_day)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT status, COUNT(*) AS calls,
                       COALESCE(SUM({self._effective_cost_sql()}), 0.0) AS cost,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM model_usage_reservations
                {where}
                GROUP BY status
                ORDER BY status
                """,
                tuple(parameters),
            ).fetchall()
        statuses = {
            row["status"]: {
                "calls": int(row["calls"]),
                "cost_usd": round(float(row["cost"]), 8),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
            }
            for row in rows
        }
        return {
            "run_id": run_id,
            "match_id": match_id,
            "utc_day": utc_day,
            "statuses": statuses,
            "total_calls": sum(item["calls"] for item in statuses.values()),
            "total_cost_usd": round(sum(item["cost_usd"] for item in statuses.values()), 8),
        }


class BudgetedModelGateway:
    """Reserve cost before issuing a gateway request, then reconcile usage."""

    def __init__(self, gateway: ModelGateway, ledger: ModelBudgetLedger) -> None:
        self.gateway = gateway
        self.ledger = ledger

    def call(
        self,
        request: ModelRequest,
        *,
        model_id: str,
        estimated_cost_usd: float | None = None,
    ) -> GatewayResult:
        reservation = self.ledger.reserve(
            request,
            provider=self.gateway.primary_provider,
            model_id=model_id,
            estimated_cost_usd=estimated_cost_usd,
        )
        try:
            result = self.gateway.call(request)
        except Exception:
            self.ledger.fail(reservation.reservation_id)
            raise
        self.ledger.reconcile(reservation.reservation_id, result.response.usage)
        return result
