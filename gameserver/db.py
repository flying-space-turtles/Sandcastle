from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from typing import Dict, Optional

from checkers.contract import CheckerResult, ServiceTarget
from models import FlagState, MatchState, RoundState
from security import hash_team_token, verify_team_token


DEFAULT_DB_PATH = "/app/data/gameserver.db"
DEFAULT_CONFIG_PATH = "/app/config/arena.env"
DEFAULT_SCORING_POLICY_VERSION = "sandcastle-v1"
DEFAULT_ATTACK_POINTS = 10.0
DEFAULT_DEFENSE_POINTS = 2.0
DEFAULT_SLA_POINTS = 1.0


def get_db_path() -> str:
    return os.environ.get("GAMESERVER_DB_PATH", DEFAULT_DB_PATH)


def get_config_path() -> str:
    return os.environ.get("ARENA_CONFIG_FILE", DEFAULT_CONFIG_PATH)


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = get_db_path()

    # Ensure parent directory exists for local files
    if db_path != ":memory:":
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def check_db_readiness(db_path: Optional[str] = None) -> bool:
    """Check database health by running a simple query."""
    conn = None
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT 1;")
        cursor.fetchone()
        return True
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def restart_match(conn: sqlite3.Connection, match_id: int = 1) -> tuple:
    """Reset a finished or failed match while preserving registry and policy."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, status, created_at, updated_at
            FROM matches WHERE id = ?
            """,
            (match_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"match {match_id} does not exist")
        if row[1] not in {MatchState.FINISHED.value, MatchState.FAILED.value}:
            raise ValueError("match restart requires a FINISHED or FAILED match")

        conn.execute("DELETE FROM score_events WHERE match_id = ?", (match_id,))
        conn.execute(
            """
            DELETE FROM submissions
            WHERE flag IN (SELECT flag FROM flags WHERE match_id = ?)
            """,
            (match_id,),
        )
        conn.execute("DELETE FROM checker_results WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM flags WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM rounds WHERE match_id = ?", (match_id,))
        conn.execute(
            """
            UPDATE matches
            SET status = ?, created_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (MatchState.CREATED.value, match_id),
        )
        restarted = conn.execute(
            """
            SELECT id, status, created_at, updated_at
            FROM matches WHERE id = ?
            """,
            (match_id,),
        ).fetchone()
        conn.commit()
        if restarted is None:
            raise RuntimeError("restarted match could not be read back")
        return restarted
    except Exception:
        conn.rollback()
        raise


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Initialize SQLite tables deterministically if they do not exist."""
    cursor = conn.cursor()

    # Matches table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            scoring_policy_version TEXT NOT NULL DEFAULT 'sandcastle-v1',
            attack_points REAL NOT NULL DEFAULT 10.0 CHECK(attack_points >= 0),
            defense_points REAL NOT NULL DEFAULT 2.0 CHECK(defense_points >= 0),
            sla_points REAL NOT NULL DEFAULT 1.0 CHECK(sla_points >= 0),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    _initialize_match_scoring_schema(conn)

    # Teams table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            token TEXT NOT NULL UNIQUE,
            ip_address TEXT NOT NULL UNIQUE
        );
    """)

    # Services table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            port INTEGER NOT NULL
        );
    """)

    _initialize_rounds_schema(conn)
    _initialize_flags_schema(conn)

    _initialize_checker_results_schema(conn)

    # Submissions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flag TEXT NOT NULL,
            attacker_id INTEGER NOT NULL,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL,
            FOREIGN KEY(attacker_id) REFERENCES teams(id) ON DELETE CASCADE,
            UNIQUE(flag, attacker_id)
        );
    """)

    _initialize_score_events_schema(conn)
    _migrate_legacy_scoring_policy(conn)

    # Trigger to auto-update matches.updated_at
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS update_matches_timestamp
        AFTER UPDATE ON matches
        BEGIN
            UPDATE matches SET updated_at = CURRENT_TIMESTAMP WHERE id = new.id;
        END;
    """)

    # Initialize a default match (ID=1) if none exists, in CREATED state
    cursor.execute("SELECT COUNT(*) FROM matches WHERE id = 1;")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO matches (id, status) VALUES (1, ?);",
            (MatchState.CREATED.value,)
        )

    conn.commit()


