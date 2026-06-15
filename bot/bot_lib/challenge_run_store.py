"""ChallengeRunStore — persistent history of challenge generation runs.

Each row records one call to POST /challenges/generate, with the spec used,
the status (running/published/failed/cancelled), and the deployed_at timestamp
once the organiser injects it into arena containers.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DDL = """
CREATE TABLE IF NOT EXISTS challenge_runs (
    id               TEXT PRIMARY KEY,
    challenge_id     TEXT,
    status           TEXT NOT NULL DEFAULT 'running',
    spec_json        TEXT,
    vulnerability    TEXT,
    difficulty       TEXT,
    seed             INTEGER,
    decoy_endpoints  INTEGER DEFAULT 0,
    provider         TEXT,
    model_id         TEXT,
    max_attempts     INTEGER DEFAULT 3,
    selected_at      TEXT,
    deployed_at      TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""


class ChallengeRunStore:
    """Thin SQLite wrapper for challenge generation run history."""

    def __init__(self, db_path: Path) -> None:
        self._db = str(db_path)
        with sqlite3.connect(self._db) as conn:
            conn.execute(_DDL)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(challenge_runs)").fetchall()
            }
            if "selected_at" not in columns:
                conn.execute("ALTER TABLE challenge_runs ADD COLUMN selected_at TEXT")
            now = _now()
            conn.execute(
                """
                UPDATE challenge_runs
                SET status = 'failed',
                    error = 'controller restarted before generation completed',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def insert(
        self,
        *,
        run_id: str | None = None,
        vulnerability: str,
        difficulty: str,
        seed: int,
        decoy_endpoints: int = 0,
        provider: str = "fake",
        model_id: str = "",
        max_attempts: int = 3,
        spec_json: str = "{}",
    ) -> str:
        rid = run_id or uuid.uuid4().hex[:16]
        now = _now()
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO challenge_runs
                  (id, status, spec_json, vulnerability, difficulty, seed,
                   decoy_endpoints, provider, model_id, max_attempts,
                   created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rid,
                    "running",
                    spec_json,
                    vulnerability,
                    difficulty,
                    seed,
                    decoy_endpoints,
                    provider,
                    model_id,
                    max_attempts,
                    now,
                    now,
                ),
            )
            conn.commit()
        return rid

    def update(self, run_id: str, **values: Any) -> None:
        values["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in values)
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                f"UPDATE challenge_runs SET {assignments} WHERE id = ?",  # noqa: S608
                [*values.values(), run_id],
            )
            conn.commit()

    def select(self, run_id: str) -> dict[str, Any] | None:
        """Select exactly one published challenge for the next match."""
        now = _now()
        with sqlite3.connect(self._db) as conn:
            conn.execute("UPDATE challenge_runs SET selected_at = NULL")
            conn.execute(
                """
                UPDATE challenge_runs
                SET selected_at = ?, updated_at = ?
                WHERE id = ? AND status = 'published'
                """,
                (now, now, run_id),
            )
            conn.commit()
        return self.get(run_id)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM challenge_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM challenge_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_published(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM challenge_runs WHERE status = 'published' ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def selected(self) -> dict[str, Any] | None:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM challenge_runs
                WHERE selected_at IS NOT NULL AND status = 'published'
                ORDER BY selected_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def deployed(self) -> dict[str, Any] | None:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM challenge_runs
                WHERE deployed_at IS NOT NULL
                ORDER BY deployed_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def active(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM challenge_runs WHERE status = 'running'").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def payload(row: dict[str, Any]) -> dict[str, Any]:
        """Return a safe API payload (spec_json parsed, no raw secrets)."""
        spec = {}
        try:
            spec = json.loads(row.get("spec_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        return {
            "id": row["id"],
            "challenge_id": row.get("challenge_id"),
            "status": row.get("status", "unknown"),
            "vulnerability": row.get("vulnerability"),
            "difficulty": row.get("difficulty"),
            "seed": row.get("seed"),
            "decoy_endpoints": row.get("decoy_endpoints", 0),
            "provider": row.get("provider"),
            "model_id": row.get("model_id"),
            "max_attempts": row.get("max_attempts", 3),
            "selected_at": row.get("selected_at"),
            "deployed_at": row.get("deployed_at"),
            "error": row.get("error"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "spec": spec,
        }
