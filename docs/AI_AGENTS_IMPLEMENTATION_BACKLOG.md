# Sandcastle AI Agents Implementation Backlog

Plan date: 2026-06-15

This document tracks the product implementation for two AI agents:

1. `ChallengeGeneratorAgent` runs before a match and creates a validated
   vulnerable service variant.
2. `AttackDefenseAgent` runs as a team bot during a match and chooses attack
   and defense tools until stopped.

The implementation goal is not model cleverness. The goal is a reproducible,
bounded, observable workflow where OpenAI, Gemini, or the deterministic fake
provider can drive typed tools without exposing provider keys to team
containers.

## Current Product Flow

### ChallengeGeneratorAgent

Status: implemented as a model-driven controller workflow.

Operators use Challenge Lab in the visualizer to select:

- vulnerability type: `path_traversal`, `command_injection`, or
  `sql_injection`;
- difficulty: `easy` or `medium`;
- seed and decoy endpoint count;
- provider/model: `fake`, `openai`, or `gemini`;
- maximum attempts.

The bot controller creates an organizer-scoped run, then calls the configured
provider through the budgeted model gateway. Each model response may select one
registered challenge tool:

```text
challenge.spec.create
challenge.spec.revise
challenge.render
challenge.validate
challenge.inspect_errors
challenge.publish
challenge.discard
```

The model never writes files directly. It proposes a `ChallengeSpec` and the
local renderer/validator/registry perform all filesystem, validation, and
publication work. Fake mode follows the same gateway contract with deterministic
tool choices.

Published challenge artifacts are summarized in API payloads and agent memory,
including the generated file tree. Deploying a challenge runs:

```bash
./scripts/setup.sh --template challenges/published/<challenge_id> --overwrite-services
./scripts/arena.sh up
```

This copies the published artifact into every team service workspace and
recreates the vulnerable app containers.

### AttackDefenseAgent

Status: implemented as a deployable model-backed bot path with attack and
defense tools.

Operators assign an A&D agent to one or more teams from the visualizer. The
assignment stores provider/model, team scope, target policy, and allowed tools.
Starting the match can prepare the newest published challenge, restart the
arena, and launch queued agents before round 1.

The deployed bot still runs inside `teamN-ssh`, but model planning happens in
`sandcastle-bot-controller` through `/plan`. Provider keys remain only in the
controller. Team containers receive deployment-scoped planning and defense
tokens.

The A&D tool surface is:

```text
attack.recon
attack.exploit
defend.inspect_files
defend.read_file
defend.search_source
defend.snapshot
defend.apply_patch
defend.run_checker
defend.run_exploit_regression
defend.rollback
```

`attack.exploit` uses the existing registered reference exploit logic and the
bot runtime submits captured flags through the gameserver authority.

Defense tools are exposed by the team-local `service_control.py` API inside
`teamN-vuln`. Requests require both the own SSH-container source IP and the
deployment-scoped defense token. The API constrains paths to the own generated
service root and uses the existing bounded defensive tools and transactional
patch workflow.

## Completed Foundations

- Stable contracts for agents, tool calls, model requests/responses, usage,
  budgets, memory, and `ChallengeSpec`.
- Provider-neutral gateway with deterministic fake provider.
- OpenAI and Gemini provider adapters behind the same gateway.
- Persistent local budget ledger with per-call, per-run, per-match, and daily
  limits.
- Authenticated `/plan` endpoint using deployment-scoped tokens.
- First-class agent run identity, memory, telemetry summaries, and safe logs.
- Deterministic challenge renderer, fixture validator, registry, publication,
  artifact summaries, and Challenge Lab UI.
- Match-plan persistence and match start integration in the visualizer.
- Deployable A&D action registry for attack, patching, checker validation, and
  exploit regression.
- Team-local defense API with source inspection, patch, checker, exploit
  regression, rollback, and token checks.

## Remaining P0 Work

