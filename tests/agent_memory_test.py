#!/usr/bin/env python3
"""Tests for AI-007: AgentMemoryStore — bounded structured memory for agent runs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import AgentMemoryEntry, AgentType
from bot_lib.agent_memory import (
    AgentMemoryStore,
    make_error_entry,
    make_observation_entry,
    make_tool_result_entry,
    redact,
)


def _make_entry(
    agent_id: str = "agent-001",
    run_id: str = "run-001",
    kind: str = "observation",
    summary: str = "test summary",
    data: dict | None = None,
) -> AgentMemoryEntry:
    return AgentMemoryEntry(
        agent_id=agent_id,
        run_id=run_id,
        kind=kind,
        summary=summary,
        data=data or {},
    )


class RedactionTests(unittest.TestCase):
    def test_flag_is_replaced(self) -> None:
        result = redact("captured FLAG{aabbccddaabbccddaabbccddaabbccdd}")
        self.assertIn("FLAG{<redacted>}", str(result))
        self.assertNotIn("aabbccddaabbccddaabbccddaabbccdd", str(result))

    def test_sensitive_dict_keys_are_replaced(self) -> None:
        result = redact(
            {"token": "secret", "safe": "ok", "flag": "FLAG{aabbccddaabbccddaabbccddaabbccdd}"}
        )
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result["token"], "<redacted>")
        self.assertEqual(result["safe"], "ok")
        self.assertEqual(result["flag"], "<redacted>")

    def test_nested_structures_recursively_redacted(self) -> None:
        result = redact({"outer": {"password": "hunter2", "items": [{"key": "k", "val": 1}]}})
        assert isinstance(result, dict)
        self.assertEqual(result["outer"]["password"], "<redacted>")  # type: ignore[index]
        self.assertEqual(result["outer"]["items"][0]["key"], "<redacted>")  # type: ignore[index]
        self.assertEqual(result["outer"]["items"][0]["val"], 1)  # type: ignore[index]

    def test_list_values_are_redacted(self) -> None:
        result = redact(["FLAG{aabbccddaabbccddaabbccddaabbccdd}", "safe"])
        assert isinstance(result, list)
        self.assertIn("FLAG{<redacted>}", result[0])
        self.assertEqual(result[1], "safe")


class AppendAndRecentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = AgentMemoryStore(self._tmp.name, max_entries=50)

    def tearDown(self) -> None:
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_append_returns_positive_row_id(self) -> None:
        entry = _make_entry()
        row_id = self.store.append(entry)
        self.assertGreater(row_id, 0)

    def test_recent_returns_entries_in_chronological_order(self) -> None:
        for i in range(5):
            self.store.append(_make_entry(summary=f"entry-{i}"))
        entries = self.store.recent("run-001")
        self.assertEqual(len(entries), 5)
        summaries = [e.summary for e in entries]
        self.assertEqual(summaries, [f"entry-{i}" for i in range(5)])

    def test_recent_with_limit(self) -> None:
        for i in range(10):
            self.store.append(_make_entry(summary=f"e-{i}"))
        entries = self.store.recent("run-001", limit=3)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[-1].summary, "e-9")  # most recent last

    def test_runs_are_isolated(self) -> None:
        self.store.append(_make_entry(run_id="run-A", summary="A"))
        self.store.append(_make_entry(run_id="run-B", summary="B"))
        a_entries = self.store.recent("run-A")
        b_entries = self.store.recent("run-B")
        self.assertEqual(len(a_entries), 1)
        self.assertEqual(a_entries[0].summary, "A")
        self.assertEqual(len(b_entries), 1)
        self.assertEqual(b_entries[0].summary, "B")

    def test_entry_fields_round_trip(self) -> None:
        entry = AgentMemoryEntry(
            agent_id="agent-xyz",
            run_id="run-xyz",
            kind="tool_result",
            summary="tool completed",
            data={"tool_id": "recon.health", "status": "ok"},
            agent_type=AgentType.ATTACK_DEFENSE,
        )
        self.store.append(entry)
        retrieved = self.store.recent("run-xyz")
        self.assertEqual(len(retrieved), 1)
        self.assertEqual(retrieved[0].kind, "tool_result")
        self.assertEqual(retrieved[0].data["tool_id"], "recon.health")

    def test_schema_version_is_preserved(self) -> None:
        self.store.append(_make_entry())
        entries = self.store.recent("run-001")
        self.assertEqual(entries[0].schema_version, 1)

    def test_sensitive_data_is_redacted_before_storage(self) -> None:
        entry = _make_entry(data={"token": "super-secret", "result": "ok"})
        self.store.append(entry)
        retrieved = self.store.recent("run-001")
        self.assertEqual(retrieved[0].data["token"], "<redacted>")
        self.assertEqual(retrieved[0].data["result"], "ok")

    def test_flag_in_summary_is_redacted(self) -> None:
        entry = _make_entry(summary="got FLAG{aabbccddaabbccddaabbccddaabbccdd} here")
        self.store.append(entry)
        retrieved = self.store.recent("run-001")
        self.assertNotIn("aabbccddaabbccddaabbccddaabbccdd", retrieved[0].summary)
        self.assertIn("FLAG{<redacted>}", retrieved[0].summary)

    def test_recent_as_dicts_contains_required_fields(self) -> None:
        self.store.append(_make_entry())
        dicts = self.store.recent_as_dicts("run-001")
        self.assertEqual(len(dicts), 1)
        for field in (
            "agent_id",
            "run_id",
            "kind",
            "summary",
            "data",
            "created_at",
            "schema_version",
        ):
            self.assertIn(field, dicts[0], f"missing field: {field}")


class PruningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = AgentMemoryStore(self._tmp.name, max_entries=5)

    def tearDown(self) -> None:
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_oldest_entries_pruned_on_count_limit(self) -> None:
        for i in range(8):
            self.store.append(_make_entry(summary=f"entry-{i}"))
        entries = self.store.recent("run-001", limit=100)
        self.assertEqual(len(entries), 5)
        # Most recent 5 should remain
        summaries = {e.summary for e in entries}
        self.assertIn("entry-7", summaries)
        self.assertNotIn("entry-0", summaries)

    def test_manual_prune_deletes_excess(self) -> None:
        store2 = AgentMemoryStore(self._tmp.name, max_entries=3)
        for i in range(6):
            store2.store = None  # work around: use raw append
            AgentMemoryStore(self._tmp.name, max_entries=100).append(_make_entry(summary=f"e-{i}"))
        # Now prune with limit=3
        store3 = AgentMemoryStore(self._tmp.name, max_entries=3)
        deleted = store3.prune("run-001")
        self.assertGreaterEqual(deleted, 3)
        remaining = store3.recent("run-001", limit=100)
        self.assertLessEqual(len(remaining), 3)

    def test_different_runs_prune_independently(self) -> None:
        store = AgentMemoryStore(self._tmp.name, max_entries=3)
        for i in range(5):
            store.append(_make_entry(run_id="run-X", summary=f"x-{i}"))
        for i in range(2):
            store.append(_make_entry(run_id="run-Y", summary=f"y-{i}"))
        x_entries = store.recent("run-X", limit=100)
        y_entries = store.recent("run-Y", limit=100)
        self.assertLessEqual(len(x_entries), 3)
        self.assertEqual(len(y_entries), 2)


class PersistenceTests(unittest.TestCase):
    def test_entries_survive_store_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store1 = AgentMemoryStore(db_path)
            store1.append(_make_entry(summary="persistent"))
            # Open a new store instance on the same DB
            store2 = AgentMemoryStore(db_path)
            entries = store2.recent("run-001")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].summary, "persistent")
        finally:
            Path(db_path).unlink(missing_ok=True)


class HelperTests(unittest.TestCase):
    def test_make_tool_result_entry_structure(self) -> None:
        entry = make_tool_result_entry(
            agent_id="agent-1",
            agent_type=AgentType.ATTACK_DEFENSE,
            run_id="run-1",
            tool_id="exploit.sqli",
            call_id="call-001",
            status="ok",
            summary="flag captured",
            data={"flag": "FLAG{aabbccddaabbccddaabbccddaabbccdd}"},
        )
        self.assertEqual(entry.kind, "tool_result")
        self.assertEqual(entry.data["tool_id"], "exploit.sqli")
        self.assertEqual(entry.data["status"], "ok")

    def test_make_observation_entry_structure(self) -> None:
        entry = make_observation_entry("agent-1", "run-1", "round 5 started", {"round": 5})
        self.assertEqual(entry.kind, "observation")
        self.assertEqual(entry.data["round"], 5)

    def test_make_error_entry_structure(self) -> None:
        entry = make_error_entry("agent-1", "run-1", "timeout", {"provider": "openai"})
        self.assertEqual(entry.kind, "error")
        self.assertIn("timeout", entry.summary)


if __name__ == "__main__":
    unittest.main()
