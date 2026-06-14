"""Bounded structured memory for Sandcastle AI agent runs.

Each agent run accumulates concise observations, selected tool calls, validated
tool arguments, tool status, safe result summaries, checker/exploit outcomes,
patch identifiers, model usage, and errors.

The store shares the bot-controller SQLite database so memory entries survive
controller restarts and are queryable alongside deployment and budget records.

Sensitive values — flags, tokens, API keys, full source files, and raw model
reasoning — are redacted before storage.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_contracts import AgentMemoryEntry, AgentType

# Entries whose data JSON exceeds this limit are not stored.
MAX_DATA_BYTES = 16_384  # 16 KB per entry
# Default per-run retention limits.
DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_PAYLOAD_BYTES = 65_536  # 64 KB total summary+data across a run

# Keys whose values are unconditionally replaced with "<redacted>".
_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "password",
        "secret",
        "key",
        "api_key",
        "plan_token",
        "submission_token",
        "flag",
        "raw_response",
        "source",
        "patch",
        "diff",
    }
)

_FLAG_RE = re.compile(r"FLAG\{[a-f0-9]{32}\}", re.IGNORECASE)


def redact(value: object) -> object:
    """Recursively redact sensitive values from a JSON-compatible structure."""
    if isinstance(value, str):
        return _FLAG_RE.sub("FLAG{<redacted>}", value)
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if k.lower() in _SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentMemoryStore:
    """SQLite-backed memory store for agent runs.

    Shares the same database file as the deployment controller and budget
    ledger. All writes are atomic. Memory entries are bounded per run by
    entry count and total payload size; the oldest entries are pruned when
    limits are exceeded.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be at least 1")
        self.path = str(path)
        self.max_entries = max_entries
        self.max_payload_bytes = max_payload_bytes
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_memory (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id       TEXT NOT NULL,
                    agent_type     TEXT NOT NULL DEFAULT 'attack_defense',
                    run_id         TEXT NOT NULL,
                    kind           TEXT NOT NULL,
                    summary        TEXT NOT NULL,
                    data_json      TEXT NOT NULL DEFAULT '{}',
                    created_at     TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS agent_memory_run_idx
                ON agent_memory(run_id, id DESC);

                CREATE INDEX IF NOT EXISTS agent_memory_agent_idx
                ON agent_memory(agent_id, id DESC);
                """
            )
            conn.commit()

    def append(self, entry: AgentMemoryEntry) -> int:
        """Insert a memory entry. Returns the new row id.

        Redacts sensitive values before storage. Prunes oldest entries if the
        per-run retention limits would be exceeded after insertion.
        """
        safe_data: dict[str, Any] = dict(redact(entry.data))  # type: ignore[arg-type]
        data_json = json.dumps(safe_data, sort_keys=True, separators=(",", ":"))
        if len(data_json.encode()) > MAX_DATA_BYTES:
            data_json = json.dumps(
                {"truncated": True, "reason": "data exceeded per-entry size limit"},
                separators=(",", ":"),
            )

        safe_summary = str(redact(entry.summary))[:2000]

        with closing(self.connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO agent_memory
                    (agent_id, agent_type, run_id, kind, summary, data_json, created_at, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.agent_id,
                    str(entry.agent_type),
                    entry.run_id,
                    entry.kind,
                    safe_summary,
                    data_json,
                    entry.created_at,
                    entry.schema_version,
                ),
            )
            row_id = cursor.lastrowid or 0
            conn.commit()

        self._prune(entry.run_id)
        return row_id

    def _prune(self, run_id: str) -> int:
        """Delete the oldest entries for a run if retention limits are exceeded."""
        with closing(self.connect()) as conn:
            # Count-based pruning
            count_row = conn.execute(
                "SELECT COUNT(*) FROM agent_memory WHERE run_id = ?", (run_id,)
            ).fetchone()
            total = int(count_row[0])
            deleted = 0
            if total > self.max_entries:
                excess = total - self.max_entries
                conn.execute(
                    """
                    DELETE FROM agent_memory WHERE id IN (
                        SELECT id FROM agent_memory WHERE run_id = ?
                        ORDER BY id ASC LIMIT ?
                    )
                    """,
                    (run_id, excess),
                )
                deleted += excess

            # Payload-size pruning
            size_row = conn.execute(
                """
                SELECT COALESCE(SUM(LENGTH(summary) + LENGTH(data_json)), 0)
                FROM agent_memory WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            total_bytes = int(size_row[0])
            while total_bytes > self.max_payload_bytes:
                oldest = conn.execute(
                    "SELECT id, LENGTH(summary) + LENGTH(data_json) FROM agent_memory "
                    "WHERE run_id = ? ORDER BY id ASC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if oldest is None:
                    break
                conn.execute("DELETE FROM agent_memory WHERE id = ?", (oldest[0],))
                total_bytes -= int(oldest[1])
                deleted += 1

            conn.commit()
        return deleted

    def recent(self, run_id: str, limit: int = 20) -> list[AgentMemoryEntry]:
        """Return up to ``limit`` most recent entries for the given run."""
        limit = max(1, min(limit, 500))
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT agent_id, run_id, kind, summary, data_json, created_at, schema_version
                FROM agent_memory WHERE run_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        entries = []
        for row in reversed(rows):  # chronological order
            try:
                data = json.loads(row["data_json"])
            except (json.JSONDecodeError, TypeError):
                data = {}
            entries.append(
                AgentMemoryEntry(
                    agent_id=str(row["agent_id"]),
                    run_id=str(row["run_id"]),
                    kind=str(row["kind"]),
                    summary=str(row["summary"]),
                    data=data,
                    created_at=str(row["created_at"]),
                    schema_version=int(row["schema_version"]),
                )
            )
        return entries

    def recent_as_dicts(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent entries as plain dicts (for API responses)."""
        return [e.as_dict() for e in self.recent(run_id, limit)]

    def prune(self, run_id: str) -> int:
        """Manually trigger retention enforcement. Returns deleted row count."""
        return self._prune(run_id)


def make_tool_result_entry(
    agent_id: str,
    agent_type: AgentType,
    run_id: str,
    tool_id: str,
    call_id: str,
    status: str,
    summary: str,
    data: dict[str, Any] | None = None,
) -> AgentMemoryEntry:
    """Construct a ToolResult-shaped memory entry."""
    return AgentMemoryEntry(
        agent_id=agent_id,
        run_id=run_id,
        kind="tool_result",
        summary=summary[:2000],
        data={
            "tool_id": tool_id,
            "call_id": call_id,
            "status": status,
            **(data or {}),
        },
        created_at=_now_iso(),
    )


def make_observation_entry(
    agent_id: str,
    run_id: str,
    summary: str,
    data: dict[str, Any] | None = None,
) -> AgentMemoryEntry:
    """Construct an observation memory entry."""
    return AgentMemoryEntry(
        agent_id=agent_id,
        run_id=run_id,
        kind="observation",
        summary=summary[:2000],
        data=data or {},
        created_at=_now_iso(),
    )


def make_error_entry(
    agent_id: str,
    run_id: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> AgentMemoryEntry:
    """Construct an error memory entry."""
    return AgentMemoryEntry(
        agent_id=agent_id,
        run_id=run_id,
        kind="error",
        summary=message[:2000],
        data=data or {},
        created_at=_now_iso(),
    )