def _create_checker_results_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checker_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('PUT', 'GET', 'CHECK')),
            plugin_name TEXT NOT NULL,
            plugin_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('UP', 'DOWN', 'MUMBLE', 'CORRUPT')),
            message TEXT NOT NULL,
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            data_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
            UNIQUE(match_id, team_id, service_id, round_number, operation)
        );
    """)


def _initialize_match_scoring_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(matches)").fetchall()}
    additions = (
        (
            "scoring_policy_version",
            "TEXT NOT NULL DEFAULT 'legacy-submission-v0'",
        ),
        ("attack_points", "REAL NOT NULL DEFAULT 10.0 CHECK(attack_points >= 0)"),
        ("defense_points", "REAL NOT NULL DEFAULT 2.0 CHECK(defense_points >= 0)"),
        ("sla_points", "REAL NOT NULL DEFAULT 1.0 CHECK(sla_points >= 0)"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {name} {definition}")


def _initialize_checker_results_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'checker_results'"
    ).fetchone()
    if existing is None:
        _create_checker_results_table(conn)
        return

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(checker_results)").fetchall()
    }
    if "operation" in columns and "match_id" in columns:
        return

    conn.execute("ALTER TABLE checker_results RENAME TO checker_results_legacy")
    _create_checker_results_table(conn)
    if "operation" in columns:
        conn.execute("""
            INSERT INTO checker_results (
                id, match_id, team_id, service_id, round_number, operation,
                plugin_name, plugin_version, status, message,
                duration_ms, data_json, created_at
            )
            SELECT
                id, 1, team_id, service_id, round_number, operation,
                plugin_name, plugin_version, status, message,
                duration_ms, data_json, created_at
            FROM checker_results_legacy
        """)
    else:
        conn.execute("""
            INSERT INTO checker_results (
                id, match_id, team_id, service_id, round_number, operation,
                plugin_name, plugin_version, status, message,
                duration_ms, data_json, created_at
            )
            SELECT
                id, 1, team_id, service_id, round_number, 'CHECK',
                'legacy', '0', status, COALESCE(details, ''),
                0, '{}', created_at
            FROM checker_results_legacy
        """)
    conn.execute("DROP TABLE checker_results_legacy")


def _initialize_rounds_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rounds'"
    ).fetchone()
    if existing is None:
        _create_rounds_table(conn)
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(rounds)").fetchall()}
    if "status" in columns and "deadline_at" in columns:
        return

    conn.execute("ALTER TABLE rounds RENAME TO rounds_legacy")
    _create_rounds_table(conn)
    conn.execute("""
        INSERT INTO rounds (
            id, match_id, round_number, status, started_at, deadline_at,
            completed_at, duration_seconds, error
        )
        SELECT
            id, match_id, round_number, ?, started_at,
            datetime(started_at, '+' || duration_seconds || ' seconds'),
            started_at, duration_seconds, NULL
        FROM rounds_legacy
    """, (RoundState.COMPLETED.value,))
    conn.execute("DROP TABLE rounds_legacy")


def _create_rounds_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED')),
            started_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            completed_at TEXT,
            duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0),
            error TEXT,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
            UNIQUE(match_id, round_number)
        );
    """)


def _initialize_flags_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'flags'"
    ).fetchone()
    if existing is None:
        _create_flags_table(conn)
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(flags)").fetchall()}
    if (
        "match_id" in columns
        and "expires_after_round" in columns
        and "target_host" in columns
    ):
        return

    conn.execute("ALTER TABLE flags RENAME TO flags_legacy")
    _create_flags_table(conn)
    conn.execute("""
        INSERT INTO flags (
            id, flag, match_id, team_id, service_id, round_number,
            target_host, service_name, service_port,
            status, expires_after_round, created_at, expired_at
        )
        SELECT
            id, flag, 1, team_id, service_id, round_number,
            COALESCE((SELECT ip_address FROM teams WHERE id = flags_legacy.team_id), ''),
            COALESCE((SELECT name FROM services WHERE id = flags_legacy.service_id), ''),
            COALESCE((SELECT port FROM services WHERE id = flags_legacy.service_id), 1),
            ?, 2147483647, created_at, NULL
        FROM flags_legacy
    """, (FlagState.ACTIVE.value,))
    conn.execute("DROP TABLE flags_legacy")


