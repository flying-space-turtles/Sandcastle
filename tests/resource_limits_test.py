#!/usr/bin/env python3
"""Tests for SC-018: resource limits and failure containment."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gameserver"))

import resource_monitor


def _status(**kwargs: object) -> resource_monitor.ContainerStatus:
    defaults: dict[str, object] = {
        "name": "test-container",
        "running": True,
        "restart_count": 0,
        "oom_killed": False,
    }
    defaults.update(kwargs)
    return resource_monitor.ContainerStatus(**defaults)  # type: ignore[arg-type]


def _inspect_data(**overrides: object) -> dict:
    """Build a minimal docker inspect dict, with optional field overrides."""
    data: dict = {
        "Name": "/team1-vuln",
        "RestartCount": 0,
        "State": {
            "Running": True,
            "OOMKilled": False,
            "ExitCode": 0,
        },
        "HostConfig": {
            "Memory": 0,
        },
    }
    for dotted_key, value in overrides.items():
        if "." in dotted_key:
            section, field = dotted_key.split(".", 1)
            data[section][field] = value
        else:
            data[dotted_key] = value
    return data


class ContainerStatusHealthLabelTests(unittest.TestCase):
    def test_running_container_is_healthy(self) -> None:
        self.assertEqual(_status(running=True).health_label, "running")

    def test_stopped_container_label(self) -> None:
        self.assertEqual(_status(running=False).health_label, "stopped")

    def test_oom_killed_label(self) -> None:
        s = _status(running=False, oom_killed=True)
        self.assertEqual(s.health_label, "oom_killed")

    def test_oom_killed_takes_priority_over_restart_loop(self) -> None:
        # A container that OOMed AND has many restarts is labelled oom_killed, not restart_loop.
        s = _status(running=False, oom_killed=True,
                    restart_count=resource_monitor.RESTART_LOOP_THRESHOLD + 5)
        self.assertEqual(s.health_label, "oom_killed")

    def test_restart_loop_at_exact_threshold(self) -> None:
        s = _status(restart_count=resource_monitor.RESTART_LOOP_THRESHOLD)
        self.assertTrue(s.is_restart_loop)
        self.assertEqual(s.health_label, "restart_loop")

    def test_one_below_threshold_is_not_a_loop(self) -> None:
        s = _status(restart_count=resource_monitor.RESTART_LOOP_THRESHOLD - 1)
        self.assertFalse(s.is_restart_loop)
        self.assertNotEqual(s.health_label, "restart_loop")

    def test_zero_restarts_is_not_a_loop(self) -> None:
        self.assertFalse(_status(restart_count=0).is_restart_loop)


class ParseInspectTests(unittest.TestCase):
    def test_parses_running_container(self) -> None:
        s = resource_monitor.parse_inspect(_inspect_data())
        self.assertEqual(s.name, "team1-vuln")
        self.assertTrue(s.running)
        self.assertFalse(s.oom_killed)
        self.assertEqual(s.restart_count, 0)
        self.assertIsNone(s.mem_limit_bytes)

    def test_strips_leading_slash_from_name(self) -> None:
        s = resource_monitor.parse_inspect(_inspect_data(Name="/team2-ssh"))
        self.assertEqual(s.name, "team2-ssh")

    def test_parses_oom_killed_container(self) -> None:
        data = _inspect_data(**{
            "State.Running": False,
            "State.OOMKilled": True,
            "State.ExitCode": 137,
        })
        s = resource_monitor.parse_inspect(data)
        self.assertTrue(s.oom_killed)
        self.assertFalse(s.running)
        self.assertEqual(s.exit_code, 137)

    def test_parses_memory_limit(self) -> None:
        data = _inspect_data(**{"HostConfig.Memory": 536870912})  # 512 MiB
        s = resource_monitor.parse_inspect(data)
        self.assertEqual(s.mem_limit_bytes, 536870912)

    def test_zero_memory_limit_returns_none(self) -> None:
        data = _inspect_data(**{"HostConfig.Memory": 0})
        s = resource_monitor.parse_inspect(data)
        self.assertIsNone(s.mem_limit_bytes)

    def test_parses_restart_count(self) -> None:
        data = _inspect_data(RestartCount=7)
        s = resource_monitor.parse_inspect(data)
        self.assertEqual(s.restart_count, 7)

    def test_missing_host_config_does_not_crash(self) -> None:
        data = _inspect_data()
        del data["HostConfig"]
        s = resource_monitor.parse_inspect(data)
        self.assertIsNone(s.mem_limit_bytes)


class ViolationsTests(unittest.TestCase):
    def test_no_violations_for_healthy_containers(self) -> None:
        statuses = [_status(name="team1-vuln"), _status(name="team2-vuln")]
        self.assertEqual(resource_monitor.violations(statuses), [])

    def test_oom_kill_produces_violation(self) -> None:
        s = _status(name="team1-vuln-app", oom_killed=True, mem_limit_bytes=268435456)
        result = resource_monitor.violations([s])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], resource_monitor.OOM_KILL_EVENT)
        self.assertEqual(result[0]["container"], "team1-vuln-app")
        self.assertEqual(result[0]["mem_limit_bytes"], 268435456)

    def test_restart_loop_produces_violation(self) -> None:
        threshold = resource_monitor.RESTART_LOOP_THRESHOLD
        s = _status(name="team2-vuln-app", restart_count=threshold, running=False)
        result = resource_monitor.violations([s])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], resource_monitor.RESTART_LOOP_EVENT)
        self.assertEqual(result[0]["restart_count"], threshold)

    def test_multiple_violations_all_returned(self) -> None:
        threshold = resource_monitor.RESTART_LOOP_THRESHOLD
        statuses = [
            _status(name="team1-vuln-app", oom_killed=True),
            _status(name="team2-vuln-app", restart_count=threshold),
        ]
        result = resource_monitor.violations(statuses)
        self.assertEqual(len(result), 2)
        types = {v["type"] for v in result}
        self.assertIn(resource_monitor.OOM_KILL_EVENT, types)
        self.assertIn(resource_monitor.RESTART_LOOP_EVENT, types)

    def test_below_threshold_restart_count_not_a_violation(self) -> None:
        threshold = resource_monitor.RESTART_LOOP_THRESHOLD
        s = _status(name="team1-ssh", restart_count=threshold - 1)
        self.assertEqual(resource_monitor.violations([s]), [])

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(resource_monitor.violations([]), [])


class TeamContainerNamesTests(unittest.TestCase):
    def test_two_teams_yields_six_names(self) -> None:
        names = resource_monitor.team_container_names(2)
        self.assertEqual(len(names), 6)
        for n in ("team1-vuln", "team1-ssh", "team1-vuln-app",
                  "team2-vuln", "team2-ssh", "team2-vuln-app"):
            self.assertIn(n, names)

    def test_zero_teams_returns_empty(self) -> None:
        self.assertEqual(resource_monitor.team_container_names(0), [])

    def test_one_team_yields_three_names(self) -> None:
        names = resource_monitor.team_container_names(1)
        self.assertEqual(names, ["team1-vuln", "team1-ssh", "team1-vuln-app"])


class ParseDfOutputTests(unittest.TestCase):
    _HEADER = "Filesystem     1K-blocks    Used Available Use% Mounted on\n"

    def test_parses_standard_df_output(self) -> None:
        output = self._HEADER + "overlay  20971520 5242880 15728640  25% /\n"
        result = resource_monitor.parse_df_output(output)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["used_pct"], 25)
        self.assertEqual(result["used_bytes"], 5242880 * 1024)
        self.assertEqual(result["available_bytes"], 15728640 * 1024)

    def test_parses_high_usage(self) -> None:
        output = self._HEADER + "overlay  10485760 9437184  1048576  90% /\n"
        result = resource_monitor.parse_df_output(output)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["used_pct"], 90)
        self.assertGreater(result["used_pct"], resource_monitor.DISK_PRESSURE_THRESHOLD_PCT)

    def test_parses_output_without_header(self) -> None:
        output = "overlay  10485760 5242880 5242880  50% /\n"
        result = resource_monitor.parse_df_output(output)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["used_pct"], 50)

    def test_returns_none_for_empty_output(self) -> None:
        self.assertIsNone(resource_monitor.parse_df_output(""))

    def test_returns_none_for_header_only(self) -> None:
        self.assertIsNone(resource_monitor.parse_df_output(self._HEADER))

    def test_returns_none_for_truncated_line(self) -> None:
        self.assertIsNone(resource_monitor.parse_df_output("overlay 10485760\n"))


class ViolationsDiskPressureTests(unittest.TestCase):
    def test_disk_pressure_at_threshold_produces_violation(self) -> None:
        threshold = resource_monitor.DISK_PRESSURE_THRESHOLD_PCT
        s = _status(name="team1-vuln-app", disk_used_pct=threshold, disk_available_bytes=1024)
        result = resource_monitor.violations([s])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], resource_monitor.DISK_PRESSURE_EVENT)
        self.assertEqual(result[0]["disk_used_pct"], threshold)
        self.assertEqual(result[0]["disk_available_bytes"], 1024)

    def test_disk_pressure_above_threshold_produces_violation(self) -> None:
        s = _status(name="team2-vuln-app", disk_used_pct=95, disk_available_bytes=512)
        result = resource_monitor.violations([s])
        types = [v["type"] for v in result]
        self.assertIn(resource_monitor.DISK_PRESSURE_EVENT, types)

    def test_disk_below_threshold_is_not_a_violation(self) -> None:
        threshold = resource_monitor.DISK_PRESSURE_THRESHOLD_PCT
        s = _status(name="team1-vuln", disk_used_pct=threshold - 1)
        self.assertEqual(resource_monitor.violations([s]), [])

    def test_no_disk_info_is_not_a_violation(self) -> None:
        s = _status(name="team1-ssh")
        self.assertIsNone(s.disk_used_pct)
        self.assertEqual(resource_monitor.violations([s]), [])

    def test_disk_pressure_and_restart_loop_both_reported(self) -> None:
        threshold_disk = resource_monitor.DISK_PRESSURE_THRESHOLD_PCT
        threshold_restart = resource_monitor.RESTART_LOOP_THRESHOLD
        s = _status(
            name="team1-vuln-app",
            restart_count=threshold_restart,
            disk_used_pct=threshold_disk,
        )
        result = resource_monitor.violations([s])
        types = {v["type"] for v in result}
        self.assertIn(resource_monitor.RESTART_LOOP_EVENT, types)
        self.assertIn(resource_monitor.DISK_PRESSURE_EVENT, types)


class EventConstantsTests(unittest.TestCase):
    def test_oom_kill_event_constant(self) -> None:
        self.assertEqual(resource_monitor.OOM_KILL_EVENT, "resource.oom_kill")

    def test_restart_loop_event_constant(self) -> None:
        self.assertEqual(resource_monitor.RESTART_LOOP_EVENT, "resource.restart_loop")

    def test_disk_pressure_event_constant(self) -> None:
        self.assertEqual(resource_monitor.DISK_PRESSURE_EVENT, "resource.disk_pressure")

    def test_restart_loop_threshold_is_positive(self) -> None:
        self.assertGreater(resource_monitor.RESTART_LOOP_THRESHOLD, 0)

    def test_disk_pressure_threshold_is_reasonable(self) -> None:
        pct = resource_monitor.DISK_PRESSURE_THRESHOLD_PCT
        self.assertGreaterEqual(pct, 50)
        self.assertLessEqual(pct, 100)


if __name__ == "__main__":
    unittest.main()
