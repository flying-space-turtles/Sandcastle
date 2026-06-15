#!/usr/bin/env python3
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

_TEMP_DIR = tempfile.TemporaryDirectory()
_DATABASE = Path(_TEMP_DIR.name) / "bot-controller.db"
_SPEC = importlib.util.spec_from_file_location(
    "sandcastle_bot_api_test", ROOT / "bot" / "bot_api.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bot_api = importlib.util.module_from_spec(_SPEC)
with patch.dict(os.environ, {"BOT_CONTROLLER_DB": str(_DATABASE)}):
    _SPEC.loader.exec_module(bot_api)


def tearDownModule() -> None:
    _TEMP_DIR.cleanup()


class BotAPIValidationTest(unittest.TestCase):
    def test_team_selection_is_normalized_and_bounded(self) -> None:
        self.assertEqual(bot_api._validate_teams(["2", 1, 2]), [1, 2])
        invalid_values = (
            None,
            "1,2",
            [],
            [0],
            [bot_api.ARENA_DEFAULTS.team_count + 1],
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                bot_api._validate_teams(value)

    def test_public_config_normalizes_limits_and_known_actions(self) -> None:
        config = bot_api._public_config(
            {
                "bot_name": "  " + ("x" * 100),
                "planner": "recon_first",
                "target_policy": "selected",
                "target_teams": [2],
                "actions": ["recon.health", "exploit.sqli"],
                "loop_interval": -10,
                "timeout": 999,
            }
        )

        self.assertEqual(len(config["bot_name"]), 80)
        self.assertEqual(config["loop_interval"], 0)
        self.assertEqual(config["timeout"], 120)
        self.assertEqual(config["target_teams"], [2])
        self.assertEqual(config["service_port"], bot_api.ARENA_DEFAULTS.service_port)

    def test_public_config_rejects_invalid_policy_targets_and_actions(self) -> None:
        invalid_bodies = (
            {"target_policy": "random"},
            {"target_policy": "selected", "target_teams": []},
            {"target_policy": "selected", "target_teams": [0]},
            {"actions": []},
            {"actions": "recon.health"},
            {"actions": ["unknown.action"]},
        )
        for body in invalid_bodies:
            with self.subTest(body=body), self.assertRaises((TypeError, ValueError)):
                bot_api._public_config(body)

    def test_event_parser_and_summary_ignore_invalid_lines(self) -> None:
        events = bot_api._parse_events(
            "\n".join(
                [
                    '{"type":"flag.captured"}',
                    "not-json",
                    "[]",
                    '{"type":"submission.completed","accepted":true}',
                    '{"type":"round.failed"}',
                ]
            )
        )
        summary = bot_api._event_summary(events)

        self.assertEqual(len(events), 3)
        self.assertEqual(summary["captures"], 1)
        self.assertEqual(summary["submissions"], 1)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["failures"], 1)

    def test_model_budget_ledger_uses_controller_database(self) -> None:
        summary = bot_api.BUDGET_LEDGER.summary()
        self.assertEqual(summary["total_calls"], 0)
        self.assertEqual(summary["total_cost_usd"], 0)

    def test_match_plan_assignment_config_uses_registered_actions(self) -> None:
        teams = bot_api._validate_teams([1])
        config = bot_api._assignment_config(
            {"assignment_kind": "attack_defense", "provider": "fake", "model_id": "fake-v1"},
            "attack_defense",
        )

        self.assertEqual(teams, [1])
        self.assertEqual(config["planner"], "model")
        self.assertIn("attack.recon", config["actions"])
        self.assertIn("attack.exploit", config["actions"])
        self.assertIn("defend.apply_patch", config["actions"])

    def test_match_plan_store_upserts_and_clears_assignments(self) -> None:
        config = bot_api._assignment_config({"actions": ["recon.health"]}, "scripted")
        bot_api.MATCH_PLAN.upsert(1, "scripted", config)
        rows = bot_api.MATCH_PLAN.list()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["team_id"], 1)
        self.assertEqual(rows[0]["assignment_kind"], "scripted")

        deleted = bot_api.MATCH_PLAN.delete([1])
        self.assertEqual(deleted, 1)
        self.assertEqual(bot_api.MATCH_PLAN.list(), [])

    def test_challenge_artifact_summary_includes_file_tree(self) -> None:
        challenge_id = "unit-artifact"
        sandbox = Path(_TEMP_DIR.name) / "artifact-root"
        root = sandbox / "challenges" / "published" / challenge_id
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "manifest.json").write_text(
                '{"service":{"name":"demo"},"spec":{"vulnerability":"path_traversal"}}',
                encoding="utf-8",
            )
            (root / "app").mkdir()
            (root / "app" / "app.py").write_text("print('demo')\n", encoding="utf-8")

            with patch.object(bot_api, "REPO_ROOT", sandbox):
                summary = bot_api._challenge_artifact_summary(challenge_id)

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["challenge_id"], challenge_id)
            self.assertGreaterEqual(summary["file_count"], 2)
            self.assertIn("app.py", summary["tree"])
            self.assertEqual(summary["service"]["name"], "demo")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_challenge_deploy_copies_to_team_workspaces_and_rebuilds_apps(self) -> None:
        challenge_id = "unit-deploy"
        sandbox = Path(_TEMP_DIR.name) / "deploy-root"
        root = sandbox / "challenges" / "published" / challenge_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "app").mkdir()
        (root / "app" / "app.py").write_text(
            '@app.post("/internal/plant")\n@app.get("/internal/retrieve")\n',
            encoding="utf-8",
        )
        (root / "validation_report.json").write_text(
            """
            {
              "status": "passed",
              "steps": [
                {"name": "checker_before_patch", "status": "passed"},
                {"name": "exploit_before_patch", "status": "passed"},
                {"name": "checker_after_patch", "status": "passed"},
                {"name": "exploit_blocked_after_patch", "status": "passed"}
              ]
            }
            """,
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], timeout: int = 30, env: dict[str, str] | None = None):
            del timeout, env
            calls.append(cmd)
            if cmd[:4] == ["docker", "inspect", "--format", "{{.State.Running}}"]:
                return 0, "true"
            if "curl" in cmd:
                return 0, '{"status":"ok"}'
            return 0, "ok"

        try:
            with (
                patch.object(bot_api, "REPO_ROOT", sandbox),
                patch.object(bot_api, "_run", side_effect=fake_run),
                patch.object(
                    bot_api,
                    "_verify_deployed_challenge",
                    return_value=(True, "team1: verified"),
                ),
            ):
                ok, output = bot_api._deploy_challenge_to_arena(challenge_id)

            self.assertTrue(ok)
            self.assertIn("health ok", output)
            self.assertTrue(
                any(call[:2] == ["docker", "cp"] and str(root) in call[2] for call in calls)
            )
            self.assertTrue(
                any(
                    call[:4] == ["docker", "exec", "team1-vuln", "sh"]
                    and "docker compose up -d --build" in call[-1]
                    for call in calls
                )
            )
            self.assertTrue(
                any(
                    call[:2] == ["docker", "cp"]
                    and call[-1] == "sandcastle-gameserver:/app/services/example-vuln/checker.py"
                    for call in calls
                )
            )
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_challenge_generation_uses_model_gateway_and_records_plan_entries(self) -> None:
        sandbox = Path(_TEMP_DIR.name) / "challenge-model-root"
        db = Path(_TEMP_DIR.name) / "challenge-model.db"
        store = bot_api.ChallengeRunStore(db)
        memory = bot_api.AgentMemoryStore(db)
        ledger = bot_api.ModelBudgetLedger(db)
        run_id = store.insert(
            vulnerability="path_traversal",
            difficulty="easy",
            seed=42,
            decoy_endpoints=1,
            provider="fake",
            model_id="fake-v1",
            max_attempts=3,
        )

        with (
            patch.object(bot_api, "REPO_ROOT", sandbox),
            patch.object(bot_api, "CHALLENGE_STORE", store),
            patch.object(bot_api, "AGENT_MEMORY", memory),
            patch.object(bot_api, "BUDGET_LEDGER", ledger),
            patch.dict(os.environ, {"CHALLENGE_DOCKER_VALIDATION": "0"}),
        ):
            bot_api._generate_challenge_bg(
                run_id,
                "path_traversal",
                "easy",
                42,
                1,
                3,
                "fake",
                "fake-v1",
            )

        row = store.get(run_id)
        assert row is not None
        self.assertEqual(row["status"], "published")
        self.assertTrue(row["challenge_id"])
        entries = memory.recent_as_dicts(run_id, limit=20)
        plan_entries = [entry for entry in entries if entry["kind"] == "plan"]
        model_request_entries = [entry for entry in entries if entry["kind"] == "model_request"]
        tool_entries = [entry for entry in entries if entry["kind"] == "tool_result"]
        self.assertGreaterEqual(len(plan_entries), 4)
        self.assertGreaterEqual(len(model_request_entries), 4)
        self.assertGreaterEqual(len(tool_entries), 4)
        self.assertEqual(plan_entries[0]["data"]["provider"], "fake")
        self.assertEqual(model_request_entries[0]["data"]["model_id"], "fake-v1")
        self.assertTrue(all(entry["agent_type"] == "challenge_generator" for entry in entries))
        with patch.object(bot_api, "AGENT_MEMORY", memory):
            markdown = bot_api._agent_log_markdown(
                run_id,
                title="Challenge Generator Log",
            )
        self.assertIn("Challenge Generator Log", markdown)
        self.assertIn("model_request", markdown)
        self.assertIn("challenge.render", markdown)

    def test_prepare_match_plan_deploys_arena_before_starting_agents(self) -> None:
        order: list[str] = []

        def fake_ensure(challenge_run_id=None):
            self.assertIsNone(challenge_run_id)
            order.append("deploy")
            return True, "arena ready", None, True

        def fake_start():
            order.append("agents")
            return True, [{"id": "dep1", "status": "RUNNING"}], "agents ready"

        with (
            patch.object(bot_api, "_ensure_selected_challenge_deployed", side_effect=fake_ensure),
            patch.object(bot_api, "_start_match_plan", side_effect=fake_start),
        ):
            ok, deployments, output, challenge, challenge_deployed = bot_api._prepare_match_plan()

        self.assertTrue(ok)
        self.assertEqual(order, ["deploy", "agents"])
        self.assertEqual(deployments[0]["id"], "dep1")
        self.assertIn("arena ready", output)
        self.assertIsNone(challenge)
        self.assertTrue(challenge_deployed)

    def test_challenge_store_selects_exactly_one_published_run(self) -> None:
        db = Path(_TEMP_DIR.name) / "challenge-selection.db"
        store = bot_api.ChallengeRunStore(db)
        first = store.insert(
            vulnerability="path_traversal",
            difficulty="easy",
            seed=1,
        )
        second = store.insert(
            vulnerability="sql_injection",
            difficulty="easy",
            seed=2,
        )
        store.update(first, status="published", challenge_id="first")
        store.update(second, status="published", challenge_id="second")

        store.select(first)
        self.assertEqual(store.selected()["id"], first)
        store.select(second)
        self.assertEqual(store.selected()["id"], second)
        self.assertIsNone(store.get(first)["selected_at"])


if __name__ == "__main__":
    unittest.main()
