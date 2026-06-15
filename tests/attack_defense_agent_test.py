#!/usr/bin/env python3
"""Tests for AI-014: AttackDefenseAgent autonomous loop."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import ToolCall
from bot_lib.agent_memory import AgentMemoryStore
from bot_lib.attack_defense_agent import (
    ALL_TOOLS,
    DEFENSIVE_TOOLS,
    OFFENSIVE_TOOLS,
    TOOL_SCHEMAS,
    AttackDefenseAgent,
    FixtureActionExecutor,
    ToolRejectedError,
    _deterministic_fallback,
    AttackDefenseRunState,
)


def _call(tool_id: str, args: dict | None = None) -> ToolCall:
    return ToolCall(call_id="c1", tool_id=tool_id, arguments=args or {})


def _make_agent(
    team_id: int = 1,
    opponents: list[int] | None = None,
    db_path: str | None = None,
    **executor_kwargs,
) -> tuple[AttackDefenseAgent, AgentMemoryStore]:
    if opponents is None:
        opponents = [2, 3]
    tmp = tempfile.mkdtemp()
    db = db_path or str(Path(tmp) / "mem.db")
    mem = AgentMemoryStore(db)
    executor = FixtureActionExecutor(team_id=team_id, opponent_teams=opponents, **executor_kwargs)
    agent = AttackDefenseAgent(
        team_id=team_id, opponent_teams=opponents, memory=mem, executor=executor
    )
    return agent, mem


class ToolSchemaTests(unittest.TestCase):
    def test_all_tool_ids_in_schema(self):
        schema_ids = {s["id"] for s in TOOL_SCHEMAS}
        self.assertEqual(schema_ids, ALL_TOOLS)

    def test_offensive_and_defensive_disjoint(self):
        self.assertTrue(OFFENSIVE_TOOLS.isdisjoint(DEFENSIVE_TOOLS))

    def test_all_tools_union(self):
        self.assertEqual(ALL_TOOLS, OFFENSIVE_TOOLS | DEFENSIVE_TOOLS)


class AgentInitTests(unittest.TestCase):
    def test_self_attack_raises_on_init(self):
        tmp = tempfile.mkdtemp()
        mem = AgentMemoryStore(str(Path(tmp) / "m.db"))
        exec_ = FixtureActionExecutor(team_id=1, opponent_teams=[1, 2])
        with self.assertRaises(ValueError):
            AttackDefenseAgent(team_id=1, opponent_teams=[1, 2], memory=mem, executor=exec_)

    def test_start_returns_running_state(self):
        agent, _ = _make_agent()
        state = agent.start()
        self.assertEqual(state.status, "running")
        self.assertEqual(state.team_id, 1)
        self.assertIsNotNone(state.run_id)


class ToolValidationTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.mem = _make_agent()
        self.state = self.agent.start()

    def test_unknown_tool_raises(self):
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(self.state, _call("unknown.tool"))

    def test_self_attack_raises(self):
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(self.state, _call("attack.recon", {"target_team": 1}))

    def test_disallowed_opponent_raises(self):
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(self.state, _call("attack.recon", {"target_team": 99}))

    def test_missing_target_team_raises(self):
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(self.state, _call("attack.exploit", {}))

    def test_empty_flag_raises(self):
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(
                self.state,
                _call("attack.submit_flag", {"flag": "", "target_team": 2}),
            )

    def test_empty_diff_raises(self):
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(
                self.state,
                _call("defend.apply_patch", {"diff": ""}),
            )


class AttackSequenceTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.mem = _make_agent(team_id=1, opponents=[2, 3])
        self.state = self.agent.start()

    def test_recon_succeeds(self):
        r = self.agent.execute_tool(self.state, _call("attack.recon", {"target_team": 2}))
        self.assertEqual(r.status, "ok")

    def test_exploit_captures_flag(self):
        r = self.agent.execute_tool(self.state, _call("attack.exploit", {"target_team": 2}))
        self.assertEqual(r.status, "ok")
        self.assertIn("flag", str(r.data).lower())
        self.assertEqual(self.state.flags_captured, 1)

    def test_submit_flag_increments_counter(self):
        self.agent.execute_tool(
            self.state,
            _call(
                "attack.submit_flag",
                {"flag": "FLAG{aabbccddaabbccddaabbccddaabbccdd}", "target_team": 2},
            ),
        )
        self.assertEqual(self.state.flags_submitted, 1)

    def test_full_attack_sequence(self):
        """recon → exploit → submit_flag — all succeed."""
        self.agent.execute_tool(self.state, _call("attack.recon", {"target_team": 3}))
        r_exp = self.agent.execute_tool(self.state, _call("attack.exploit", {"target_team": 3}))
        flag = r_exp.data.get("flag", "FLAG{aabbccddaabbccddaabbccddaabbccdd}")
        self.agent.execute_tool(
            self.state,
            _call("attack.submit_flag", {"flag": flag, "target_team": 3}),
        )
        self.assertEqual(self.state.flags_captured, 1)
        self.assertEqual(self.state.flags_submitted, 1)
        self.assertEqual(len(self.state.decisions), 3)


class DefenseSequenceTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.mem = _make_agent(
            team_id=1, opponents=[2], checker_passes=True, exploit_blocked=True, patch_commits=True
        )
        self.state = self.agent.start()

    def test_inspect_files(self):
        r = self.agent.execute_tool(self.state, _call("defend.inspect_files"))
        self.assertEqual(r.status, "ok")
        self.assertIn("files", r.data)

    def test_read_file(self):
        r = self.agent.execute_tool(self.state, _call("defend.read_file", {"path": "app/app.py"}))
        self.assertEqual(r.status, "ok")

    def test_search_source(self):
        r = self.agent.execute_tool(self.state, _call("defend.search_source", {"pattern": "VULN"}))
        self.assertEqual(r.status, "ok")

    def test_snapshot(self):
        r = self.agent.execute_tool(self.state, _call("defend.snapshot"))
        self.assertEqual(r.status, "ok")

    def test_patch_commits(self):
        diff = "--- a/app/app.py\n+++ b/app/app.py\n@@ -1,1 +1,1 @@\n-# VULN\n+# PATCHED\n"
        r = self.agent.execute_tool(
            self.state,
            _call("defend.apply_patch", {"diff": diff, "correlation_id": "tx-001"}),
        )
        self.assertEqual(r.status, "ok")
        self.assertEqual(self.state.patch_attempts, 1)
        self.assertEqual(self.state.patches_committed, 1)

    def test_run_checker(self):
        r = self.agent.execute_tool(self.state, _call("defend.run_checker"))
        self.assertEqual(r.status, "ok")

    def test_exploit_regression(self):
        r = self.agent.execute_tool(self.state, _call("defend.run_exploit_regression"))
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.data.get("exploit_blocked"))

    def test_rollback(self):
        r = self.agent.execute_tool(self.state, _call("defend.rollback"))
        self.assertEqual(r.status, "ok")
        self.assertEqual(self.state.rollback_count, 1)

    def test_complete_defend_sequence(self):
        """inspect → search → snapshot → apply_patch → checker → exploit_regression."""
        self.agent.execute_tool(self.state, _call("defend.inspect_files"))
        self.agent.execute_tool(self.state, _call("defend.search_source", {"pattern": "VULN"}))
        self.agent.execute_tool(self.state, _call("defend.snapshot"))
        diff = "--- a/app/app.py\n+++ b/app/app.py\n@@ -1,1 +1,1 @@\n-# x\n+# y\n"
        self.agent.execute_tool(self.state, _call("defend.apply_patch", {"diff": diff}))
        self.agent.execute_tool(self.state, _call("defend.run_checker"))
        self.agent.execute_tool(self.state, _call("defend.run_exploit_regression"))
        self.assertEqual(len(self.state.decisions), 6)
        self.assertEqual(self.state.patches_committed, 1)


class MemoryTests(unittest.TestCase):
    def test_tool_results_stored_in_memory(self):
        agent, mem = _make_agent()
        state = agent.start()
        agent.execute_tool(state, _call("attack.recon", {"target_team": 2}))
        entries = mem.recent(state.run_id)
        tool_entries = [e for e in entries if e.kind == "tool_result"]
        self.assertGreaterEqual(len(tool_entries), 1)

    def test_observation_bounded(self):
        import json

        agent, _ = _make_agent()
        state = agent.start()
        obs = agent.build_observation(state)
        self.assertLessEqual(len(json.dumps(obs)), 12000)

    def test_observation_has_required_keys(self):
        agent, _ = _make_agent()
        state = agent.start()
        obs = agent.build_observation(state)
        for k in ("my_team", "opponent_teams", "current_round", "prior_results"):
            self.assertIn(k, obs)

    def test_memory_no_api_keys(self):
        agent, mem = _make_agent()
        state = agent.start()
        agent.execute_tool(state, _call("attack.recon", {"target_team": 2}))
        for entry in mem.recent(state.run_id):
            for secret in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
                self.assertNotIn(secret, str(entry.data))
                self.assertNotIn(secret, entry.summary)


class FallbackTests(unittest.TestCase):
    def test_fallback_returns_recon_action(self):
        _, mem = _make_agent()
        state = AttackDefenseRunState(run_id="r", agent_id="a", team_id=1, current_round=0)
        call = _deterministic_fallback(state, [2, 3])
        self.assertIsNotNone(call)
        self.assertEqual(call.tool_id, "attack.recon")
        self.assertIn("target_team", call.arguments)

    def test_fallback_none_when_no_opponents(self):
        state = AttackDefenseRunState(run_id="r", agent_id="a", team_id=1)
        call = _deterministic_fallback(state, [])
        self.assertIsNone(call)

    def test_fallback_rotates_targets(self):
        s1 = AttackDefenseRunState(run_id="r", agent_id="a", team_id=1, current_round=0)
        s2 = AttackDefenseRunState(run_id="r", agent_id="a", team_id=1, current_round=1)
        c1 = _deterministic_fallback(s1, [2, 3])
        c2 = _deterministic_fallback(s2, [2, 3])
        self.assertNotEqual(c1.arguments["target_team"], c2.arguments["target_team"])

    def test_stop_sets_status(self):
        agent, _ = _make_agent()
        state = agent.start()
        agent.stop(state)
        self.assertEqual(state.status, "stopped")

    def test_state_as_dict_serializable(self):
        import json

        agent, _ = _make_agent()
        state = agent.start()
        json.dumps(state.as_dict())


if __name__ == "__main__":
    unittest.main()
