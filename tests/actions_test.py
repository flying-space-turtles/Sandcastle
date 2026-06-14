#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.actions import (
    CommandInjectionAction,
    HealthCheckAction,
    PathTraversalAction,
    PlantProbeAction,
    SqlInjectionAction,
    WatchdogAction,
    action_catalog,
)
from bot_lib.config import BotConfig
from bot_lib.runtime import BotContext

FLAG = "FLAG{0123456789abcdef0123456789abcdef}"


def _context(capabilities: frozenset[str] = frozenset()) -> BotContext:
    return BotContext(
        config=BotConfig(),
        num_teams=3,
        my_team=1,
        capabilities=capabilities,
    )


class ActionTest(unittest.TestCase):
    def test_health_and_exploit_actions_classify_results(self) -> None:
        context = _context()
        with patch.object(context, "get", side_effect=['{"status":"ok"}', FLAG]):
            health = HealthCheckAction().run(context, 2)
            traversal = PathTraversalAction().run(context, 2)
        with patch.object(context, "post", return_value=f"diagnostics: {FLAG}") as post:
            command = CommandInjectionAction().run(context, 3)

        self.assertEqual((health.status, health.message), ("ok", "/health ok"))
        self.assertEqual(traversal.flags, [FLAG])
        self.assertEqual(command.flags, [FLAG])
        self.assertIn("cat /app/data/flag.txt", post.call_args.args[1]["host"])

    def test_sql_injection_handles_unreachable_login_and_captured_flag(self) -> None:
        context = _context()
        with patch.object(context, "post", return_value=None), patch.object(context, "get"):
            unreachable = SqlInjectionAction().run(context, 2)
        self.assertEqual((unreachable.status, unreachable.message), ("miss", "login unreachable"))

        with (
            patch.object(context, "post", return_value="logged in"),
            patch.object(context, "get", return_value=f"notes {FLAG}"),
        ):
            captured = SqlInjectionAction().run(context, 2)
        self.assertEqual(captured.flags, [FLAG])

    def test_plant_probe_distinguishes_response_classes(self) -> None:
        context = _context()
        cases = (
            ((403, "forbidden"), "ok", 0),
            ((200, "planted"), "ok", 1),
            ((0, "network down"), "miss", 0),
            ((500, "failure"), "miss", 0),
        )
        for response, expected_status, expected_flag_count in cases:
            with (
                self.subTest(response=response),
                patch.object(context, "post_json", return_value=response),
            ):
                result = PlantProbeAction().run(context, 2)
                self.assertEqual(result.status, expected_status)
                self.assertEqual(len(result.flags), expected_flag_count)

    def test_watchdog_checks_health_before_restart(self) -> None:
        context = _context(frozenset({"service.control.local"}))
        with patch(
            "bot_lib.actions.call_service_control",
            side_effect=[{"running": False}, {"running": True}],
        ) as service_control:
            result = WatchdogAction().run(context)

        self.assertEqual((result.status, result.message), ("ok", "service app restarted"))
        self.assertEqual(
            service_control.call_args_list,
            [
                call("10.10.1.3", "GET", "/service/health"),
                call("10.10.1.3", "POST", "/service/restart"),
            ],
        )

    def test_catalog_exposes_unique_actions_and_capabilities(self) -> None:
        catalog = action_catalog()
        ids = [entry["id"] for entry in catalog]
        self.assertEqual(len(ids), len(set(ids)))
        watchdog = next(entry for entry in catalog if entry["id"] == "maintain.watchdog")
        self.assertEqual(watchdog["required_capabilities"], ["service.control.local"])


if __name__ == "__main__":
    unittest.main()