def _create_flags_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flag TEXT NOT NULL UNIQUE,
            match_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            target_host TEXT NOT NULL,
            service_name TEXT NOT NULL,
            service_port INTEGER NOT NULL CHECK(service_port BETWEEN 1 AND 65535),
            status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'EXPIRED')),
            expires_after_round INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expired_at TEXT,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
            UNIQUE(match_id, team_id, service_id, round_number)
        );
    """)


def _initialize_score_events_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'score_events'"
    ).fetchone()
    if existing is None:
        _create_score_events_table(conn)
    else:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(score_events)").fetchall()
        }
        if "match_id" not in columns or "checker_result_id" not in columns:
            conn.execute("ALTER TABLE score_events RENAME TO score_events_legacy")
            _create_score_events_table(conn)
            match_id = "match_id" if "match_id" in columns else "1"
            submission_id = "submission_id" if "submission_id" in columns else "NULL"
            checker_result_id = (
                "checker_result_id" if "checker_result_id" in columns else "NULL"
            )
            conn.execute(
                f"""
                INSERT INTO score_events (
                    id, match_id, team_id, round_number, event_type, points,
                    details, submission_id, checker_result_id, created_at
                )
                SELECT
                    id, {match_id}, team_id, round_number, event_type, points,
                    details, {submission_id}, {checker_result_id}, created_at
                FROM score_events_legacy
                """
            )
            conn.execute("DROP TABLE score_events_legacy")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS score_events_submission_unique
        ON score_events(submission_id) WHERE submission_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS score_events_checker_result_unique
        ON score_events(event_type, checker_result_id)
        WHERE checker_result_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS prevent_score_event_updates
        BEFORE UPDATE ON score_events
        BEGIN
            SELECT RAISE(ABORT, 'score events are immutable');
        END
        """
    )


