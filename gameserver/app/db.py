"""SQLite-backed persistence for the gameserver."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterable, Iterator

from .config import CONFIG

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    ip_address  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    flag          TEXT NOT NULL UNIQUE,
    team_id       INTEGER NOT NULL REFERENCES teams(id),
    note_id       INTEGER,
    round         INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    expired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_flags_round ON flags(round);
CREATE INDEX IF NOT EXISTS idx_flags_team ON flags(team_id);

CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id       INTEGER NOT NULL REFERENCES flags(id),
    attacker_id   INTEGER NOT NULL REFERENCES teams(id),
    submitted_at  REAL NOT NULL,
    UNIQUE(flag_id, attacker_id)
);
CREATE INDEX IF NOT EXISTS idx_submissions_attacker ON submissions(attacker_id);

CREATE TABLE IF NOT EXISTS sla_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    round       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    details     TEXT,
    checked_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sla_checks_team_round ON sla_checks(team_id, round);

CREATE TABLE IF NOT EXISTS scores (
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    round       INTEGER NOT NULL,
    attack_pts  REAL NOT NULL DEFAULT 0,
    defense_pts REAL NOT NULL DEFAULT 0,
    sla_pts     REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (team_id, round)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL NOT NULL,
    round        INTEGER,
    kind         TEXT NOT NULL,
    team_id      INTEGER,
    message      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ---- Teams -------------------------------------------------------

    def upsert_team(self, team_id: int, name: str, ip_address: str) -> None:
        token = uuid.uuid4().hex
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO teams (id, name, token, ip_address)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    ip_address = excluded.ip_address
                """,
                (team_id, name, token, ip_address),
            )

    def list_teams(self) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            return cur.execute(
                "SELECT * FROM teams ORDER BY id ASC"
            ).fetchall()

    def find_team_by_token(self, token: str) -> sqlite3.Row | None:
        with self.cursor() as cur:
            return cur.execute(
                "SELECT * FROM teams WHERE token = ?", (token,)
            ).fetchone()

    # ---- State -------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    # ---- Flags -------------------------------------------------------

    def insert_flag(
        self,
        flag: str,
        team_id: int,
        round_no: int,
        note_id: int | None = None,
    ) -> int:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO flags (flag, team_id, note_id, round, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (flag, team_id, note_id, round_no, time.time()),
            )
            return int(cur.lastrowid or 0)

    def latest_flag_for_team(self, team_id: int) -> sqlite3.Row | None:
        with self.cursor() as cur:
            return cur.execute(
                "SELECT * FROM flags WHERE team_id = ? "
                "ORDER BY round DESC LIMIT 1",
                (team_id,),
            ).fetchone()

    def find_flag(self, flag: str) -> sqlite3.Row | None:
        with self.cursor() as cur:
            return cur.execute(
                "SELECT * FROM flags WHERE flag = ?", (flag,)
            ).fetchone()

    def expire_flags_older_than(self, current_round: int, expiry_rounds: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE flags SET expired = 1 "
                "WHERE round <= ? AND expired = 0",
                (current_round - expiry_rounds,),
            )

    # ---- Submissions -------------------------------------------------

    def record_submission(self, flag_id: int, attacker_id: int) -> bool:
        try:
            with self.cursor() as cur:
                cur.execute(
                    "INSERT INTO submissions (flag_id, attacker_id, submitted_at) "
                    "VALUES (?, ?, ?)",
                    (flag_id, attacker_id, time.time()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def submissions_for_flag(self, flag_id: int) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            return cur.execute(
                "SELECT * FROM submissions WHERE flag_id = ?",
                (flag_id,),
            ).fetchall()

    # ---- SLA ---------------------------------------------------------

    def record_sla(
        self,
        team_id: int,
        round_no: int,
        status: str,
        details: str | None = None,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO sla_checks (team_id, round, status, details, checked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, status, details, time.time()),
            )

    def latest_sla_per_team(self) -> dict[int, sqlite3.Row]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT s.* FROM sla_checks s "
                "JOIN ( "
                "  SELECT team_id, MAX(checked_at) AS m "
                "  FROM sla_checks GROUP BY team_id "
                ") t ON s.team_id = t.team_id AND s.checked_at = t.m"
            ).fetchall()
            return {row["team_id"]: row for row in rows}

    # ---- Scoring -----------------------------------------------------

    def upsert_score(
        self,
        team_id: int,
        round_no: int,
        attack: float,
        defense: float,
        sla: float,
        total: float,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scores (team_id, round, attack_pts, defense_pts, sla_pts, total)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, round) DO UPDATE SET
                    attack_pts = excluded.attack_pts,
                    defense_pts = excluded.defense_pts,
                    sla_pts = excluded.sla_pts,
                    total = excluded.total
                """,
                (team_id, round_no, attack, defense, sla, total),
            )

    def cumulative_scores(self) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            return cur.execute(
                """
                SELECT t.id AS team_id, t.name,
                       COALESCE(SUM(s.attack_pts), 0)  AS attack,
                       COALESCE(SUM(s.defense_pts), 0) AS defense,
                       COALESCE(SUM(s.sla_pts), 0)     AS sla,
                       COALESCE(SUM(s.total), 0)       AS total
                FROM teams t LEFT JOIN scores s ON t.id = s.team_id
                GROUP BY t.id
                ORDER BY total DESC, t.id ASC
                """
            ).fetchall()

    # ---- Events ------------------------------------------------------

    def add_event(
        self,
        kind: str,
        message: str,
        round_no: int | None = None,
        team_id: int | None = None,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO events (created_at, round, kind, team_id, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), round_no, kind, team_id, message),
            )

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            return cur.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()


def open_db() -> Database:
    return Database(CONFIG.db_path)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(zip(r.keys(), tuple(r))) for r in rows]
