#!/usr/bin/env python3
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.config import BotConfig
from bot_lib.planners import BotTask, load_planner, planner_catalog
from bot_lib.runtime import BotContext


def _context(config: BotConfig | None = None) -> BotContext:
    return BotContext(
        config=config or BotConfig(),
        num_teams=4,
        my_team=2,
        capabilities=frozenset(),
    )


class PlannerTest(unittest.TestCase):
    def test_scripted_targets_exclude_own_team_and_honor_selection(self) -> None:
        planner = load_planner("scripted")
        self.assertEqual(planner.targets(_context()), [1, 3, 4])

        selected = _context(BotConfig(target_policy="selected", target_teams=[4, 2, 1]))
        self.assertEqual(planner.targets(selected), [4, 1])
        self.assertEqual(planner.targets(selected, override_target=3), [3])

    def test_scripted_plan_filters_unknown_and_self_scoped_actions(self) -> None:
        context = _context(
            BotConfig(
                target_policy="selected",
                target_teams=[1, 3],
                actions=["recon.health", "missing.action", "maintain.watchdog", "exploit.sqli"],
            )
        )

        self.assertEqual(
            list(load_planner("scripted").plan(context)),
            [
                BotTask(1, "recon.health"),
                BotTask(1, "exploit.sqli"),
                BotTask(3, "recon.health"),
                BotTask(3, "exploit.sqli"),
            ],
        )

    def test_recon_first_runs_recon_across_targets_before_exploits(self) -> None:
        context = _context(
            BotConfig(
                target_policy="selected",
                target_teams=[1, 3],
                actions=["exploit.cmdi", "recon.health", "exploit.sqli"],
            )
        )

        self.assertEqual(
            list(load_planner("recon_first").plan(context)),
            [
                BotTask(1, "recon.health"),
                BotTask(3, "recon.health"),
                BotTask(1, "exploit.cmdi"),
                BotTask(1, "exploit.sqli"),
                BotTask(3, "exploit.cmdi"),
                BotTask(3, "exploit.sqli"),
            ],
        )

    def test_external_planner_classes_are_instantiated(self) -> None:
        module = types.ModuleType("test_external_planner")

        class ExternalPlanner:
            id = "external"

        module.ExternalPlanner = ExternalPlanner
        with patch.dict(sys.modules, {module.__name__: module}):
            planner = load_planner(f"{module.__name__}:ExternalPlanner")

        self.assertIsInstance(planner, ExternalPlanner)

    def test_catalog_and_unknown_planner_are_explicit(self) -> None:
        ids = {entry["id"] for entry in planner_catalog()}
        self.assertTrue({"scripted", "recon_first", "model"} <= ids)
        with self.assertRaisesRegex(ValueError, "unknown planner"):
            load_planner("does-not-exist")


if __name__ == "__main__":
    unittest.main()
