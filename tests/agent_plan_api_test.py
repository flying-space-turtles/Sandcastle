#!/usr/bin/env python3
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import BudgetPolicy, ModelProvider
from bot_lib.agent_planning import (
    AgentPlanningService,
    DeterministicPlanningFakeProvider,
    PlanningCredentialStore,
    PlanningIdentity,
    PlanningRequestError,
)
from bot_lib.model_budget import BudgetedModelGateway, ModelBudgetExceeded, ModelBudgetLedger
from bot_lib.model_gateway import FakeModelProvider, ModelGateway


def _payload(*, my_team: int = 1, targets: list[int] | None = None) -> dict:
    return {
        "observation": {
            "my_team": my_team,
            "num_teams": 2,
            "opponent_teams": targets if targets is not None else [2],
            "capabilities": ["network.attack"],
            "round_number": 1,
            "previous_results": [],
            "elapsed_seconds": 0,
        },
        "action_schemas": [
            {
                "id": "recon.health",
                "label": "Health check",
                "category": "Recon",
                "scope": "target",
                "description": "Check health",
                "required_capabilities": [],
            }
        ],
        "budget": {},
    }


class PlanningCredentialStoreTest(unittest.TestCase):
    def test_issue_validate_wrong_token_expiry_and_deactivation(self) -> None:
        now = [1000.0]
        with tempfile.TemporaryDirectory() as tmp:
            store = PlanningCredentialStore(
                Path(tmp) / "controller.db",
                ttl_seconds=10,
                clock=lambda: now[0],
            )
            token = store.issue("deployment-1", 1)
            credential = store.validate(token)
            self.assertIsNotNone(credential)
            self.assertEqual(credential.team_id, 1)
            self.assertIsNone(store.validate("deployment-1.wrong"))
            now[0] = 1011.0
            self.assertIsNone(store.validate(token))

            token = store.issue("deployment-1", 1)
            store.deactivate("deployment-1")
            self.assertIsNone(store.validate(token))


class PlanningServiceTest(unittest.TestCase):
    def _service(
        self,
        temp: str,
        *,
        provider: FakeModelProvider | None = None,
        policy: BudgetPolicy | None = None,
    ) -> AgentPlanningService:
        provider = provider or DeterministicPlanningFakeProvider()
        gateway = ModelGateway(
            {ModelProvider.FAKE: provider},
            primary_provider=ModelProvider.FAKE,
            max_retries=0,
        )
        return AgentPlanningService(
            BudgetedModelGateway(
                gateway,
                ModelBudgetLedger(Path(temp) / "controller.db"),
            ),
            num_teams=2,
            model_id="fake-v1",
            budget=policy or BudgetPolicy(),
        )

    @staticmethod
    def _identity() -> PlanningIdentity:
        return PlanningIdentity(
            deployment_id="deployment-1",
            team_id=1,
            allowed_targets=frozenset({2}),
            allowed_actions=frozenset({"recon.health"}),
        )

    def test_valid_plan_returns_existing_wire_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            response = self._service(tmp).plan(self._identity(), _payload())
        self.assertEqual(
            response["tasks"],
            [{"target_team": 2, "action_id": "recon.health"}],
        )
        self.assertEqual(response["provider"], "fake")

    def test_rejects_team_target_and_action_mismatches_before_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            for payload in (
                _payload(my_team=2),
                _payload(targets=[1]),
                {
                    **_payload(),
                    "action_schemas": [{"id": "exploit.sqli"}],
                },
            ):
                with self.subTest(payload=payload), self.assertRaises(PlanningRequestError):
                    service.plan(self._identity(), payload)

    def test_rejects_provider_self_target(self) -> None:
        provider = FakeModelProvider(
            script=[
                {
                    "model_id": "fake-v1",
                    "tool_calls": [
                        {
                            "call_id": "call-1",
                            "tool_id": "recon.health",
                            "arguments": {"target_team": 1},
                        }
                    ],
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PlanningRequestError):
                self._service(tmp, provider=provider).plan(self._identity(), _payload())

    def test_budget_is_enforced_before_second_provider_call(self) -> None:
        policy = BudgetPolicy(
            max_calls_per_round=1,
            max_calls_per_match=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, policy=policy)
            service.plan(self._identity(), _payload())
            with self.assertRaises(ModelBudgetExceeded):
                service.plan(self._identity(), _payload())


class PlanningHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        database = Path(cls.temp.name) / "bot-controller.db"
        spec = importlib.util.spec_from_file_location(
            "sandcastle_plan_api_test",
            ROOT / "bot" / "bot_api.py",
        )
        assert spec is not None and spec.loader is not None
        cls.bot_api = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"BOT_CONTROLLER_DB": str(database)}):
            spec.loader.exec_module(cls.bot_api)
        cls.bot_api.STORE.insert(
            "deployment-http",
            1,
            {
                "bot_name": "Model Bot",
                "planner": "model",
                "target_policy": "all_opponents",
                "target_teams": [],
                "actions": ["recon.health"],
            },
        )
        cls.token = cls.bot_api.PLAN_CREDENTIALS.issue("deployment-http", 1)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.bot_api.BotAPIHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def _post(self, token: str | None, payload: object) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", "/plan", json.dumps(payload), headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        return response.status, body

    def _get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        return response.status, body

    def test_operator_routes_require_arena_operator_token(self) -> None:
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, _ = self._get("/match-plan")
        self.assertEqual(status, 401)
        status, _ = self._get("/match-plan", "wrong-token")
        self.assertEqual(status, 401)

        status, body = self._get("/match-plan", self.bot_api._operator_token())
        self.assertEqual(status, 200)
        self.assertIn("assignments", body)

    def test_plan_endpoint_authenticates_and_returns_tasks(self) -> None:
        status, _ = self._post(None, _payload())
        self.assertEqual(status, 401)
        status, _ = self._post("deployment-http.wrong", _payload())
        self.assertEqual(status, 401)
        status, body = self._post(self.token, _payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["tasks"][0]["target_team"], 2)

    def test_invalid_identity_request_does_not_call_provider(self) -> None:
        provider = self.bot_api.PLANNING_SERVICE.gateway.gateway.adapters[ModelProvider.FAKE]
        before = provider.call_count
        status, _ = self._post(self.token, _payload(targets=[1]))
        self.assertEqual(status, 400)
        self.assertEqual(provider.call_count, before)


if __name__ == "__main__":
    unittest.main()
