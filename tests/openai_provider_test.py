#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import AgentType, BudgetPolicy, ModelRequest
from bot_lib.model_gateway import (
    ModelGatewayError,
    ModelGatewayResponseError,
    ModelGatewayTimeout,
)
from bot_lib.openai_provider import OpenAIProvider


def _request() -> ModelRequest:
    return ModelRequest(
        agent_id="team1-agent",
        agent_type=AgentType.ATTACK_DEFENSE,
        run_id="run-1",
        correlation_id="corr-1",
        system_prompt="Choose a safe action.",
        observation={"opponent_teams": [2]},
        tool_schemas=[
            {
                "id": "recon.health",
                "description": "Check health",
                "scope": "target",
                "parameters": {
                    "target_team": {"type": "integer"},
                    "vuln_type": {
                        "type": "string",
                        "enum": ["path_traversal", "command_injection"],
                    },
                },
                "required": ["target_team"],
            }
        ],
        budget=BudgetPolicy(max_output_tokens=100),
        team_id=1,
    )


def _response_payload(plan: dict | None = None) -> dict:
    plan = plan or {
        "tool_calls": [
            {
                "call_id": "call-1",
                "tool_id": "recon.health",
                "arguments": {"target_team": 2},
            }
        ]
    }
    return {
        "id": "resp_123",
        "model": "gpt-5.4-mini",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(plan)}],
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def _function_call_payload(arguments: dict | str | None = None) -> dict:
    if arguments is None:
        arguments = {"target_team": 2}
    return {
        "id": "resp_123",
        "model": "gpt-5.4-mini",
        "output": [
            {
                "type": "function_call",
                "id": "fc_123",
                "call_id": "call-1",
                "name": "sandcastle_tool_1",
                "arguments": (
                    arguments if isinstance(arguments, str) else json.dumps(arguments)
                ),
                "status": "completed",
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


class StubResponse:
    def __init__(self, payload: dict, request_id: str = "req_123") -> None:
        self.payload = json.dumps(payload).encode()
        self.headers = {"x-request-id": request_id}

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _assert_strict_object_schemas(test: unittest.TestCase, schema: dict) -> None:
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        test.assertIsInstance(properties, dict)
        test.assertEqual(sorted(schema.get("required", [])), sorted(properties))
        test.assertFalse(schema.get("additionalProperties"))
    if isinstance(schema.get("properties"), dict):
        for nested in schema["properties"].values():
            if isinstance(nested, dict):
                _assert_strict_object_schemas(test, nested)
    if isinstance(schema.get("items"), dict):
        _assert_strict_object_schemas(test, schema["items"])
    if isinstance(schema.get("anyOf"), list):
        for nested in schema["anyOf"]:
            if isinstance(nested, dict):
                _assert_strict_object_schemas(test, nested)


class OpenAIProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenAIProvider(
            api_key="test-key",
            model_id="gpt-5.4-mini",
            input_cost_per_million=0.75,
            output_cost_per_million=4.50,
        )

    def test_requires_key_and_model(self) -> None:
        with self.assertRaises(ValueError):
            OpenAIProvider(api_key="", model_id="gpt-5.4-mini")
        with self.assertRaises(ValueError):
            OpenAIProvider(api_key="key", model_id="")

    def test_request_uses_responses_function_tools(self) -> None:
        body = self.provider._request_body(_request())
        self.assertEqual(body["model"], "gpt-5.4-mini")
        self.assertEqual(body["tool_choice"], "auto")
        self.assertTrue(body["parallel_tool_calls"])
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertNotIn("text", body)
        tool = body["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["name"], "sandcastle_tool_1")
        self.assertIn("recon.health", tool["description"])
        self.assertTrue(tool["strict"])
        arguments = tool["parameters"]
        _assert_strict_object_schemas(self, arguments)
        self.assertEqual(arguments["required"], ["target_team", "vuln_type"])
        self.assertEqual(arguments["properties"]["target_team"]["type"], "integer")
        self.assertEqual(arguments["properties"]["vuln_type"]["type"], ["string", "null"])
        self.assertEqual(
            arguments["properties"]["vuln_type"]["enum"],
            ["path_traversal", "command_injection", None],
        )

    def test_parses_tool_calls_usage_request_id_and_cost(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=StubResponse(_function_call_payload({"target_team": 2, "vuln_type": None})),
        ):
            response = self.provider.complete(_request(), timeout=2.0)
        self.assertEqual(response.tool_calls[0].tool_id, "recon.health")
        self.assertEqual(response.tool_calls[0].arguments, {"target_team": 2})
        self.assertEqual(response.usage.provider_request_id, "req_123")
        self.assertEqual(response.usage.total_tokens, 120)
        self.assertAlmostEqual(response.usage.cost_usd or 0, 0.000165)

    def test_parses_refusal_without_tool_calls(self) -> None:
        payload = _response_payload()
        payload["output"][0]["content"] = [{"type": "refusal", "refusal": "cannot comply"}]
        with patch("urllib.request.urlopen", return_value=StubResponse(payload)):
            response = self.provider.complete(_request(), timeout=2.0)
        self.assertEqual(response.finish_reason, "refused")
        self.assertEqual(response.tool_calls, [])

    def test_rejects_incomplete_or_invalid_structured_output(self) -> None:
        incomplete = _response_payload()
        incomplete["status"] = "incomplete"
        incomplete["incomplete_details"] = {"reason": "max_output_tokens"}
        with patch("urllib.request.urlopen", return_value=StubResponse(incomplete)):
            with self.assertRaises(ModelGatewayResponseError):
                self.provider.complete(_request(), timeout=2.0)

        invalid = _response_payload()
        invalid["output"][0]["content"][0]["text"] = "{not-json"
        with patch("urllib.request.urlopen", return_value=StubResponse(invalid)):
            with self.assertRaises(ModelGatewayResponseError):
                self.provider.complete(_request(), timeout=2.0)

    def test_maps_auth_rate_limit_and_timeout_errors(self) -> None:
        for status in (401, 429):
            error = urllib.error.HTTPError(
                "https://api.openai.com/v1/responses",
                status,
                "error",
                {},
                io.BytesIO(b'{"error":{"message":"provider error"}}'),
            )
            with (
                self.subTest(status=status),
                patch("urllib.request.urlopen", side_effect=error),
                self.assertRaises(ModelGatewayError),
            ):
                self.provider.complete(_request(), timeout=2.0)

        with patch("urllib.request.urlopen", side_effect=socket.timeout()):
            with self.assertRaises(ModelGatewayTimeout):
                self.provider.complete(_request(), timeout=2.0)

    def test_authorization_header_is_not_exposed_in_errors(self) -> None:
        captured = MagicMock()

        def fail(request, timeout):
            captured.request = request
            captured.timeout = timeout
            raise urllib.error.URLError("offline")

        with patch("urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(ModelGatewayError) as raised:
                self.provider.complete(_request(), timeout=3.0)
        self.assertNotIn("test-key", str(raised.exception))
        self.assertEqual(captured.timeout, 3.0)


if __name__ == "__main__":
    unittest.main()
