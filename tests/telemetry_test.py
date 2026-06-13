#!/usr/bin/env python3
"""Tests for SC-015: structured match and agent telemetry."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gameserver"))

import db
import telemetry


def _make_db() -> object:
    conn = db.get_db_connection(":memory:")
    db.initialize_schema(conn)
    return conn


class RedactionTests(unittest.TestCase):
    def test_flag_values_are_scrubbed_from_strings(self) -> None:
        raw = "captured FLAG{aabbccddaabbccddaabbccddaabbccdd} in round 3"
        result = telemetry.redact(raw)
        self.assertNotIn("aabbccddaabbccddaabbccddaabbccdd", result)
        self.assertIn("FLAG{<redacted>}", result)

    def test_sensitive_dict_keys_are_replaced(self) -> None:
        payload = {
            "token": "supersecret",
            "password": "hunter2",
            "flag": "FLAG{aabbccddaabbccddaabbccddaabbccdd}",
            "round": 5,
            "message": "ok",
        }
        result = telemetry.redact(payload)
        self.assertEqual(result["token"], "<redacted>")
        self.assertEqual(result["password"], "<redacted>")
        self.assertEqual(result["flag"], "<redacted>")
        self.assertEqual(result["round"], 5)
        self.assertEqual(result["message"], "ok")

    def test_nested_structures_are_recursively_redacted(self) -> None:
        payload = {
            "outer": {
                "secret": "val",
                "items": [{"token": "t", "safe": 1}],
            }
        }
        result = telemetry.redact(payload)
        self.assertEqual(result["outer"]["secret"], "<redacted>")
        self.assertEqual(result["outer"]["items"][0]["token"], "<redacted>")
        self.assertEqual(result["outer"]["items"][0]["safe"], 1)

    def test_flag_pattern_in_nested_string(self) -> None:
        payload = {"details": "got FLAG{00000000000000000000000000000000} here"}
        result = telemetry.redact(payload)
        self.assertIn("FLAG{<redacted>}", result["details"])
        self.assertNotIn("00000000000000000000000000000000", result["details"])


class EmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_emit_stores_event_and_returns_id(self) -> None:
        event_id = telemetry.emit(
            self.conn,
            telemetry.ROUND_STARTED,
            "gameserver",
            match_id=1,
            round_number=1,
            payload={"duration_seconds": 120},
        )
        self.conn.commit()
        self.assertIsInstance(event_id, int)
        self.assertGreater(event_id, 0)

    def test_emit_redacts_payload_before_storing(self) -> None:
        telemetry.emit(
            self.conn,
            "test.event",
            "test",
            payload={"flag": "FLAG{aabbccddaabbccddaabbccddaabbccdd}", "round": 1},
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT payload_json FROM telemetry_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stored = json.loads(row[0])
        self.assertEqual(stored["flag"], "<redacted>")
        self.assertEqual(stored["round"], 1)

    def test_emit_stores_all_correlation_fields(self) -> None:
        telemetry.emit(
            self.conn,
            telemetry.SUBMISSION_ACCEPTED,
            "gameserver",
            match_id=1,
            round_number=3,
            team_id=2,
            payload={"submission_id": 42},
            correlation_id="corr-abc",
        )
        self.conn.commit()
        row = self.conn.execute(
            """
            SELECT schema_version, event_type, source, match_id, round_number,
                   team_id, correlation_id
            FROM telemetry_events ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(row[0], telemetry.EVENT_SCHEMA_VERSION)
        self.assertEqual(row[1], telemetry.SUBMISSION_ACCEPTED)
        self.assertEqual(row[2], "gameserver")
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], 3)
        self.assertEqual(row[5], 2)
        self.assertEqual(row[6], "corr-abc")

    def test_multiple_event_types_stored(self) -> None:
        for ev in (telemetry.ROUND_STARTED, telemetry.ROUND_COMPLETED, telemetry.ROUND_FAILED):
            telemetry.emit(self.conn, ev, "gameserver", match_id=1, round_number=1)
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM telemetry_events").fetchone()[0]
        self.assertEqual(count, 3)


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_export_returns_events_for_match(self) -> None:
        for i in range(3):
            telemetry.emit(
                self.conn,
                telemetry.ROUND_STARTED,
                "gameserver",
                match_id=1,
                round_number=i + 1,
            )
        self.conn.commit()
        events = telemetry.export_match(self.conn, match_id=1)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(ev["match_id"] == 1 for ev in events))
        self.assertTrue(all(ev["event_type"] == telemetry.ROUND_STARTED for ev in events))

    def test_export_contains_required_fields(self) -> None:
        telemetry.emit(
            self.conn,
            telemetry.SUBMISSION_ACCEPTED,
            "gameserver",
            match_id=1,
            round_number=2,
            team_id=1,
            payload={"code": "ACCEPTED"},
        )
        self.conn.commit()
        events = telemetry.export_match(self.conn, match_id=1)
        ev = events[0]
        for field in ("id", "schema_version", "event_type", "source",
                      "match_id", "round_number", "team_id", "payload",
                      "correlation_id", "created_at"):
            self.assertIn(field, ev, f"missing field: {field}")
        self.assertIsInstance(ev["payload"], dict)

    def test_export_empty_for_unknown_match(self) -> None:
        events = telemetry.export_match(self.conn, match_id=999)
        self.assertEqual(events, [])

    def test_export_ordered_by_insertion(self) -> None:
        for i in range(5):
            telemetry.emit(self.conn, "test.seq", "test", match_id=1,
                           payload={"seq": i})
        self.conn.commit()
        events = telemetry.export_match(self.conn, match_id=1)
        seqs = [ev["payload"]["seq"] for ev in events]
        self.assertEqual(seqs, sorted(seqs))


