#!/usr/bin/env python3
"""Tests for AI-011: ChallengeGeneratorAgent iterative tool loop."""

from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import ToolCall
from bot_lib.agent_memory import AgentMemoryStore
from challenge.agent import (
    ChallengeGeneratorAgent,
    ToolRejectedError,
    ALLOWED_TOOL_IDS,
)
from challenge.registry import ChallengeRegistry
from challenge.validator import ChallengeValidator


def _call(tool_id: str, args: dict | None = None) -> ToolCall:
    return ToolCall(call_id="c1", tool_id=tool_id, arguments=args or {})


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name) / "staging"
        self.reg_root = Path(self._tmp.name) / "published"
        self.db_path = Path(self._tmp.name) / "mem.db"
        self.staging.mkdir()
        self.memory = AgentMemoryStore(str(self.db_path))
        self.registry = ChallengeRegistry(self.reg_root)
        self.validator = ChallengeValidator(docker=False)
        self.agent = ChallengeGeneratorAgent(
            memory=self.memory,
            registry=self.registry,
            validator=self.validator,
            staging_root=self.staging,
            max_attempts=5,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _start(self):
        return self.agent.start({"max_attempts": 5})

    def test_start_returns_running_state(self):
        state = self._start()
        self.assertEqual(state.status, "running")
        self.assertIsNotNone(state.run_id)

    def test_unknown_tool_raises(self):
        state = self._start()
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(state, _call("unknown.tool"))

    def test_create_spec_succeeds(self):
        state = self._start()
        result = self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 10, "vulnerability": "path_traversal", "difficulty": "easy"},
            ),
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(state.current_spec)

    def test_revise_without_create_raises(self):
        state = self._start()
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(state, _call("challenge.spec.revise", {"seed": 99}))

    def test_render_without_create_raises(self):
        state = self._start()
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(state, _call("challenge.render"))

    def test_validate_without_render_raises(self):
        state = self._start()
        self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 11, "vulnerability": "sql_injection", "difficulty": "easy"},
            ),
        )
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(state, _call("challenge.validate"))

    def test_publish_without_passed_validation_raises(self):
        state = self._start()
        with self.assertRaises(ToolRejectedError):
            self.agent.execute_tool(state, _call("challenge.publish"))

    def test_tool_results_stored_in_memory(self):
        state = self._start()
        self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 12, "vulnerability": "command_injection", "difficulty": "easy"},
            ),
        )
        entries = self.memory.recent(state.run_id)
        tool_entries = [e for e in entries if e.kind == "tool_result"]
        self.assertGreaterEqual(len(tool_entries), 1)
        self.assertEqual(tool_entries[0].data.get("tool_id"), "challenge.spec.create")

    def test_tool_history_grows_with_calls(self):
        state = self._start()
        self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 13, "vulnerability": "path_traversal", "difficulty": "easy"},
            ),
        )
        self.agent.execute_tool(state, _call("challenge.render"))
        self.assertEqual(len(state.tool_history), 2)

    def test_full_happy_path_fake_provider(self):
        """Simulates one failed attempt, one revision, then publish."""
        state = self._start()

        # Create spec
        r = self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 20, "vulnerability": "path_traversal", "difficulty": "easy"},
            ),
        )
        self.assertEqual(r.status, "ok")

        # Render
        r = self.agent.execute_tool(state, _call("challenge.render"))
        self.assertEqual(r.status, "ok")
        render_id = state.last_render_id
        self.assertIsNotNone(render_id)

        # Validate (fixture mode → passes)
        r = self.agent.execute_tool(state, _call("challenge.validate"))
        self.assertEqual(r.status, "ok")
        self.assertEqual(state.last_validation["status"], "passed")

        # Publish
        r = self.agent.execute_tool(state, _call("challenge.publish"))
        self.assertEqual(r.status, "ok")
        self.assertEqual(state.status, "published")
        self.assertIsNotNone(state.published_challenge_id)

        # Challenge is in registry
        items = self.registry.list()
        self.assertEqual(len(items), 1)

    def test_cancel_sets_status(self):
        state = self._start()
        self.agent.cancel(state)
        self.assertEqual(state.status, "cancelled")

    def test_discard_clears_render(self):
        state = self._start()
        self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 21, "vulnerability": "sql_injection", "difficulty": "easy"},
            ),
        )
        self.agent.execute_tool(state, _call("challenge.render"))
        self.assertIsNotNone(state.last_render_id)
        self.agent.execute_tool(state, _call("challenge.discard"))
        self.assertIsNone(state.last_render_id)

    def test_observation_is_bounded(self):
        import json

        state = self._start()
        obs = self.agent.build_observation(state)
        self.assertLessEqual(len(json.dumps(obs)), 10000)

    def test_inspect_errors_returns_bounded_string(self):
        state = self._start()
        r = self.agent.execute_tool(state, _call("challenge.inspect_errors"))
        self.assertEqual(r.status, "ok")
        self.assertLessEqual(len(r.summary), 2000)

    def test_revise_changes_spec(self):
        state = self._start()
        self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 30, "vulnerability": "path_traversal", "difficulty": "easy"},
            ),
        )
        original_seed = state.current_spec["seed"]
        self.agent.execute_tool(state, _call("challenge.spec.revise", {"seed": 99}))
        self.assertEqual(state.current_spec["seed"], 99)
        self.assertNotEqual(original_seed, 99)

    def test_allowed_tools_match_schema(self):
        from challenge.agent import TOOL_SCHEMAS

        schema_ids = {s["id"] for s in TOOL_SCHEMAS}
        self.assertEqual(schema_ids, ALLOWED_TOOL_IDS)

    def test_memory_entries_do_not_contain_api_keys(self):
        state = self._start()
        self.agent.execute_tool(
            state,
            _call(
                "challenge.spec.create",
                {"seed": 40, "vulnerability": "path_traversal", "difficulty": "easy"},
            ),
        )
        entries = self.memory.recent(state.run_id)
        for entry in entries:
            for secret in ("OPENAI_API_KEY", "GEMINI_API_KEY", "sk-", "AIza"):
                self.assertNotIn(secret, str(entry.data))
                self.assertNotIn(secret, entry.summary)


if __name__ == "__main__":
    unittest.main()