def _create_score_events_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE score_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('ATTACK', 'DEFENSE', 'SLA')),
            points REAL NOT NULL CHECK(points >= 0),
            details TEXT,
            submission_id INTEGER,
            checker_result_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE RESTRICT,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE RESTRICT,
            FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE RESTRICT,
            FOREIGN KEY(checker_result_id) REFERENCES checker_results(id) ON DELETE RESTRICT
        )
    """)


def _migrate_legacy_scoring_policy(conn: sqlite3.Connection) -> None:
    legacy_matches = conn.execute(
        """
        SELECT id FROM matches
        WHERE scoring_policy_version = 'legacy-submission-v0'
        """
    ).fetchall()
    for (match_id,) in legacy_matches:
        attack_row = conn.execute(
            """
            SELECT points FROM score_events
            WHERE match_id = ? AND event_type = 'ATTACK'
            ORDER BY id LIMIT 1
            """,
            (match_id,),
        ).fetchone()
        attack_points = float(attack_row[0]) if attack_row else DEFAULT_ATTACK_POINTS
        conn.execute(
            """
            UPDATE matches
            SET scoring_policy_version = ?, attack_points = ?,
                defense_points = ?, sla_points = ?
            WHERE id = ?
            """,
            (
                DEFAULT_SCORING_POLICY_VERSION,
                attack_points,
                DEFAULT_DEFENSE_POINTS,
                DEFAULT_SLA_POINTS,
                match_id,
            ),
        )


def persist_checker_result(
    conn: sqlite3.Connection,
    target: ServiceTarget,
    round_number: int,
    result: CheckerResult,
    match_id: int = 1,
) -> int:
    """Persist one structured result per team/service/round/operation."""
    if round_number < 0:
        raise ValueError("round_number must be non-negative")
    data_json = json.dumps(result.data, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO checker_results (
            match_id, team_id, service_id, round_number, operation,
            plugin_name, plugin_version, status, message,
            duration_ms, data_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id, team_id, service_id, round_number, operation) DO UPDATE SET
            plugin_name=excluded.plugin_name,
            plugin_version=excluded.plugin_version,
            status=excluded.status,
            message=excluded.message,
            duration_ms=excluded.duration_ms,
            data_json=excluded.data_json,
            created_at=CURRENT_TIMESTAMP
        """,
        (
            match_id,
            target.team_id,
            target.service_id,
            round_number,
            result.operation.value,
            result.plugin_name,
            result.plugin_version,
            result.status.value,
            result.message,
            result.duration_ms,
            data_json,
        ),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT id FROM checker_results
        WHERE match_id = ? AND team_id = ? AND service_id = ?
          AND round_number = ? AND operation = ?
        """,
        (
            match_id,
            target.team_id,
            target.service_id,
            round_number,
            result.operation.value,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("persisted checker result could not be read back")
    return int(row[0])


def parse_arena_config(path: str) -> Dict[str, str]:
    """Parse key=value pairs from the arena configuration file."""
    values: Dict[str, str] = {}
    if not os.path.exists(path):
        return values
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                if separator:
                    values[key.strip()] = value.strip()
    except Exception as exc:
        print(f"Error reading arena config {path}: {exc}")
    return values


def sync_registry(conn: sqlite3.Connection, config_path: str) -> None:
    """Synchronize teams and services in SQLite with the current arena.env configuration."""
    cursor = conn.cursor()
    config = parse_arena_config(config_path)

    # 1. Parse team count
    team_count_str = config.get("ARENA_TEAM_COUNT", "0")
    try:
        team_count = int(team_count_str)
    except ValueError:
        team_count = 0

    # 2. Parse service port
    service_port_str = config.get("ARENA_SERVICE_PORT", "8080")
    try:
        service_port = int(service_port_str)
    except ValueError:
        service_port = 8080

    # 3. Parse subnet to construct team IPs
    subnet = config.get("ARENA_CTF_SUBNET", "10.10.0.0/16")
    match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.0\.0/16", subnet)
    if match:
        network_prefix = f"{int(match.group(1))}.{int(match.group(2))}"
    else:
        network_prefix = "10.10"

    team_token_pattern = config.get(
        "ARENA_TEAM_TOKEN_PATTERN",
        "sandcastle-team{team}-submission-token-change-me",
    )
    if "{team}" not in team_token_pattern:
        raise ValueError("ARENA_TEAM_TOKEN_PATTERN must contain {team}")

    # Synchronize Teams
    if team_count > 0:
        # Keep track of active team IDs
        active_team_ids = set()
        for i in range(1, team_count + 1):
            team_id = i
            active_team_ids.add(team_id)
            team_name = f"Team {i}"
            team_token = team_token_pattern.replace("{team}", str(i))
            existing = cursor.execute(
                "SELECT token FROM teams WHERE id = ?",
                (team_id,),
            ).fetchone()
            if existing is not None and verify_team_token(team_token, existing[0]):
                team_token_hash = existing[0]
            else:
                team_token_hash = hash_team_token(team_token)
            team_ip = f"{network_prefix}.{i}.3"

            # Insert or update
            cursor.execute(
                """
                INSERT INTO teams (id, name, token, ip_address)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    token=excluded.token,
                    ip_address=excluded.ip_address;
                """,
                (team_id, team_name, team_token_hash, team_ip)
            )

        # Remove extra teams not in configuration
        cursor.execute("SELECT id FROM teams;")
        all_db_ids = [row[0] for row in cursor.fetchall()]
        for db_id in all_db_ids:
            if db_id not in active_team_ids:
                cursor.execute("DELETE FROM teams WHERE id = ?;", (db_id,))
    
    # Synchronize default service (example-vuln)
    service_template = config.get("ARENA_SERVICE_TEMPLATE", "services/example-vuln")
    service_name = os.path.basename(service_template) if service_template else "example-vuln"
    
    cursor.execute(
        """
        INSERT INTO services (name, port)
        VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET port=excluded.port;
        """,
        (service_name, service_port)
    )

    scoring_values = (
        DEFAULT_SCORING_POLICY_VERSION,
        _parse_non_negative_score(
            config.get("ARENA_SCORE_ATTACK_POINTS"),
            DEFAULT_ATTACK_POINTS,
            "ARENA_SCORE_ATTACK_POINTS",
        ),
        _parse_non_negative_score(
            config.get("ARENA_SCORE_DEFENSE_POINTS"),
            DEFAULT_DEFENSE_POINTS,
            "ARENA_SCORE_DEFENSE_POINTS",
        ),
        _parse_non_negative_score(
            config.get("ARENA_SCORE_SLA_POINTS"),
            DEFAULT_SLA_POINTS,
            "ARENA_SCORE_SLA_POINTS",
        ),
    )
    cursor.execute(
        """
        UPDATE matches
        SET scoring_policy_version = ?, attack_points = ?,
            defense_points = ?, sla_points = ?
        WHERE id = 1 AND status = ?
          AND NOT EXISTS (
              SELECT 1 FROM score_events WHERE match_id = matches.id
          )
        """,
        (*scoring_values, MatchState.CREATED.value),
    )

    conn.commit()


def _parse_non_negative_score(raw: str | None, default: float, name: str) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value