### AI-019 - Product E2E With Running Containers

Add an opt-in Docker E2E that proves the real product path:

1. Generate a challenge with the fake provider from the visualizer/API path.
2. Publish it and deploy it into every team workspace.
3. Start a two-team match.
4. Launch an A&D agent for one team.
5. Capture and submit at least one opponent flag.
6. Apply or attempt a bounded defensive patch.
7. Export evidence for both agent identities.

Validation:

```bash
python3 -B tests/bot_api_test.py
python3 -B tests/two_agent_e2e_test.py
./scripts/run-tests.sh --fast
```

Optional native Docker validation should remain explicit because it rebuilds
arena containers.

### AI-020 - Rich Dashboard Evidence

Improve the visualizer details so operators can inspect:

- challenge model steps and validation stages;
- published artifact summary and file tree;
- A&D current action, target, result, and provider/model;
- captures, accepted submissions, failed actions, patch attempts, commits,
  rollbacks, model calls, token usage, and cost;
- budget exhaustion and fallback state.

Never render provider keys, planning tokens, defense tokens, raw flags, raw
source dumps, or raw chain-of-thought.

### AI-021 - Real-Model Smoke Scripts

Add explicit smoke scripts for OpenAI and Gemini that:

- require `SANDCASTLE_ALLOW_REAL_MODEL=1`;
- print provider, model, max calls, and max cost before running;
- generate one challenge or one A&D planning step;
- refuse to run without the corresponding API key;
- never run in required CI.

## Security Invariants

- Provider API keys stay only in `sandcastle-bot-controller`.
- Team containers receive scoped planning/defense tokens, never provider keys.
- Model output is untrusted and can invoke only registered typed tools.
- Offensive actions cannot target the agent's own team.
- Defensive actions are scoped to the requesting team's generated service root.
- Source paths are normalized and constrained; `.env`, keys, certs,
  `__pycache__`, and traversal escapes are rejected.
- Patch size, file count, command runtime, and output size are bounded.
- Failed generated candidates and failed patches do not become active
  implicitly.
- Sensitive values and flags are redacted before persistence or UI rendering.

## Proof Checklist

- [x] Challenge Lab can select fake/OpenAI/Gemini provider metadata.
- [x] Challenge generation uses the budgeted model gateway in the product path.
- [x] Fake provider drives the same model/tool loop deterministically.
- [x] Published challenges can be copied into every team workspace.
- [x] Match controls can deploy the newest published challenge before start.
- [x] A&D assignments can select fake/OpenAI/Gemini provider metadata.
- [x] `/plan` uses the selected deployment provider/model.
- [x] Deployed A&D bots expose attack and defense tools to the model planner.
- [x] Team-local defense tools require source IP plus scoped token.
- [x] UI build succeeds.
- [ ] Native Docker E2E proves the whole workflow against running containers.
- [ ] Real OpenAI smoke run is documented and budget-bounded.
- [ ] Real Gemini smoke run is documented and budget-bounded.

## Verification

Required offline checks:

```bash
python3 -B tests/bot_api_test.py
python3 -B tests/agent_plan_api_test.py
python3 -B tests/model_planner_test.py
python3 -B tests/openai_provider_test.py
python3 -B tests/challenge_generator_agent_test.py
python3 -B tests/two_agent_e2e_test.py
python3 -B tests/attack_defense_agent_test.py
python3 -B tests/defensive_tools_test.py
cd visualizer && npm run build
```

Full local verification remains:

```bash
./scripts/run-tests.sh
```

## Deferred Work

- unrestricted arbitrary app generation;
- model-generated Docker/Compose files;
- arbitrary shell execution tools;
- automatic local model installation;
- generation during an active scored round;
- public multi-tenant hosting;
- adversarial sandboxing for arbitrary generated code;
- challenge novelty guarantees beyond the supported templates;
- autonomous vulnerability discovery against unknown services.
