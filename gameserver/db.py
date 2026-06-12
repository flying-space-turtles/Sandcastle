from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Dict, Optional

from checkers.contract import CheckerResult, ServiceTarget
from models import MatchState


DEFAULT_DB_PATH = "/app/data/gameserver.db"
DEFAULT_CONFIG_PATH = "/app/config/arena.env"


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


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Initialize SQLite tables deterministically if they do not exist."""
    cursor = conn.cursor()

    # Matches table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

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

    # Rounds table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_number INTEGER NOT NULL UNIQUE,
            match_id INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_seconds INTEGER NOT NULL,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
        );
    """)

    # Flags table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flag TEXT NOT NULL UNIQUE,
            team_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE
        );
    """)

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

    # Score Events table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS score_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            points REAL NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
        );
    """)

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
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE,
            UNIQUE(team_id, service_id, round_number, operation)
        );
    """)


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
    if "operation" in columns:
        return

    conn.execute("ALTER TABLE checker_results RENAME TO checker_results_legacy")
    _create_checker_results_table(conn)
    conn.execute("""
        INSERT INTO checker_results (
            id, team_id, service_id, round_number, operation,
            plugin_name, plugin_version, status, message,
            duration_ms, data_json, created_at
        )
        SELECT
            id, team_id, service_id, round_number, 'CHECK',
            'legacy', '0', status, COALESCE(details, ''),
            0, '{}', created_at
        FROM checker_results_legacy
    """)
    conn.execute("DROP TABLE checker_results_legacy")


def persist_checker_result(
    conn: sqlite3.Connection,
    target: ServiceTarget,
    round_number: int,
    result: CheckerResult,
) -> int:
    """Persist one structured result per team/service/round/operation."""
    if round_number < 0:
        raise ValueError("round_number must be non-negative")
    data_json = json.dumps(result.data, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO checker_results (
            team_id, service_id, round_number, operation,
            plugin_name, plugin_version, status, message,
            duration_ms, data_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, service_id, round_number, operation) DO UPDATE SET
            plugin_name=excluded.plugin_name,
            plugin_version=excluded.plugin_version,
            status=excluded.status,
            message=excluded.message,
            duration_ms=excluded.duration_ms,
            data_json=excluded.data_json,
            created_at=CURRENT_TIMESTAMP
        """,
        (
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
        WHERE team_id = ? AND service_id = ? AND round_number = ? AND operation = ?
        """,
        (target.team_id, target.service_id, round_number, result.operation.value),
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

    # Synchronize Teams
    if team_count > 0:
        # Keep track of active team IDs
        active_team_ids = set()
        for i in range(1, team_count + 1):
            team_id = i
            active_team_ids.add(team_id)
            team_name = f"Team {i}"
            team_token = f"team{i}-secret-token"
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
                (team_id, team_name, team_token, team_ip)
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

    conn.commit()
