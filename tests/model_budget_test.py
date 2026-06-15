#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import (
    AgentType,
    BudgetPolicy,
    ModelProvider,
    ModelRequest,
    ModelUsage,
)
from bot_lib.model_budget import (
    BudgetedModelGateway,
    ModelBudgetExceeded,
    ModelBudgetLedger,
)
from bot_lib.model_gateway import FakeModelProvider, ModelGateway, ModelGatewayError


def _request(
    *,
    run_id: str = "run-1",
    match_id: int | None = 1,
    round_number: int = 1,
    policy: BudgetPolicy | None = None,
) -> ModelRequest:
    return ModelRequest(
        agent_id="team1-agent",
        agent_type=AgentType.ATTACK_DEFENSE,
        run_id=run_id,
        correlation_id=f"corr-{run_id}-{round_number}",
        system_prompt="Choose one tool.",
        observation={"targets": [2]},
        tool_schemas=[{"id": "recon.health"}],
        budget=policy or BudgetPolicy(),
        match_id=match_id,
        round_number=round_number,
        team_id=1,
    )


class ModelBudgetLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "controller.db"
        self.ledger = ModelBudgetLedger(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reservation_persists_across_ledger_restart(self) -> None:
        reservation = self.ledger.reserve(
            _request(),
            provider=ModelProvider.OPENAI,
            model_id="test-model",
            estimated_cost_usd=0.01,
        )
        restarted = ModelBudgetLedger(self.path)
        summary = restarted.summary(run_id="run-1")
        self.assertEqual(summary["statuses"]["RESERVED"]["calls"], 1)
        restarted.reconcile(
            reservation.reservation_id,
            ModelUsage(input_tokens=10, output_tokens=2, cost_usd=0.004),
        )
        self.assertAlmostEqual(restarted.summary(run_id="run-1")["total_cost_usd"], 0.004)

    def test_missing_cost_metadata_charges_reserved_amount(self) -> None:
        reservation = self.ledger.reserve(
            _request(),
            provider=ModelProvider.OPENAI,
            model_id="test-model",
            estimated_cost_usd=0.02,
        )
        self.ledger.reconcile(
            reservation.reservation_id,
            ModelUsage(input_tokens=10, output_tokens=5),
        )
        self.assertAlmostEqual(self.ledger.summary(run_id="run-1")["total_cost_usd"], 0.02)

    def test_exact_cost_boundary_is_allowed_then_next_call_is_rejected(self) -> None:
        policy = BudgetPolicy(
            max_cost_usd_per_call=0.05,
            max_cost_usd_per_match=0.05,
            max_cost_usd_per_day=1.0,
        )
        reservation = self.ledger.reserve(
            _request(policy=policy),
            provider=ModelProvider.OPENAI,
            model_id="test-model",
            estimated_cost_usd=0.05,
        )
        self.ledger.reconcile(reservation.reservation_id, ModelUsage(cost_usd=0.05))
        with self.assertRaises(ModelBudgetExceeded) as raised:
            self.ledger.reserve(
                _request(round_number=2, policy=policy),
                provider=ModelProvider.OPENAI,
                model_id="test-model",
                estimated_cost_usd=0.001,
            )
        self.assertIn(raised.exception.rejection.scope, {"run", "match"})

    def test_round_call_limit_is_enforced(self) -> None:
        policy = BudgetPolicy(max_calls_per_round=1)
        self.ledger.reserve(
            _request(policy=policy),
            provider=ModelProvider.FAKE,
            model_id="fake-v1",
            estimated_cost_usd=0,
        )
        with self.assertRaises(ModelBudgetExceeded) as raised:
            self.ledger.reserve(
                _request(policy=policy),
                provider=ModelProvider.FAKE,
                model_id="fake-v1",
                estimated_cost_usd=0,
            )
        self.assertEqual(raised.exception.rejection.code, "ROUND_CALL_LIMIT")

    def test_concurrent_reservations_cannot_overspend(self) -> None:
        policy = BudgetPolicy(
            max_calls_per_round=20,
            max_calls_per_match=20,
            max_cost_usd_per_call=0.05,
            max_cost_usd_per_match=0.05,
            max_cost_usd_per_day=1.0,
        )

        def reserve(index: int) -> bool:
            try:
                self.ledger.reserve(
                    _request(
                        run_id="concurrent",
                        round_number=index + 1,
                        policy=policy,
                    ),
                    provider=ModelProvider.OPENAI,
                    model_id="test-model",
                    estimated_cost_usd=0.01,
                )
                return True
            except ModelBudgetExceeded:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            outcomes = list(executor.map(reserve, range(10)))
        self.assertEqual(sum(outcomes), 5)
        self.assertAlmostEqual(self.ledger.summary(run_id="concurrent")["total_cost_usd"], 0.05)

    def test_stale_reservation_is_released(self) -> None:
        now = [1000.0]
        ledger = ModelBudgetLedger(
            self.path,
            stale_after_seconds=10,
            clock=lambda: now[0],
        )
        ledger.reserve(
            _request(),
            provider=ModelProvider.OPENAI,
            model_id="test-model",
            estimated_cost_usd=0.01,
        )
        now[0] = 1011.0
        self.assertEqual(ledger.recover_stale(), 1)
        self.assertEqual(ledger.summary(run_id="run-1")["statuses"]["RELEASED"]["calls"], 1)


class BudgetedGatewayTest(unittest.TestCase):
    def test_reserves_each_provider_attempt_and_charges_paid_failure(self) -> None:
        class FailingOpenAI:
            provider = ModelProvider.OPENAI
            model_id = "paid-test"

            def complete(self, request, timeout):
                del request, timeout
                raise ModelGatewayError("failed")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = ModelBudgetLedger(Path(tmp) / "controller.db")
            gateway = ModelGateway(
                {ModelProvider.OPENAI: FailingOpenAI()},
                primary_provider=ModelProvider.OPENAI,
                max_retries=0,
            )
            budgeted = BudgetedModelGateway(gateway, ledger)
            with self.assertRaises(ModelGatewayError):
                budgeted.call(
                    _request(),
                    model_id="paid-test",
                )
            summary = ledger.summary(run_id="run-1")
            self.assertEqual(summary["statuses"]["FAILED"]["calls"], 1)
            self.assertAlmostEqual(summary["total_cost_usd"], 0.25)

    def test_fake_provider_attempt_is_counted_without_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ModelBudgetLedger(Path(tmp) / "controller.db")
            gateway = ModelGateway(
                {ModelProvider.FAKE: FakeModelProvider()},
                primary_provider=ModelProvider.FAKE,
                max_retries=0,
            )
            BudgetedModelGateway(gateway, ledger).call(
                _request(),
                model_id="fake-v1",
            )
            summary = ledger.summary(run_id="run-1")
            self.assertEqual(summary["statuses"]["COMPLETED"]["calls"], 1)
            self.assertEqual(summary["total_cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
