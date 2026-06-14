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


if __name__ == "__main__":
    unittest.main()
