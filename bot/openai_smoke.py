#!/usr/bin/env python3
"""Opt-in, budget-bounded real OpenAI planning smoke test."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from bot_lib.agent_contracts import AgentType, BudgetPolicy, ModelProvider, ModelRequest
from bot_lib.model_budget import BudgetedModelGateway, ModelBudgetLedger
from bot_lib.model_gateway import ModelGateway
from bot_lib.openai_provider import OpenAIProvider


def main() -> int:
    max_cost = float(os.environ.get("ARENA_AGENT_MAX_COST_USD_PER_CALL", "0.02"))
    if max_cost > 0.02:
        print("smoke test refuses cost limits above $0.02", file=sys.stderr)
        return 2
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    model_id = os.environ.get("ARENA_AGENT_MODEL", "gpt-5.4-mini")
    policy = BudgetPolicy(
        max_actions_per_round=1,
        max_calls_per_round=1,
        max_calls_per_match=1,
        max_output_tokens=100,
        max_cost_usd_per_call=max_cost,
        max_cost_usd_per_match=max_cost,
        max_cost_usd_per_day=max_cost,
        timeout_seconds=15,
        max_retries=0,
    )
    request = ModelRequest(
        agent_id="openai-smoke",
        agent_type=AgentType.ATTACK_DEFENSE,
        run_id="openai-smoke",
        correlation_id="openai-smoke-1",
        system_prompt="Select at most one registered reconnaissance action.",
        observation={"my_team": 1, "opponent_teams": [2]},
        tool_schemas=[{"id": "recon.health", "description": "Check target health"}],
        budget=policy,
        team_id=1,
        round_number=1,
    )
    provider = OpenAIProvider(
        api_key=api_key,
        model_id=model_id,
        input_cost_per_million=float(os.environ.get("ARENA_OPENAI_INPUT_COST_PER_MTOK", "0.75")),
        output_cost_per_million=float(os.environ.get("ARENA_OPENAI_OUTPUT_COST_PER_MTOK", "4.50")),
    )
    gateway = ModelGateway(
        {ModelProvider.OPENAI: provider},
        primary_provider=ModelProvider.OPENAI,
        max_retries=0,
    )
    with tempfile.TemporaryDirectory() as tmp:
        budgeted = BudgetedModelGateway(
            gateway,
            ModelBudgetLedger(Path(tmp) / "smoke.db"),
        )
        result = budgeted.call(
            request,
            model_id=model_id,
            estimated_cost_usd=max_cost,
        )
    print(
        f"OpenAI smoke ok: model={result.response.model_id} "
        f"tool_calls={len(result.response.tool_calls)} "
        f"tokens={result.response.usage.total_tokens} "
        f"cost_usd={result.response.usage.cost_usd}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
