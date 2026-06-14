#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import (
    AgentMemoryEntry,
    AgentType,
    BudgetPolicy,
    ChallengeSpec,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
    canonical_json,
)
from bot_lib.arena import load_arena_defaults


class AgentContractsTest(unittest.TestCase):
    def test_agent_types_are_stable_product_values(self) -> None:
        self.assertEqual(AgentType.ATTACK_DEFENSE.value, "attack_defense")
        self.assertEqual(AgentType.CHALLENGE_GENERATOR.value, "challenge_generator")

    def test_tool_call_rejects_unknown_identifier_shape(self) -> None:
        with self.assertRaises(ValueError):
            ToolCall(call_id="call-1", tool_id="shell", arguments={})
        with self.assertRaises(ValueError):
            ToolCall(call_id="call-1", tool_id="source.read", arguments={"bad": object()})

    def test_budget_policy_enforces_cost_order_and_positive_limits(self) -> None:
        with self.assertRaises(ValueError):
            BudgetPolicy(max_calls_per_round=0)
        with self.assertRaises(ValueError):
            BudgetPolicy(
                max_cost_usd_per_call=1.0,
                max_cost_usd_per_match=0.5,
                max_cost_usd_per_day=2.0,
            )

    def test_model_request_is_deterministically_serializable(self) -> None:
        request = ModelRequest(
            agent_id="team2-agent",
            agent_type=AgentType.ATTACK_DEFENSE,
            run_id="run-1",
            correlation_id="corr-1",
            system_prompt="Choose a registered action.",
            observation={"round": 1, "targets": [1]},
            tool_schemas=[{"id": "recon.health"}],
            budget=BudgetPolicy(),
            team_id=2,
        )
        first = canonical_json(request.as_dict())
        second = canonical_json(request.as_dict())
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["agent_type"], "attack_defense")

    def test_model_request_enforces_input_size(self) -> None:
        with self.assertRaises(ValueError):
            ModelRequest(
                agent_id="agent-1",
                agent_type=AgentType.ATTACK_DEFENSE,
                run_id="run-1",
                correlation_id="corr-1",
                system_prompt="x" * 900,
                observation={"body": "y" * 900},
                tool_schemas=[],
                budget=BudgetPolicy(max_input_chars=1000),
            )

    def test_model_response_omits_raw_response_from_public_dict(self) -> None:
        response = ModelResponse(
            provider=ModelProvider.FAKE,
            model_id="fake-v1",
            tool_calls=[
                ToolCall(
                    call_id="call-1",
                    tool_id="recon.health",
                    arguments={"target_team": 2},
                )
            ],
            usage=ModelUsage(input_tokens=10, output_tokens=5, cost_usd=0.0),
            raw_response="private provider payload",
        )
        self.assertNotIn("raw_response", response.as_dict())

    def test_memory_and_challenge_specs_validate_bounds(self) -> None:
        entry = AgentMemoryEntry(
            agent_id="agent-1",
            run_id="run-1",
            kind="tool.result",
            summary="health check passed",
        )
        self.assertEqual(entry.as_dict()["kind"], "tool.result")
        spec = ChallengeSpec(seed=42, vulnerability="path_traversal")
        self.assertEqual(spec.as_dict()["seed"], 42)
        with self.assertRaises(ValueError):
            ChallengeSpec(seed=1, vulnerability="remote_code_execution")
        with self.assertRaises(ValueError):
            ChallengeSpec(seed=1, vulnerability="sql_injection", route_name="../admin")

    def test_arena_agent_config_defaults_and_validation(self) -> None:
        base = (ROOT / "config" / "arena.env").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "arena.env"
            path.write_text(base, encoding="utf-8")
            defaults = load_arena_defaults(path)
            self.assertEqual(defaults.agent_provider, "fake")
            self.assertEqual(defaults.agent_max_calls_per_round, 2)

            path.write_text(
                base.replace("ARENA_AGENT_PROVIDER=fake", "ARENA_AGENT_PROVIDER=unknown"),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_arena_defaults(path)


if __name__ == "__main__":
    unittest.main()