class IngestBatchTests(unittest.TestCase):
    """Test the ingest route via direct telemetry calls (no HTTP server)."""

    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_bot_events_can_be_stored_with_arbitrary_type(self) -> None:
        bot_events = [
            {"event_type": "bot.round.completed", "team_id": 1, "round_number": 5,
             "payload": {"flag_count": 3}},
            {"event_type": "bot.action.completed", "team_id": 2, "round_number": 5,
             "payload": {"status": "ok", "action_id": "exploit.cmdi"}},
        ]
        for ev in bot_events:
            telemetry.emit(
                self.conn,
                ev["event_type"],
                "bot-controller",
                match_id=1,
                round_number=ev.get("round_number"),
                team_id=ev.get("team_id"),
                payload=ev.get("payload"),
            )
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM telemetry_events").fetchone()[0]
        self.assertEqual(count, 2)

    def test_ingest_redacts_flag_in_forwarded_payload(self) -> None:
        telemetry.emit(
            self.conn,
            "bot.flag.captured",
            "bot-controller",
            match_id=1,
            payload={"flag": "FLAG{aabbccddaabbccddaabbccddaabbccdd}", "action": "exploit.sqli"},
        )
        self.conn.commit()
        row = self.conn.execute("SELECT payload_json FROM telemetry_events").fetchone()
        stored = json.loads(row[0])
        self.assertEqual(stored["flag"], "<redacted>")
        self.assertEqual(stored["action"], "exploit.sqli")


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_telemetry_events_table_exists(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry_events'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_telemetry_schema_version_constant(self) -> None:
        self.assertEqual(telemetry.EVENT_SCHEMA_VERSION, 1)

    def test_event_type_constants_defined(self) -> None:
        expected = [
            "ROUND_STARTED", "ROUND_COMPLETED", "ROUND_FAILED",
            "SUBMISSION_RECEIVED", "SUBMISSION_ACCEPTED", "SUBMISSION_REJECTED",
            "MATCH_STATE_CHANGED",
        ]
        for name in expected:
            self.assertTrue(hasattr(telemetry, name), f"missing constant: telemetry.{name}")

    def test_cascade_delete_removes_events_with_match(self) -> None:
        conn2 = db.get_db_connection(":memory:")
        db.initialize_schema(conn2)
        conn2.execute("INSERT INTO matches (id, status) VALUES (2, 'CREATED')")
        conn2.commit()
        telemetry.emit(conn2, telemetry.ROUND_STARTED, "gameserver", match_id=2, round_number=1)
        conn2.commit()
        count_before = conn2.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE match_id = 2"
        ).fetchone()[0]
        self.assertEqual(count_before, 1)
        conn2.execute("DELETE FROM matches WHERE id = 2")
        conn2.commit()
        count_after = conn2.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE match_id = 2"
        ).fetchone()[0]
        self.assertEqual(count_after, 0)
        conn2.close()


if __name__ == "__main__":
    unittest.main()
