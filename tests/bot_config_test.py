#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.config import BotConfig, load_config_file, merge_config, normalize_action_id


class BotConfigTest(unittest.TestCase):
    def test_environment_defaults_are_applied_at_construction(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SERVICE_PORT": "9090",
                "FLAG_RE": r"TOKEN\{[A-Z]+\}",
                "IP_PATTERN": "192.0.2.{team}",
            },
        ):
            config = BotConfig()

        self.assertEqual(config.service_port, 9090)
        self.assertEqual(config.flag_re, r"TOKEN\{[A-Z]+\}")
        self.assertEqual(config.ip_pattern, "192.0.2.{team}")

    def test_merge_coerces_values_and_supports_legacy_keys(self) -> None:
        config = merge_config(
            BotConfig(),
            {
                "bot_name": 42,
                "service_port": "8081",
                "target_teams": "3, 1,invalid",
                "exploits": ["path_traversal", "cmdi", "recon.health"],
                "stop_on_first": "off",
            },
        )

        self.assertEqual(config.bot_name, "42")
        self.assertEqual(config.service_port, 8081)
        self.assertEqual(config.target_teams, [3, 1])
        self.assertEqual(
            config.actions,
            ["exploit.path_traversal", "exploit.cmdi", "recon.health"],
        )
        self.assertFalse(config.stop_on_success)

    def test_new_keys_take_precedence_over_legacy_aliases(self) -> None:
        config = merge_config(
            BotConfig(),
            {
                "actions": ["recon.health"],
                "exploits": ["sqli"],
                "stop_on_success": True,
                "stop_on_first": False,
            },
        )

        self.assertEqual(config.actions, ["recon.health"])
        self.assertTrue(config.stop_on_success)
        self.assertEqual(normalize_action_id(" sqli "), "exploit.sqli")

    def test_load_config_file_handles_missing_invalid_and_valid_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bot.json"
            self.assertEqual(load_config_file(str(config_path)), BotConfig())

            config_path.write_text("not-json", encoding="utf-8")
            with patch("builtins.print") as printer:
                invalid = load_config_file(str(config_path))
            self.assertEqual(invalid, BotConfig())
            printer.assert_called_once()

            config_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
            with patch("builtins.print") as printer:
                non_object = load_config_file(str(config_path))
            self.assertEqual(non_object, BotConfig())
            printer.assert_called_once()

            config_path.write_text(
                json.dumps({"planner": "recon_first", "target_teams": [2]}),
                encoding="utf-8",
            )
            loaded = load_config_file(str(config_path))
            self.assertEqual(loaded.planner, "recon_first")
            self.assertEqual(loaded.target_teams, [2])


if __name__ == "__main__":
    unittest.main()
