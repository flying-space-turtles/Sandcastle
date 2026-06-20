# Agent Testing And Evals

Sandcastle treats model output as untrusted input. The agentic layer is tested
by proving the contracts around model calls, then evaluated by running complete
agent workflows through the same tool surfaces used by the product.

## What Is Tested

Unit and contract tests prove that the model boundary is safe and stable.

- `tests/agent_contracts_test.py` checks the shared request, response, budget,
  memory, and `ChallengeSpec` data contracts.
- `tests/openai_provider_test.py` checks that OpenAI requests use strict
  function tools, supported reasoning settings, bounded output, safe error
  handling, request IDs, usage, and cost parsing.
- `tests/model_gateway_test.py` checks provider retries, typed failures,
  fallback behavior, timeout handling, and redaction of raw provider output.
- `tests/model_budget_test.py` checks persistent budget reservations and
  per-call, per-run, per-match, and daily cost limits.
- `tests/model_planner_test.py` checks that model-planned tasks are filtered by
  registered action IDs, valid target teams, token/cost budgets, and
  max-actions-per-round.
- `tests/agent_plan_api_test.py` checks the authenticated `/plan` path used by
  deployed agents, including planning credentials and budget rejection.

These tests do not try to prove that a model is clever. They prove that any
provider response must fit Sandcastle's typed execution contract before it can
affect the arena.

## Challenge Generation Eval

`ChallengeGeneratorAgent` is evaluated as a tool loop:

```text
observe request -> choose tool -> create spec -> render -> validate -> publish
```

`tests/challenge_generator_agent_test.py` verifies the core loop with a fake
provider and local validator:

- unknown tools are rejected;
- `challenge.render` cannot run before `challenge.spec.create`;
- `challenge.validate` cannot run before a candidate exists;
- `challenge.publish` cannot run unless validation passed;
- successful runs create a registry entry;
- tool results are persisted to memory;
- observations and validation summaries stay bounded;
- memory entries do not include provider keys.

The generated vuln app is not accepted just because the model selected a spec.
The renderer creates the files deterministically, then the validator checks the
candidate. A published challenge must pass the checker/exploit/patch contract
before it is deployable.

## Attack/Defense Eval

`AttackDefenseAgent` is evaluated through the planning and bot execution path.
The model can only choose registered actions, and each chosen action is checked
against the agent's allowed team scope.

The important guarantees are:

- offensive actions cannot target the agent's own team;
- unknown action IDs are rejected;
- invalid target teams are rejected;
- excess actions are truncated by budget;
- provider keys stay in `sandcastle-bot-controller`;
- deployed team containers receive only scoped planning and defense tokens;
- model-selected tool calls are persisted as memory for later rounds.

The fake provider follows the same gateway and planning contract as real
providers, which makes CI deterministic while still exercising the agent loop.

## Real Provider Smoke Checks

Required CI does not spend money on real model calls. Real provider checks are
opt-in smoke tests.

`scripts/smoke-openai-agent.sh` runs `bot/openai_smoke.py` only when the caller
explicitly provides a key and cost ceiling. The smoke test performs one bounded
OpenAI planning call through the same budgeted gateway used by the product.

This catches provider integration issues such as:

- invalid request shape;
- unsupported model parameters;
- malformed function-call responses;
- timeout and HTTP error mapping;
- usage and cost parsing drift.

## Staging Eval

The staging deployment is the highest-level eval. It syncs the PR to a
disposable VPS, applies staging configuration, runs Docker-in-Docker smoke
orchestration, and leaves a live arena for review.

That path proves that:

- generated topology can start from a clean host;
- controller, visualizer, gameserver, teams, firewall, and bot paths work
  together;
- generated challenge artifacts can be deployed into team workspaces;
- cleanup handles runtime-generated files;
- the same operator flows used locally work after deployment.

## How To Run The Relevant Checks

Common agent checks:

```bash
python3 -B tests/agent_contracts_test.py
python3 -B tests/model_gateway_test.py
python3 -B tests/model_budget_test.py
python3 -B tests/model_planner_test.py
python3 -B tests/agent_plan_api_test.py
python3 -B tests/openai_provider_test.py
python3 -B tests/challenge_generator_agent_test.py
```

Broader local validation:

```bash
./scripts/run-tests.sh --fast
```

Opt-in real OpenAI smoke:

```bash
SANDCASTLE_ALLOW_REAL_MODEL=1 \
OPENAI_API_KEY=... \
SANDCASTLE_OPENAI_SMOKE_MAX_COST_USD=0.10 \
./scripts/smoke-openai-agent.sh
```

## Presentation Summary

Sandcastle validates agents in layers. Contract tests prove that model requests,
tool calls, budgets, memory, and provider adapters are safe. Deterministic fake
provider runs prove the agent loop without network or cost. Real-provider smoke
checks catch API drift. Staging evals prove that the full deployed arena still
works end to end.
