#!/usr/bin/env python3
"""Tests for SC-014: model-backed agent planner adapter."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.config import BotConfig
from bot_lib.model_planner import (
    BudgetConfig,
    FakePlannerAdapter,
    ModelBackedPlanner,
    PlannerError,
    PlannerInput,
    PlannerObservation,
    PlannerOutput,
    PlannerTimeoutError,
    RawTask,
    RemoteModelPlannerAdapter,
    build_action_schemas,
    build_observation,
    validate_plan,
)
from bot_lib.planners import BotTask, load_planner
from bot_lib.runtime import BotContext


def _ctx(num_teams: int = 4, my_team: int = 1) -> BotContext:
    return BotContext(
        config=BotConfig(),
        num_teams=num_teams,
        my_team=my_team,
        capabilities=frozenset({"network.attack", "network.submit"}),
    )


def _raw(*pairs: tuple[int, str]) -> list[RawTask]:
    return [RawTask(target_team=t, action_id=a) for t, a in pairs]


# ── Schema and observation builders ──────────────────────────────────────────


class ObservationTests(unittest.TestCase):
    def test_opponents_exclude_own_team(self) -> None:
        ctx = _ctx(num_teams=4, my_team=2)
        obs = build_observation(ctx, [])
        self.assertEqual(obs.opponent_teams, [1, 3, 4])
        self.assertEqual(obs.my_team, 2)

    def test_observation_includes_capabilities(self) -> None:
        ctx = _ctx()
        obs = build_observation(ctx, [])
        self.assertIn("network.attack", obs.capabilities)

    def test_observation_carries_previous_results(self) -> None:
        prev = [{"target_team": 2, "action_id": "recon.health", "round": 1}]
        ctx = _ctx()
        obs = build_observation(ctx, prev, round_number=2)
        self.assertEqual(obs.previous_results, prev)
        self.assertEqual(obs.round_number, 2)

    def test_observation_as_dict_is_json_serialisable(self) -> None:
        import json

        ctx = _ctx()
        obs = build_observation(ctx, [], round_number=1, elapsed_seconds=0.5)
        data = obs.as_dict()
        json.dumps(data)  # must not raise
        self.assertIn("opponent_teams", data)
        self.assertIn("capabilities", data)

    def test_action_schemas_filter_by_capability(self) -> None:
        ctx_no_service = _ctx()
        schemas = build_action_schemas(ctx_no_service)
        ids = {s.id for s in schemas}
        # network actions should be present
        self.assertIn("recon.health", ids)
        # watchdog requires service.control.local — should be absent without it
        self.assertNotIn("maintain.watchdog", ids)

    def test_action_schemas_include_watchdog_with_capability(self) -> None:
        ctx = BotContext(
            config=BotConfig(actions=["recon.health", "maintain.watchdog"]),
            num_teams=3,
            my_team=1,
            capabilities=frozenset({"network.attack", "network.submit", "service.control.local"}),
        )
        schemas = build_action_schemas(ctx)
        ids = {s.id for s in schemas}
        self.assertIn("maintain.watchdog", ids)

    def test_action_schema_as_dict(self) -> None:
        import json

        ctx = _ctx()
        schemas = build_action_schemas(ctx)
        for s in schemas:
            json.dumps(s.as_dict())  # must not raise


# ── Validation ────────────────────────────────────────────────────────────────


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_ids = frozenset({"recon.health", "exploit.sqli", "exploit.cmdi"})
        self.valid_targets = {2, 3, 4}
        self.budget = BudgetConfig(max_actions_per_round=5)

    def test_valid_tasks_are_all_accepted(self) -> None:
        output = PlannerOutput(tasks=_raw((2, "recon.health"), (3, "exploit.sqli")))
        accepted, errors = validate_plan(output, self.valid_ids, self.valid_targets, self.budget)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(errors, [])

    def test_unknown_action_id_is_rejected(self) -> None:
        output = PlannerOutput(tasks=_raw((2, "hack.everything")))
        accepted, errors = validate_plan(output, self.valid_ids, self.valid_targets, self.budget)
        self.assertEqual(accepted, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("hack.everything", errors[0])

    def test_invalid_target_team_is_rejected(self) -> None:
        output = PlannerOutput(tasks=_raw((99, "recon.health")))
        accepted, errors = validate_plan(output, self.valid_ids, self.valid_targets, self.budget)
        self.assertEqual(accepted, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("99", errors[0])

    def test_own_team_as_target_is_rejected(self) -> None:
        # valid_targets excludes own team — team 1 is not in {2,3,4}
        output = PlannerOutput(tasks=_raw((1, "exploit.sqli")))
        accepted, errors = validate_plan(output, self.valid_ids, self.valid_targets, self.budget)
        self.assertEqual(accepted, [])
        self.assertGreater(len(errors), 0)

    def test_action_budget_truncates_excess_tasks(self) -> None:
        budget = BudgetConfig(max_actions_per_round=2)
        tasks = _raw((2, "recon.health"), (3, "recon.health"), (4, "recon.health"))
        output = PlannerOutput(tasks=tasks)
        accepted, errors = validate_plan(output, self.valid_ids, self.valid_targets, budget)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("exhausted", errors[0])

    def test_token_budget_violation_appends_error(self) -> None:
        budget = BudgetConfig(max_tokens=50)
        output = PlannerOutput(tasks=[], tokens_used=200)
        _, errors = validate_plan(output, self.valid_ids, self.valid_targets, budget)
        self.assertTrue(any("token" in e for e in errors))

    def test_cost_budget_violation_appends_error(self) -> None:
        budget = BudgetConfig(max_cost_usd=0.01)
        output = PlannerOutput(tasks=[], cost_usd=0.05)
        _, errors = validate_plan(output, self.valid_ids, self.valid_targets, budget)
        self.assertTrue(any("cost" in e for e in errors))

    def test_mixed_valid_and_invalid_accepts_only_valid(self) -> None:
        tasks = _raw(
            (2, "recon.health"),  # valid
            (2, "nonexistent.action"),  # unknown
            (99, "recon.health"),  # bad target
            (3, "exploit.sqli"),  # valid
        )
        output = PlannerOutput(tasks=tasks)
        accepted, errors = validate_plan(output, self.valid_ids, self.valid_targets, self.budget)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0].action_id, "recon.health")
        self.assertEqual(accepted[1].action_id, "exploit.sqli")
        self.assertEqual(len(errors), 2)


# ── FakePlannerAdapter ────────────────────────────────────────────────────────


class FakePlannerAdapterTests(unittest.TestCase):
    def test_scripted_tasks_returned_in_order(self) -> None:
        round1 = _raw((2, "recon.health"))
        round2 = _raw((3, "exploit.sqli"))
        adapter = FakePlannerAdapter(script=[round1, round2])
        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=4, opponent_teams=[2, 3, 4], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        out1 = adapter.call(dummy_input, timeout=5.0)
        out2 = adapter.call(dummy_input, timeout=5.0)
        self.assertEqual(out1.tasks[0].action_id, "recon.health")
        self.assertEqual(out2.tasks[0].action_id, "exploit.sqli")
        self.assertEqual(adapter.call_count, 2)

    def test_returns_empty_after_script_exhausted(self) -> None:
        adapter = FakePlannerAdapter(script=[_raw((2, "recon.health"))])
        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        adapter.call(dummy_input, timeout=5.0)  # consume script
        out = adapter.call(dummy_input, timeout=5.0)
        self.assertEqual(out.tasks, [])

    def test_scripted_exception_is_raised(self) -> None:
        adapter = FakePlannerAdapter(script=[PlannerError("planned failure")])
        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        with self.assertRaises(PlannerError):
            adapter.call(dummy_input, timeout=5.0)

    def test_scripted_exception_class_is_instantiated_and_raised(self) -> None:
        adapter = FakePlannerAdapter(script=[PlannerTimeoutError])
        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        with self.assertRaises(PlannerTimeoutError):
            adapter.call(dummy_input, timeout=5.0)

    def test_delay_exceeding_timeout_raises_timeout_error(self) -> None:
        adapter = FakePlannerAdapter(delay_seconds=5.0)
        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        with self.assertRaises(PlannerTimeoutError):
            adapter.call(dummy_input, timeout=1.0)

    def test_tokens_and_model_id_are_reported(self) -> None:
        adapter = FakePlannerAdapter(
            script=[_raw((2, "recon.health"))],
            tokens_per_call=42,
            model_id="test-model",
        )
        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        out = adapter.call(dummy_input, timeout=5.0)
        self.assertEqual(out.tokens_used, 42)
        self.assertEqual(out.model_id, "test-model")


# ── ModelBackedPlanner ────────────────────────────────────────────────────────


class ModelBackedPlannerTests(unittest.TestCase):
    def test_valid_plan_yields_bot_tasks(self) -> None:
        adapter = FakePlannerAdapter(script=[_raw((2, "recon.health"), (3, "recon.health"))])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx(num_teams=4, my_team=1)
        tasks = list(planner.plan(ctx))
        self.assertEqual(len(tasks), 2)
        self.assertIsInstance(tasks[0], BotTask)
        self.assertEqual(tasks[0].target_team, 2)
        self.assertEqual(tasks[0].action_id, "recon.health")

    def test_invalid_action_id_dropped_silently(self) -> None:
        adapter = FakePlannerAdapter(script=[_raw((2, "does.not.exist"))])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx()
        tasks = list(planner.plan(ctx))
        self.assertEqual(tasks, [])

    def test_invalid_target_team_dropped_silently(self) -> None:
        adapter = FakePlannerAdapter(script=[_raw((99, "recon.health"))])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx(num_teams=4, my_team=1)
        tasks = list(planner.plan(ctx))
        self.assertEqual(tasks, [])

    def test_own_team_target_is_rejected(self) -> None:
        adapter = FakePlannerAdapter(script=[_raw((1, "exploit.sqli"))])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx(num_teams=4, my_team=1)
        tasks = list(planner.plan(ctx))
        self.assertEqual(tasks, [])

    def test_timeout_does_not_crash_bot_loop(self) -> None:
        adapter = FakePlannerAdapter(script=[PlannerTimeoutError("simulated timeout")])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx()
        tasks = list(planner.plan(ctx))  # must not raise
        self.assertEqual(tasks, [])

    def test_planner_error_does_not_crash_bot_loop(self) -> None:
        adapter = FakePlannerAdapter(script=[PlannerError("network down")])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx()
        tasks = list(planner.plan(ctx))
        self.assertEqual(tasks, [])

    def test_unexpected_exception_does_not_crash_bot_loop(self) -> None:
        adapter = FakePlannerAdapter(script=[RuntimeError("unexpected")])
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx()
        tasks = list(planner.plan(ctx))
        self.assertEqual(tasks, [])

    def test_action_budget_enforced(self) -> None:
        tasks_in = _raw((2, "recon.health"), (3, "recon.health"), (4, "recon.health"))
        adapter = FakePlannerAdapter(script=[tasks_in])
        budget = BudgetConfig(max_actions_per_round=2)
        planner = ModelBackedPlanner(adapter=adapter, budget=budget)
        ctx = _ctx(num_teams=5, my_team=1)
        tasks_out = list(planner.plan(ctx))
        self.assertEqual(len(tasks_out), 2)

    def test_retry_after_failure_returns_next_script_entry(self) -> None:
        script = [
            PlannerError("transient"),
            _raw((2, "recon.health")),
        ]
        adapter = FakePlannerAdapter(script=script)
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx()
        first = list(planner.plan(ctx))  # failure → empty
        second = list(planner.plan(ctx))  # success → tasks
        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)

    def test_previous_results_passed_to_next_round(self) -> None:
        captured_inputs: list[PlannerInput] = []

        class CapturingAdapter:
            def call(self, planner_input: PlannerInput, timeout: float) -> PlannerOutput:
                captured_inputs.append(planner_input)
                return PlannerOutput(tasks=_raw((2, "recon.health")), tokens_used=10)

        planner = ModelBackedPlanner(adapter=CapturingAdapter())
        ctx = _ctx()
        list(planner.plan(ctx))  # round 1
        list(planner.plan(ctx))  # round 2

        self.assertEqual(len(captured_inputs), 2)
        self.assertEqual(captured_inputs[0].observation.previous_results, [])
        self.assertEqual(len(captured_inputs[1].observation.previous_results), 1)
        self.assertEqual(
            captured_inputs[1].observation.previous_results[0]["action_id"],
            "recon.health",
        )

    def test_round_number_increments_each_call(self) -> None:
        captured: list[int | None] = []

        class TrackingAdapter:
            def call(self, planner_input: PlannerInput, timeout: float) -> PlannerOutput:
                captured.append(planner_input.observation.round_number)
                return PlannerOutput(tasks=[])

        planner = ModelBackedPlanner(adapter=TrackingAdapter())
        ctx = _ctx()
        list(planner.plan(ctx))
        list(planner.plan(ctx))
        list(planner.plan(ctx))
        self.assertEqual(captured, [1, 2, 3])

    def test_targets_excludes_own_team(self) -> None:
        adapter = FakePlannerAdapter()
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx(num_teams=4, my_team=2)
        self.assertEqual(planner.targets(ctx), [1, 3, 4])

    def test_override_target_restricts_targets(self) -> None:
        adapter = FakePlannerAdapter()
        planner = ModelBackedPlanner(adapter=adapter)
        ctx = _ctx(num_teams=4, my_team=1)
        self.assertEqual(planner.targets(ctx, override_target=3), [3])

    def test_model_usage_event_emitted(self) -> None:
        adapter = FakePlannerAdapter(
            script=[_raw((2, "recon.health"))],
            tokens_per_call=77,
            cost_per_call=0.001,
        )
        planner = ModelBackedPlanner(adapter=adapter)
        with tempfile.TemporaryDirectory() as tmp:
            event_file = str(Path(tmp) / "events.jsonl")
            ctx = BotContext(
                config=BotConfig(deployment_id="test"),
                num_teams=3,
                my_team=1,
                capabilities=frozenset({"network.attack", "network.submit"}),
                event_file=event_file,
            )
            list(planner.plan(ctx))
            events = [json.loads(line) for line in Path(event_file).read_text().splitlines()]
        types = [e["type"] for e in events]
        self.assertIn("planner.model_usage", types)
        usage = next(e for e in events if e["type"] == "planner.model_usage")
        self.assertEqual(usage["tokens_used"], 77)
        self.assertAlmostEqual(usage["cost_usd"], 0.001)


# ── RemoteModelPlannerAdapter ─────────────────────────────────────────────────


class RemoteAdapterTests(unittest.TestCase):
    def test_rejects_empty_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            RemoteModelPlannerAdapter("", "tok")

    def test_parses_well_formed_response(self) -> None:
        import json
        from unittest.mock import MagicMock

        response_body = json.dumps(
            {
                "tasks": [{"target_team": 2, "action_id": "recon.health"}],
                "tokens_used": 50,
                "cost_usd": 0.002,
                "model_id": "test-model",
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        adapter = RemoteModelPlannerAdapter("http://localhost:9999", "secret")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            output = adapter.call(dummy_input, timeout=5.0)

        self.assertEqual(len(output.tasks), 1)
        self.assertEqual(output.tasks[0].target_team, 2)
        self.assertEqual(output.tasks[0].action_id, "recon.health")
        self.assertEqual(output.tokens_used, 50)
        self.assertAlmostEqual(output.cost_usd, 0.002)

    def test_network_error_raises_planner_error(self) -> None:
        import urllib.error

        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        adapter = RemoteModelPlannerAdapter("http://localhost:9999", "secret")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(PlannerError):
                adapter.call(dummy_input, timeout=5.0)

    def test_timeout_error_raises_planner_timeout_error(self) -> None:
        import urllib.error

        dummy_input = PlannerInput(
            observation=PlannerObservation(
                my_team=1, num_teams=3, opponent_teams=[2, 3], capabilities=[]
            ),
            action_schemas=[],
            budget=BudgetConfig(),
        )
        adapter = RemoteModelPlannerAdapter("http://localhost:9999", "secret")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(PlannerTimeoutError):
                adapter.call(dummy_input, timeout=5.0)


# ── load_planner integration ──────────────────────────────────────────────────


class LoadPlannerIntegrationTests(unittest.TestCase):
    def test_model_planner_loads_via_env(self) -> None:
        import os

        env = {
            **os.environ,
            "PLAN_ENDPOINT": "http://bot-controller:8080",
            "PLAN_TOKEN": "operator-secret",
            "PLAN_MAX_ACTIONS": "10",
            "PLAN_TIMEOUT_SECONDS": "5.0",
        }
        with patch.dict(os.environ, env):
            planner = load_planner("model")
        self.assertEqual(planner.id, "model")
        self.assertIsInstance(planner, ModelBackedPlanner)
        self.assertEqual(planner.budget.max_actions_per_round, 10)
        self.assertAlmostEqual(planner.budget.max_plan_seconds, 5.0)

    def test_unknown_planner_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            load_planner("does_not_exist")

    def test_budget_config_rejects_non_positive_actions(self) -> None:
        with self.assertRaises(ValueError):
            BudgetConfig(max_actions_per_round=0)

    def test_budget_config_rejects_non_positive_seconds(self) -> None:
        with self.assertRaises(ValueError):
            BudgetConfig(max_plan_seconds=0.0)


if __name__ == "__main__":
    unittest.main()
