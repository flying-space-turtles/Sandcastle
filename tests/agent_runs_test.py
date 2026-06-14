#!/usr/bin/env python3
"""Tests for AI-006: Agent runs, identity, concurrent roles, and database migration."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

# Load bot_api with a temp DB to avoid touching the real controller database.
_TEMP_DIR = tempfile.TemporaryDirectory()
_DATABASE = Path(_TEMP_DIR.name) / "bot-controller.db"
_SPEC = importlib.util.spec_from_file_location(
    "sandcastle_bot_api_agent_runs_test", ROOT / "bot" / "bot_api.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bot_api = importlib.util.module_from_spec(_SPEC)
with patch.dict(os.environ, {"BOT_CONTROLLER_DB": str(_DATABASE)}):
    _SPEC.loader.exec_module(bot_api)


def tearDownModule() -> None:
    _TEMP_DIR.cleanup()


class MigrationTests(unittest.TestCase):
    """Verify that the DeploymentStore migration adds identity columns safely."""

    def test_new_store_has_identity_columns(self) -> None:
        with bot_api.STORE.connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(deployments)").fetchall()}
        for col in ("agent_type", "agent_id", "run_id", "provider", "model_id"):
            self.assertIn(col, columns, f"missing column: {col}")

    def test_existing_legacy_row_gets_backfilled_agent_identity(self) -> None:
        """A row inserted without identity columns should get agent_id = run_id = id."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            legacy_db = Path(f.name)
        try:
            import sqlite3

            # Create a legacy-style table without identity columns
            with sqlite3.connect(str(legacy_db)) as conn:
                conn.execute(
                    """
                    CREATE TABLE deployments (
                        id TEXT PRIMARY KEY,
                        team_id INTEGER NOT NULL,
                        bot_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        config_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        stopped_at TEXT,
                        pid INTEGER,
                        error TEXT,
                        archived_log TEXT NOT NULL DEFAULT '',
                        archived_events TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO deployments "
                    "(id, team_id, bot_name, status, config_json, created_at, updated_at) "
                    "VALUES ('legacy-001', 1, 'OldBot', 'STOPPED', '{}', 'T', 'T')"
                )
                conn.commit()

            # Opening DeploymentStore on that DB triggers migration + backfill
            from bot_api import DeploymentStore

            store = DeploymentStore(legacy_db)
            row = store.get("legacy-001")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(str(row["agent_id"]), "legacy-001")
            self.assertEqual(str(row["run_id"]), "legacy-001")
            self.assertEqual(str(row["agent_type"]), "scripted")
        finally:
            legacy_db.unlink(missing_ok=True)


class IdentityFieldsTests(unittest.TestCase):
    """Verify identity fields are stored and returned without credentials."""

    def test_insert_stores_agent_type_and_identity(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from bot_api import DeploymentStore

            store = DeploymentStore(db_path)
            store.insert(
                "dep-001",
                1,
                {"bot_name": "TestAgent"},
                agent_type="attack_defense",
                agent_id="team1-attack-defense",
                run_id="dep-001",
                provider="fake",
                model_id="fake-v1",
            )
            row = store.get("dep-001")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(str(row["agent_type"]), "attack_defense")
            self.assertEqual(str(row["agent_id"]), "team1-attack-defense")
            self.assertEqual(str(row["run_id"]), "dep-001")
            self.assertEqual(str(row["provider"]), "fake")
            self.assertEqual(str(row["model_id"]), "fake-v1")
        finally:
            db_path.unlink(missing_ok=True)

    def test_insert_defaults_agent_id_to_deployment_id(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from bot_api import DeploymentStore

            store = DeploymentStore(db_path)
            store.insert("dep-002", 1, {"bot_name": "Bot"})
            row = store.get("dep-002")
            assert row is not None
            self.assertEqual(str(row["agent_id"]), "dep-002")
            self.assertEqual(str(row["run_id"]), "dep-002")
        finally:
            db_path.unlink(missing_ok=True)


class UniquenessTests(unittest.TestCase):
    """Verify scoped uniqueness: one active run per (agent_type, team)."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        from bot_api import DeploymentStore

        self.store = DeploymentStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        Path(self._tmp.name).unlink(missing_ok=True)

    def _insert_active(self, dep_id: str, team_id: int, agent_type: str = "scripted") -> None:
        self.store.insert(dep_id, team_id, {"bot_name": "Bot"}, agent_type=agent_type)
        self.store.update(dep_id, status="RUNNING")

    def test_active_by_scope_returns_matching_runs(self) -> None:
        self._insert_active("dep-A", 1, "attack_defense")
        self._insert_active("dep-B", 1, "scripted")
        self._insert_active("dep-C", 2, "attack_defense")

        ad_team1 = self.store.active_by_scope("attack_defense", 1)
        self.assertEqual(len(ad_team1), 1)
        self.assertEqual(str(ad_team1[0]["id"]), "dep-A")

        scripted_team1 = self.store.active_by_scope("scripted", 1)
        self.assertEqual(len(scripted_team1), 1)
        self.assertEqual(str(scripted_team1[0]["id"]), "dep-B")

    def test_stopping_attack_defense_does_not_stop_challenge_generator(self) -> None:
        self._insert_active("dep-AD", 1, "attack_defense")
        self._insert_active("dep-CG", 0, "challenge_generator")  # organizer scope

        # Simulate stopping dep-AD
        self.store.update("dep-AD", status="STOPPED")

        # dep-CG must still be active
        cg_active = self.store.active_by_scope("challenge_generator", 0)
        self.assertEqual(len(cg_active), 1)
        self.assertEqual(str(cg_active[0]["id"]), "dep-CG")

    def test_challenge_generator_organizer_scope(self) -> None:
        self._insert_active("cg-001", 0, "challenge_generator")
        self._insert_active("cg-002", 0, "challenge_generator")
        active = self.store.active_by_scope("challenge_generator", 0)
        self.assertEqual(len(active), 2)  # both active until superseded

    def test_list_by_agent_type_filters_correctly(self) -> None:
        self._insert_active("dep-1", 1, "attack_defense")
        self._insert_active("dep-2", 2, "scripted")
        self._insert_active("dep-3", 1, "scripted")

        ad_rows = self.store.list_by_agent_type("attack_defense")
        self.assertEqual(len(ad_rows), 1)
        self.assertEqual(str(ad_rows[0]["id"]), "dep-1")

        scripted_rows = self.store.list_by_agent_type("scripted")
        self.assertEqual(len(scripted_rows), 2)


class DeploymentPayloadTests(unittest.TestCase):
    """Verify _deployment_payload includes identity fields and no credentials."""

    def test_payload_contains_agent_identity_fields(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from bot_api import DeploymentStore, _deployment_payload

            store = DeploymentStore(db_path)
            store.insert(
                "dep-xyz",
                1,
                {"bot_name": "AgentBot"},
                agent_type="attack_defense",
                agent_id="team1-attack-defense",
                run_id="dep-xyz",
                provider="fake",
                model_id="fake-v1",
            )
            row = store.get("dep-xyz")
            assert row is not None
            # Patch STORE so _deployment_payload can call STORE.update
            import bot_api as ba

            original_store = ba.STORE
            ba.STORE = store
            try:
                payload = _deployment_payload(row)
            finally:
                ba.STORE = original_store

            for field in ("agent_type", "agent_id", "run_id", "provider", "model_id"):
                self.assertIn(field, payload, f"missing field: {field}")
            self.assertEqual(payload["agent_type"], "attack_defense")
            self.assertEqual(payload["agent_id"], "team1-attack-defense")
            # No credentials in the payload
            for secret_key in ("plan_token", "api_key", "password", "submission_token"):
                self.assertNotIn(secret_key, payload)
        finally:
            db_path.unlink(missing_ok=True)


class PlanningIdentityTests(unittest.TestCase):
    """Verify PlanningIdentity carries stable agent_id and run_id."""

    def test_planning_identity_has_agent_id_and_run_id_fields(self) -> None:
        from bot_lib.agent_contracts import AgentType
        from bot_lib.agent_planning import PlanningIdentity

        identity = PlanningIdentity(
            deployment_id="dep-001",
            team_id=1,
            allowed_targets=frozenset({2}),
            allowed_actions=frozenset({"recon.health"}),
            agent_id="team1-attack-defense",
            run_id="dep-001",
            agent_type=AgentType.ATTACK_DEFENSE,
        )
        self.assertEqual(identity.agent_id, "team1-attack-defense")
        self.assertEqual(identity.run_id, "dep-001")
        self.assertEqual(identity.agent_type, AgentType.ATTACK_DEFENSE)

    def test_planning_identity_defaults_are_empty_strings(self) -> None:
        from bot_lib.agent_planning import PlanningIdentity

        identity = PlanningIdentity(
            deployment_id="dep-001",
            team_id=1,
            allowed_targets=frozenset(),
            allowed_actions=frozenset(),
        )
        self.assertEqual(identity.agent_id, "")
        self.assertEqual(identity.run_id, "")


class AgentMemoryStoreIntegrationTests(unittest.TestCase):
    """Integration: bot_api exposes AGENT_MEMORY backed by the controller DB."""

    def test_agent_memory_global_is_initialized(self) -> None:
        # AGENT_MEMORY should be present on the loaded module
        self.assertTrue(hasattr(bot_api, "AGENT_MEMORY"))
        self.assertIsNotNone(bot_api.AGENT_MEMORY)

    def test_budget_ledger_uses_same_database(self) -> None:
        # BUDGET_LEDGER and AGENT_MEMORY should share the same DB path
        self.assertEqual(bot_api.BUDGET_LEDGER.path, bot_api.AGENT_MEMORY.path)

    def test_model_budget_ledger_uses_controller_database(self) -> None:
        summary = bot_api.BUDGET_LEDGER.summary()
        self.assertEqual(summary["total_calls"], 0)


if __name__ == "__main__":
    unittest.main()
