#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import (
    AgentType,
    BudgetPolicy,
    ModelProvider,
    ModelRequest,
)
from bot_lib.model_gateway import (
    FakeModelProvider,
    ModelGateway,
    ModelGatewayError,
    ModelGatewayResponseError,
    ModelGatewayTimeout,
    safe_raw_response,
)


def _request() -> ModelRequest:
    return ModelRequest(
        agent_id="team1-agent",
        agent_type=AgentType.ATTACK_DEFENSE,
        run_id="run-1",
        correlation_id="corr-1",
        system_prompt="Choose one registered tool.",
        observation={"targets": [2]},
        tool_schemas=[{"id": "recon.health"}],
        budget=BudgetPolicy(timeout_seconds=1.0),
        team_id=1,
    )


class FakeProviderTest(unittest.TestCase):
    def test_returns_scripted_valid_response(self) -> None:
        adapter = FakeModelProvider(
            script=[
                {
                    "model_id": "fake-test",
                    "tool_calls": [
                        {
                            "call_id": "call-1",
                            "tool_id": "recon.health",
                            "arguments": {"target_team": 2},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 4, "cost_usd": 0},
                }
            ]
        )
        response = adapter.complete(_request(), timeout=1.0)
        self.assertEqual(response.tool_calls[0].tool_id, "recon.health")
        self.assertEqual(response.usage.total_tokens, 14)
        self.assertEqual(adapter.call_count, 1)

    def test_rejects_invalid_json_and_schema(self) -> None:
        invalid_json = FakeModelProvider(script=["not-json"])
        with self.assertRaises(ModelGatewayResponseError):
            invalid_json.complete(_request(), timeout=1.0)

        invalid_schema = FakeModelProvider(script=[{"tool_calls": "wrong"}])
        with self.assertRaises(ModelGatewayResponseError):
            invalid_schema.complete(_request(), timeout=1.0)

    def test_timeout_is_typed(self) -> None:
        adapter = FakeModelProvider(delay_seconds=2.0)
        with self.assertRaises(ModelGatewayTimeout):
            adapter.complete(_request(), timeout=1.0)

    def test_raw_response_is_redacted_and_bounded(self) -> None:
        raw = {
            "authorization": "Bearer secret-value",
            "message": "captured FLAG{0123456789abcdef0123456789abcdef}",
            "body": "x" * 100,
        }
        safe = safe_raw_response(raw, limit=80)
        self.assertNotIn("secret-value", safe)
        self.assertNotIn("0123456789abcdef", safe)
        self.assertLessEqual(len(safe), 80)


class ModelGatewayTest(unittest.TestCase):
    def test_retries_primary_then_succeeds(self) -> None:
        primary = FakeModelProvider(
            script=[
                ModelGatewayError("transient"),
                {"model_id": "fake-v1", "tool_calls": []},
            ]
        )
        gateway = ModelGateway(
            {ModelProvider.FAKE: primary},
            primary_provider=ModelProvider.FAKE,
            max_retries=1,
        )
        result = gateway.call(_request())
        self.assertEqual(result.provider_attempts, ("fake", "fake"))
        self.assertFalse(result.used_fallback)

    def test_uses_fallback_after_bounded_primary_failure(self) -> None:
        class FailingOpenAI:
            provider = ModelProvider.OPENAI

            def complete(self, request: ModelRequest, timeout: float):
                del request, timeout
                raise ModelGatewayError("unavailable")

        fallback = FakeModelProvider(script=[{"model_id": "fake-fallback", "tool_calls": []}])
        gateway = ModelGateway(
            {
                ModelProvider.OPENAI: FailingOpenAI(),
                ModelProvider.FAKE: fallback,
            },
            primary_provider=ModelProvider.OPENAI,
            fallback_provider=ModelProvider.FAKE,
            max_retries=1,
        )
        result = gateway.call(_request())
        self.assertEqual(result.provider_attempts, ("openai", "openai", "fake"))
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.response.finish_reason, "fallback")

    def test_raises_after_exhaustion_without_fallback(self) -> None:
        adapter = FakeModelProvider(script=[ModelGatewayError("failed")])
        gateway = ModelGateway(
            {ModelProvider.FAKE: adapter},
            primary_provider=ModelProvider.FAKE,
            max_retries=0,
        )
        with self.assertRaises(ModelGatewayError):
            gateway.call(_request())


if __name__ == "__main__":
    unittest.main()
