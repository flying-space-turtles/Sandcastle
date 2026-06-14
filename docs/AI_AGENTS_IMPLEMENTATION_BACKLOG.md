# Sandcastle AI Agents Implementation Backlog

Plan date: 2026-06-14

This document is the implementation plan for adding two AI agents as product
features:

1. `AttackDefenseAgent` plays for an arena team by attacking opponents and
   protecting its own service.
2. `ChallengeGeneratorAgent` creates and validates vulnerable service variants
   before a match.

The tasks are complete, sequential, and topologically sorted. Every dependency
of a task appears earlier in this document. Each **Agent prompt** is intended to
be pasted into a coding agent after the preceding tasks have been merged.

Before starting any task, read:

- [`../VISION.md`](../VISION.md)
- [`../README.md`](../README.md)
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
- [`PROJECT_AUDIT_AND_BACKLOG.md`](PROJECT_AUDIT_AND_BACKLOG.md)
- [`THREAT_MODEL.md`](THREAT_MODEL.md)

## Product Goal

An organizer should be able to:

1. Ask `ChallengeGeneratorAgent` to create an easy vulnerable HTTP challenge
   from a deterministic seed.
2. Observe the agent generate, build, test, revise, and publish the challenge.
3. Start a two-team match using the published challenge.
4. Assign `AttackDefenseAgent` to one team.
5. Play against that team while the agent attacks opponents, submits captured
   flags, inspects its own service, applies bounded patches, validates SLA, and
   rolls back broken changes.
6. Export evidence that two distinct AI agents used models, tools, observations,
   and iterative decision loops.

The first implementation is a proof of concept. Model quality is not a release
criterion. Deterministic validation, bounded execution, reproducibility, and
clear evidence of agent behavior are release criteria.

## Current Foundation

The repository already provides:

- a typed bot action registry and planner contract;
- a remote model planner adapter contract and deterministic fake adapter;
- team-scoped flag submission;
- bot deployment records, logs, and event files;
- a narrow team-local service restart API;
- gameserver rounds, checkers, scoring, telemetry, and export;
- a mutable per-team copy of the TurtleNotes Flask service;
- reference exploits for path traversal, command injection, and SQL injection;
- a bot operator UI;
- trusted, filtered-proxy, and Docker-in-Docker operating modes.

The important missing behavior is:

- a real model gateway and `/plan` controller endpoint;
- safe API-key injection and enforceable cost limits;
- stable identities for the two product agents;
- real action-result feedback and persistent agent memory;
- bounded source inspection, patching, build, validation, and rollback tools;
- a deterministic challenge specification and renderer;
- an isolated challenge-validation pipeline;
- a multi-step challenge-generation agent loop;
- UI and end-to-end evidence for both agents.

## Architecture Decision

### Agent 1: AttackDefenseAgent

`AttackDefenseAgent` is one autonomous agent with one identity, one memory, and
two allowed modes:

- `ATTACK`: recon, exploit selection, flag capture, and submission;
- `DEFEND`: source inspection, patch proposal, build, checker validation,
  exploit regression, commit, and rollback.

The model chooses the next typed action. It never receives the host Docker
socket and never executes an arbitrary host command.

The runtime remains in `teamN-ssh`. Defensive operations are executed through
an authenticated, team-local control API in `teamN-vuln`, where filesystem and
service lifecycle access can be constrained to that team's generated service.

### Agent 2: ChallengeGeneratorAgent

`ChallengeGeneratorAgent` runs in the organizer control plane before a match.
It does not generate an unrestricted repository from free-form text.

The model produces and revises a versioned `ChallengeSpec` JSON document.
Deterministic local code renders that specification into a known template,
including:

- application source;
- service metadata;
- Docker build definition;
- PUT/GET/CHECK checker;
- reference exploit;
- reference patch;
- seed and generation manifest.

The agent iterates over typed tools:

```text
spec.create -> render -> build -> validate -> observe -> revise -> publish
```

A challenge can be published only after the validator proves:

- legitimate CHECK, PUT, and GET operations pass before patching;
- the reference exploit captures a planted flag before patching;
- the reference patch applies successfully;
- legitimate CHECK, PUT, and GET operations still pass after patching;
- the reference exploit no longer captures the flag after patching;
- generation is reproducible from the same specification and seed.

### Model Boundary

Model credentials remain only in `sandcastle-bot-controller`. Team containers
receive a short-lived or deployment-scoped planning token, not a provider API
key.

The initial provider order is:

1. `fake`, required for deterministic tests and CI;
2. `openai`, required for the first real-model demonstration;
3. `ollama`, optional local-model provider after the OpenAI path is stable;
4. `gemini`, optional provider if project time remains.

Provider adapters must implement one internal contract. No agent behavior may
depend directly on a provider SDK.

## Engineering Invariants

These invariants apply to every task in this backlog.

### Arena And Repository

- Preserve every invariant in `VISION.md` and `CONTRIBUTING.md`.
- `config/arena.env` remains the canonical non-secret arena configuration.
- `.env` is local-only and contains provider secrets; it must remain ignored by
  Git and excluded from Docker build contexts.
- Generated team workspaces remain under `teams/generated/`.
- Generated challenge candidates are not canonical source until explicitly
  published.
- The same challenge specification, template version, and seed must produce
  byte-identical generated source.
- Arena startup must remain usable without any model key by selecting `fake`,
  `scripted`, or disabled agent mode.

### Agent Identity

- The two required agents have stable, distinct values:
  `attack_defense` and `challenge_generator`.
- Every plan, action, tool result, model call, error, and published artifact is
  correlated with `agent_id`, `agent_type`, and a run/deployment ID.
- `AttackDefenseAgent` cannot target its own team offensively.
- `ChallengeGeneratorAgent` cannot modify a running team's workspace.
- A scripted bot or deterministic generator is a fallback, not evidence that
  an AI agent ran. AI demonstrations must persist a successful real model call.

### Model And Cost Safety

- Provider API keys never enter `teamN-ssh`, `teamN-vuln`, vulnerable
  applications, the visualizer, logs, telemetry payloads, or generated files.
- Model output is untrusted data and must pass schema and policy validation.
- Every model call has request timeout, input/output size limits, retry limits,
  and a persisted cost or usage reservation.
- Per-call, per-run, per-match, and daily hard limits are enforced by the
  controller before issuing a provider request.
- Exhausted budgets produce a typed result and deterministic fallback; they do
  not crash the arena loop.
- Tests and CI never require a real provider key or network access.
- Raw chain-of-thought is neither requested nor persisted. Store concise model
  decisions, selected tools, validation errors, and outcomes.

### Tool Safety

- Models can invoke only registered typed tools.
- Tool arguments are validated independently of model output.
- Source paths are normalized and constrained to an explicit allowlist.
- Patch size, changed-file count, execution time, output size, and build
  attempts are bounded.
- No initial agent tool exposes arbitrary shell execution.
- Defensive operations are scoped to the requesting team and service.
- Challenge builds and validation run with no provider secrets and no host
  Docker socket inside the generated service.
- Failed patches and failed generated candidates cannot become active
  implicitly.

### Functional Correctness

- A defensive patch is successful only if legitimate service behavior remains
  valid and the corresponding exploit stops working.
- A generated challenge is valid only if both its vulnerable and patched
  states are proven automatically.
- Model success is never inferred from prose. It is derived from tool results,
  checker outcomes, exploit outcomes, submissions, and persisted artifacts.
- The gameserver remains authoritative for rounds, flags, submissions, and
  scores.

### Observability And Testing

- Sensitive values and flags are redacted before persistence.
- Every new runtime behavior has focused unit tests.
- Every external provider has mocked contract tests.
- Every P0 agent workflow has a deterministic fake-model end-to-end test.
- Real-model smoke tests are explicit, opt-in, budget-bounded, and excluded from
  required CI.
- `./scripts/run-tests.sh` remains the local verification entry point.

## Configuration Contract

The implementation should converge on these local secret and non-secret
settings. Exact names may be adjusted once in AI-001, then remain stable.

Local `.env`:

```text
OPENAI_API_KEY=
GEMINI_API_KEY=
```

Canonical `config/arena.env`:

```text
ARENA_AGENT_PROVIDER=fake
ARENA_AGENT_MODEL=
ARENA_AGENT_FALLBACK_PROVIDER=fake
ARENA_AGENT_PLAN_TOKEN=
ARENA_AGENT_MAX_CALLS_PER_ROUND=2
ARENA_AGENT_MAX_CALLS_PER_MATCH=30
ARENA_AGENT_MAX_INPUT_CHARS=20000
ARENA_AGENT_MAX_OUTPUT_TOKENS=500
ARENA_AGENT_MAX_COST_USD_PER_CALL=0.05
ARENA_AGENT_MAX_COST_USD_PER_MATCH=0.50
ARENA_AGENT_MAX_COST_USD_PER_DAY=1.00
ARENA_AGENT_TIMEOUT_SECONDS=15
ARENA_AGENT_MAX_RETRIES=1
ARENA_CHALLENGE_MAX_ATTEMPTS=3
ARENA_CHALLENGE_MAX_COST_USD=0.25
```

Never commit a real secret as a default. Setup should generate a local planning
token or fail with a clear remediation when model mode requires one.

## Milestones

### A0 - Safe Model Foundation

Complete AI-001 through AI-005. The controller can call a fake or OpenAI model
through one validated, authenticated, and budgeted contract.

### A1 - Agent Identity And Evidence

Complete AI-006 and AI-007. Agent runs have stable identities, persistent
memory, result feedback, and exportable telemetry.

### A2 - Deterministic Challenge Factory

Complete AI-008 through AI-011. A validated challenge specification can be
rendered, tested in vulnerable and patched states, and published safely.

### A3 - Playable Attack/Defense Agent

Complete AI-012 through AI-014. One AI-controlled team can attack opponents and
transactionally patch its own service without direct Docker authority.

### A4 - Product Integration And Proof

Complete AI-015 through AI-018. Both agents are operable from the product,
covered by end-to-end tests, documented, and demonstrable with a bounded real
model run.

## Topologically Sorted Tasks

### AI-001 - Define Agent, Model, Budget, And Challenge Contracts

**Priority:** P0 / `agents`, `architecture`, `configuration`

**Dependencies:** existing SC-012, SC-014, SC-015, SC-016, SC-019

**Description**

Establish the stable data contracts used by every later task. This task should
add schemas and configuration only; it should not call a real model or modify
services.

**Agent prompt**

> Read VISION.md, CONTRIBUTING.md, docs/THREAT_MODEL.md, this backlog, and the
> current bot planner/action implementation. Define versioned Python data
> models for agent identity, model requests, model responses, tool calls, tool
> results, usage, budget policy, budget rejection, agent memory entries, and
> ChallengeSpec. Use standard-library dataclasses and explicit validation unless
> an existing repository dependency clearly fits. Add stable agent types
> `attack_defense` and `challenge_generator`. Extend arena configuration parsing
> and setup validation with non-secret provider and budget settings. Keep real
> provider keys in local `.env`, document them in `.env.example` with empty
> values, and ensure setup-generated Compose injects secrets only into
> bot-controller. Do not add a provider SDK or real network call. Add focused
> tests for valid and invalid schemas, configuration bounds, redaction, and
> deterministic JSON serialization. Update scripts/run-tests.sh.

**Acceptance criteria**

- Versioned request, response, usage, budget, memory, and challenge schemas
  exist.
- Both required agent types are explicit enum-like values.
- Invalid tool names, agent types, numeric limits, and challenge fields fail
  validation.
- `.env.example` contains empty provider-key placeholders.
- Real keys cannot appear in generated Compose output or public controller
  payloads.
- Configuration defaults permit the arena to run without a model key.
- Serialization is deterministic and covered by tests.

**Validation**

```bash
python3 -B tests/agent_contracts_test.py
python3 -B tests/bot_config_test.py
./scripts/setup.sh --teams 2
docker compose config --quiet
git diff --check
```

### AI-002 - Implement A Provider-Neutral Model Gateway With Fake Provider

**Priority:** P0 / `agents`, `ai`, `backend`

**Dependencies:** AI-001

**Description**

Create the internal model gateway and a deterministic fake provider. This is
the only interface agents should use for model inference.

**Agent prompt**

> Implement a provider-neutral model gateway in the bot-controller layer using
> the contracts from AI-001. Define an adapter protocol that accepts a validated
> model request and returns a validated structured model response plus usage.
> Implement a deterministic fake adapter that can return scripted tool calls,
> malformed responses, timeouts, and provider errors for tests. Add provider
> selection through arena configuration, strict response parsing, bounded raw
> response retention, one retry at most when configured, and a typed fallback
> result. Do not implement OpenAI, Gemini, or Ollama yet. Do not let provider
> adapters import bot runtime internals. Add unit tests for success, invalid
> JSON/schema, timeout, retry, fallback, and secret redaction.

**Acceptance criteria**

- One provider-neutral interface is used by both future agents.
- Fake responses are deterministic from test input.
- Invalid output never reaches an agent executor.
- Timeout and provider failures return typed errors.
- Retry count is bounded by configuration.
- Raw responses have a configured size limit and are redacted.
- No network or API key is required by tests.

**Validation**

```bash
python3 -B tests/model_gateway_test.py
python3 -B tests/model_planner_test.py
./scripts/run-tests.sh --fast
```

### AI-003 - Add A Persistent Hard-Budget And Usage Ledger

**Priority:** P0 / `agents`, `cost-control`, `persistence`

**Dependencies:** AI-002

**Description**

Prevent accidental cost growth independently of provider-side dashboards.
Budget checks must be durable across controller restarts.

**Agent prompt**

> Add a SQLite-backed model usage and budget ledger to bot-controller. Persist
> reservations and final usage by agent ID, agent type, provider, model,
> deployment/run, match, round, and UTC day. Before a provider call, atomically
> reserve the configured maximum call cost and reject the call if any per-call,
> per-run, per-match, daily, token, or call-count limit would be exceeded. After
> the call, reconcile the reservation with actual usage when available. Release
> or conservatively account for failed calls according to a documented policy.
> Recover stale reservations after a bounded timeout. Expose redacted read-only
> usage summaries through the controller API. Add tests for concurrent
> reservations, restart persistence, exact-boundary behavior, exhausted budget,
> stale recovery, and missing provider cost metadata.

**Acceptance criteria**

- Budget checks are atomic and persistent.
- Concurrent calls cannot overspend the configured reservation budget.
- An exhausted budget prevents the provider call from starting.
- Controller restart does not reset usage.
- Missing cost metadata uses a documented conservative estimate.
- Usage APIs reveal no API key, planning token, prompt secret, or raw flag.
- Budget rejection can trigger the fake/scripted fallback.

**Validation**

```bash
python3 -B tests/model_budget_test.py
python3 -B tests/bot_api_test.py
./scripts/run-tests.sh --fast
```

### AI-004 - Implement The OpenAI Structured-Output Provider

**Priority:** P0 / `agents`, `openai`, `integration`

**Dependencies:** AI-003

**Description**

Add the first real provider while preserving the provider-neutral gateway. Use
structured output and keep the key only in bot-controller.

**Agent prompt**

> Implement an OpenAI provider adapter behind the model gateway. Read
> OPENAI_API_KEY only from the bot-controller environment. Use the current
> official OpenAI API suitable for structured JSON output, but keep the SDK or
> HTTP-specific code isolated in the provider module. Send only the validated
> system instructions, compact observation, and registered tool schemas. Parse
> the result into the internal response contract and capture provider request
> ID, model ID, input tokens, output tokens, and reported or estimated cost.
> Enforce timeout, maximum output tokens, response-size limits, and the budget
> reservation from AI-003. Never log the key or raw sensitive prompts. Add
> mocked HTTP/provider contract tests for success, authentication failure, rate
> limit, timeout, malformed output, and usage extraction. Add an opt-in smoke
> script that requires an explicit environment flag and defaults to a maximum
> cost of a few cents; do not run it in CI.

**Acceptance criteria**

- OpenAI is selected only through the gateway configuration.
- The key exists only in bot-controller environment state.
- Team containers and generated Compose inspection do not expose the key.
- Structured output is validated through the internal contract.
- Usage is reconciled with the persistent budget ledger.
- Required tests mock all network behavior.
- The real smoke test is opt-in and refuses to run without an explicit cost
  limit.

**Validation**

```bash
python3 -B tests/openai_provider_test.py
python3 -B tests/model_budget_test.py
./scripts/run-tests.sh --fast

# Optional and never part of CI:
SANDCASTLE_ALLOW_REAL_MODEL=1 \
ARENA_AGENT_MAX_COST_USD_PER_CALL=0.02 \
./scripts/smoke-openai-agent.sh
```

### AI-005 - Implement Authenticated Planning And Tool-Selection Endpoints

**Priority:** P0 / `agents`, `api`, `security`

**Dependencies:** AI-004

**Description**

Connect the existing remote planner adapter to a real controller endpoint
without exposing provider credentials.

**Agent prompt**

> Implement authenticated bot-controller endpoints for model planning using the
> model gateway. Support the existing RemoteModelPlannerAdapter wire contract
> while migrating it to the versioned contracts from AI-001. Validate a
> deployment-scoped planning token, agent identity, team identity, allowed
> targets, registered tool schemas, observation size, and budget scope. Reject
> unknown tools, self-targeted offensive actions, mismatched team identity, and
> expired or wrong tokens before any provider call. Generate or derive scoped
> planning credentials during deployment without placing provider keys in team
> containers. Add request correlation IDs and return only validated tool calls
> plus safe usage metadata. Add API tests for authentication, authorization,
> malformed bodies, self-targeting, replay/expiry policy, budget exhaustion,
> and successful fake-provider planning.

**Acceptance criteria**

- The existing model planner can receive a valid plan from bot-controller.
- Provider keys remain absent from team runtime configuration.
- Planning tokens are scoped and not returned by public deployment APIs.
- Invalid requests cannot consume provider budget.
- Model output cannot expand the submitted tool allowlist.
- Offensive self-targeting is rejected twice: at API validation and executor
  validation.
- Fake-provider integration works without Docker or network access.

**Validation**

```bash
python3 -B tests/agent_plan_api_test.py
python3 -B tests/model_planner_test.py
python3 -B tests/bot_api_test.py
./scripts/run-tests.sh --fast
```

### AI-006 - Add Stable Agent Runs, Identity, And Concurrent Product Roles

**Priority:** P0 / `agents`, `deployment`, `persistence`

**Dependencies:** AI-005

**Description**

Represent the two agents as first-class product entities rather than generic
bot names.

**Agent prompt**

> Extend bot-controller persistence and deployment APIs with stable agent type,
> agent ID, run ID, provider, model, and lifecycle state. Support one active
> AttackDefenseAgent per selected team and one organizer-scoped
> ChallengeGeneratorAgent run. Preserve compatibility for existing scripted bot
> deployments. Replace assumptions that active deployment uniqueness is only by
> team with an explicit uniqueness policy by agent type and scope. Ensure
> stopping or superseding one agent does not stop an unrelated agent. Include
> database migration logic for existing controller databases and expose safe
> agent identity in API payloads. Add tests for migration, uniqueness,
> concurrent roles, restart recovery, stop, and supersede behavior.

**Acceptance criteria**

- `attack_defense` and `challenge_generator` are persisted and queryable.
- Existing deployment records migrate without data loss.
- Agent IDs and run IDs remain stable across controller restart.
- Agent lifecycle operations are scoped to the selected agent run.
- Existing scripted bot deployment remains functional.
- Public payloads contain identity and model metadata but no credentials.

**Validation**

```bash
python3 -B tests/agent_runs_test.py
python3 -B tests/bot_api_test.py
./scripts/run-tests.sh --fast
```

### AI-007 - Persist Agent Memory, Real Tool Results, And Evaluation Telemetry

**Priority:** P0 / `agents`, `telemetry`, `evaluation`

**Dependencies:** AI-006

**Description**

Give both agents bounded memory and ensure future plans observe actual outcomes,
not merely previously proposed actions.

**Agent prompt**

> Implement bounded structured memory for agent runs. Persist concise
> observations, selected tool calls, validated arguments, tool status, safe
> result summaries, checker/exploit outcomes, patch identifiers, model usage,
> and errors. Update ModelBackedPlanner so the next planning request receives
> actual completed action results rather than only accepted task names. Add
> configurable retention by entry count and payload size. Forward normalized
> `agent.*` events to gameserver telemetry with match, round, team, agent ID,
> run ID, and correlation ID when applicable. Extend telemetry metrics with
> model calls, cost, successful actions, patch attempts, rollback count,
> challenge attempts, and validation outcomes. Redact flags, credentials, full
> source files, and raw model reasoning. Add export and metrics tests.

**Acceptance criteria**

- The next plan can observe real success/failure from the previous action.
- Memory is bounded and survives controller restart.
- Telemetry distinguishes the two agents.
- Match export contains normalized agent evidence without scraping logs.
- Flags, tokens, keys, and raw source are redacted.
- Metrics derive from persisted events and tool outcomes.

**Validation**

```bash
python3 -B tests/agent_memory_test.py
python3 -B tests/model_planner_test.py
python3 -B tests/telemetry_test.py
./scripts/run-tests.sh --fast
```

### AI-008 - Define A Deterministic ChallengeSpec And Template Renderer

**Priority:** P0 / `challenge-generation`, `services`, `reproducibility`

**Dependencies:** AI-007

**Description**

Build the deterministic non-AI foundation for generated challenges. Limit the
first version to one Flask template and the three vulnerabilities already
understood by the repository.

**Agent prompt**

> Implement a versioned ChallengeSpec and deterministic renderer for a Flask
> notes-style HTTP service. Support exactly these initial vulnerability kinds:
> path traversal, command injection, and SQL injection. Allow bounded seeded
> variation of route names, parameter names, database/entity names, visible
> labels, and decoy endpoints. Render a complete service plugin containing
> application source, requirements, Dockerfile, service metadata, checker,
> reference exploit, reference patch, reset/seed behavior, README, and a
> generation manifest. Derive all variation from an explicit seed; do not use
> wall-clock time, random global state, provider output order, or host paths.
> Render into a staging directory outside `services/` and `teams/generated/`.
> Reject unsafe identifiers, paths, ports, dependencies, and unsupported
> vulnerability combinations. Add golden tests proving byte-identical output
> for the same spec and meaningful differences for different seeds.

**Acceptance criteria**

- One valid spec renders a complete service-plugin candidate.
- All three supported vulnerability types have checker, exploit, and patch
  artifacts.
- Same spec, template version, and seed produce byte-identical files.
- Unsupported or unsafe values fail before files are rendered.
- Generated code does not contain provider credentials or host-specific paths.
- The canonical `services/example-vuln` template is not modified by rendering.

**Validation**

```bash
python3 -B tests/challenge_spec_test.py
python3 -B tests/challenge_renderer_test.py
git diff --check
```

### AI-009 - Implement The Isolated Challenge Validation Pipeline

**Priority:** P0 / `challenge-generation`, `docker`, `testing`

**Dependencies:** AI-008

**Description**

Prove generated candidates work before an agent is allowed to publish them.
Validation must test both vulnerable and patched states.

**Agent prompt**

> Implement a bounded challenge validator for staged candidates. It must inspect
> the candidate manifest and Compose/build inputs, reject privileged mode, host
> networking, host paths, Docker socket mounts, extra capabilities, unapproved
> images, and ports outside the configured range. Build and start the candidate
> in an isolated temporary project with unique names, no provider secrets,
> resource limits, log limits, and cleanup in success and failure paths. Run
> CHECK, PUT, GET, the reference exploit, apply the reference patch, rebuild,
> then rerun CHECK, PUT, GET, and the exploit. Produce a structured validation
> report with bounded logs. The vulnerable exploit must succeed and the patched
> exploit must fail while legitimate behavior remains valid. Add a no-Docker
> fixture mode for CI plus an opt-in real Docker integration test.

**Acceptance criteria**

- Unsafe candidate Compose/build settings are rejected before startup.
- Vulnerable-state CHECK/PUT/GET and exploit success are required.
- Patched-state CHECK/PUT/GET success and exploit failure are required.
- Every container, network, image tag, and temporary directory is cleaned up or
  reported with remediation.
- Validation has global and per-step timeouts.
- Fixture tests are deterministic and required by `run-tests.sh`.
- Real Docker validation is documented and opt-in where the environment cannot
  support it.

**Validation**

```bash
python3 -B tests/challenge_validator_test.py
./tests/challenge_validation_test.sh --local
./scripts/run-tests.sh --fast

# Native Linux Docker validation:
./tests/challenge_validation_test.sh
```

### AI-010 - Implement Challenge Publication And Arena Selection

**Priority:** P0 / `challenge-generation`, `arena`, `lifecycle`

**Dependencies:** AI-009

**Description**

Create an explicit promotion boundary between staged candidates and challenges
that arena setup may distribute to teams.

**Agent prompt**

> Implement a challenge artifact registry and atomic publication workflow.
> Publication must accept only a successful validation report whose artifact
> digest matches the staged candidate. Store immutable published artifacts under
> a dedicated generated-challenge registry path with manifest, source digest,
> spec, seed, template version, validation report, creation time, and producing
> agent run ID. Add list, inspect, publish, and delete-unreferenced operations;
> do not allow mutation of a published version. Extend arena configuration and
> setup so ARENA_SERVICE_TEMPLATE can select a published challenge by immutable
> ID while preserving support for `services/example-vuln`. Setup must copy the
> selected artifact reproducibly to every team and must never use an
> unvalidated staging directory. Add tests for digest mismatch, duplicate
> publication, atomic failure, selection, reproducible team copies, and
> referenced-artifact deletion protection.

**Acceptance criteria**

- Only validated artifacts can be published.
- Published artifacts are immutable and content-addressed or equivalently
  digest-verified.
- Arena setup can select a published challenge explicitly.
- Every team receives the same published service version.
- Re-running setup produces no tracked diff and does not mutate the artifact.
- Existing static service selection remains supported.

**Validation**

```bash
python3 -B tests/challenge_registry_test.py
./tests/setup_test.sh
./scripts/setup.sh --teams 2
docker compose config --quiet
git diff --check
```

### AI-011 - Implement ChallengeGeneratorAgent's Iterative Tool Loop

**Priority:** P0 / `agents`, `challenge-generation`, `ai`

**Dependencies:** AI-010

**Description**

Build the second required AI agent on top of deterministic generation,
validation, publication, memory, and the model gateway.

**Agent prompt**

> Implement ChallengeGeneratorAgent as an organizer-scoped iterative agent run.
> Give it only typed tools for creating/revising ChallengeSpec, rendering,
> validating, inspecting bounded validation summaries, publishing a validated
> artifact, and discarding a failed candidate. The model must never write files
> directly or invoke Docker directly. Start from an organizer request containing
> difficulty, vulnerability allowlist, seed, and maximum attempts. On each
> iteration, build a compact observation from the current spec and validator
> errors, ask the model for exactly one next tool call, validate it, execute it,
> persist the outcome, and continue until published, failed, cancelled, budget
> exhausted, or maximum attempts reached. Provide a deterministic fake-provider
> script that demonstrates one failed candidate, one revision, successful
> validation, and publication. Expose start, status, cancel, and artifact result
> APIs. Add tests for invalid model actions, repeated invalid specs, cancellation,
> budget exhaustion, attempt exhaustion, successful publication, and restart
> recovery.

**Acceptance criteria**

- ChallengeGeneratorAgent has its own identity, run, memory, and lifecycle.
- It demonstrates a multi-step observe/decide/tool/observe loop.
- It cannot bypass renderer, validator, or publication gates.
- Attempt and cost limits stop the loop deterministically.
- Controller restart can resume or clearly fail an interrupted run.
- Fake-provider end-to-end generation publishes a valid artifact.
- Real-model use is optional and budget-bounded.

**Validation**

```bash
python3 -B tests/challenge_generator_agent_test.py
python3 -B tests/agent_memory_test.py
./tests/challenge_generation_e2e_test.sh --local
./scripts/run-tests.sh --fast
```

### AI-012 - Expand Team-Local Defensive Source And Service Tools

**Priority:** P0 / `agents`, `defense`, `security`

**Dependencies:** AI-011

**Description**

Add the bounded team-local tools needed by AttackDefenseAgent's defensive mode.
Do not expose arbitrary shell access.

**Agent prompt**

> Extend `bot/service_control.py` into an authenticated team-local defensive
> control API while preserving existing health and restart behavior. Add typed
> endpoints for listing allowed source files, reading bounded text ranges,
> searching source with literal or safely compiled patterns, creating a source
> snapshot, validating and applying a unified diff, showing a bounded diff,
> rebuilding the own-team app, running the own service checker, running only
> registered reference exploits against the own service, restoring a snapshot,
> and committing/discarding a patch transaction. Normalize paths and constrain
> all file access to the generated service root and an extension allowlist.
> Authenticate requests with a per-team, deployment-scoped credential in
> addition to source-IP checks. Enforce request size, changed-file count, patch
> size, timeout, output size, and one active transaction limits. Never accept a
> raw command. Ensure Docker operations are fixed to the own-team app container
> or own-team DinD daemon. Add tests for traversal, symlinks, wrong team/token,
> oversized patch, forbidden file, arbitrary command attempts, transaction
> conflicts, and own-team success.

**Acceptance criteria**

- Defensive tools work only for the requesting team's service.
- Source-IP checks are not the only authentication mechanism.
- Path traversal and symlink escapes are rejected.
- No endpoint accepts arbitrary shell commands.
- Patch operations require a snapshot transaction.
- Docker operations cannot name another team or control-plane container.
- Trusted, isolated, and DinD modes have documented behavior.

**Validation**

```bash
python3 -B tests/service_control_test.py
python3 -B tests/defensive_tools_test.py
./tests/isolation_test.sh
./tests/dind_isolation_test.sh
./scripts/run-tests.sh --fast
```

### AI-013 - Implement Transactional Defensive Patch Validation

**Priority:** P0 / `agents`, `defense`, `reliability`

**Dependencies:** AI-012

**Description**

Turn low-level defensive tools into one safe patch workflow with automatic
rollback.

**Agent prompt**

> Add a typed defensive patch action that orchestrates the API from AI-012 as a
> transaction: snapshot, apply validated diff, rebuild, wait for health, run
> CHECK/PUT/GET, run the selected reference exploit, then commit only when
> legitimate behavior passes and the exploit no longer captures a flag. On any
> failure or timeout, restore the snapshot, rebuild the original service, prove
> health and checker recovery, and emit a rollback result. Persist patch ID,
> changed-file metadata, validation outcomes, durations, and safe error
> summaries. Do not persist full source or raw flags in telemetry. Make the
> action idempotent for retried correlation IDs and prevent concurrent patch
> transactions. Add tests for successful patch, syntax/build failure, health
> failure, SLA regression, exploit still succeeding, rollback failure, duplicate
> request, and controller/runtime interruption.

**Acceptance criteria**

- A patch cannot commit without checker success and exploit failure.
- Every failed attempt triggers and verifies rollback.
- The original service is restored after a failed patch where recovery is
  possible.
- Duplicate action delivery cannot apply the same patch twice.
- Patch telemetry is complete, correlated, and redacted.
- A test proves path traversal can be patched without breaking legitimate
  service behavior.

**Validation**

```bash
python3 -B tests/defensive_patch_test.py
python3 -B tests/actions_test.py
python3 -B tests/telemetry_test.py
./scripts/run-tests.sh --fast
```

### AI-014 - Implement AttackDefenseAgent's Autonomous Match Loop

**Priority:** P0 / `agents`, `attack`, `defense`

**Dependencies:** AI-013

**Description**

Build the first required AI agent by combining existing offensive actions with
the new defensive workflow and real match observations.

**Agent prompt**

> Implement AttackDefenseAgent as a first-class team deployment using the
> existing bot runtime, model gateway, memory, and typed action registry. Give
> it an explicit allowlist containing offensive recon/exploit/submission actions,
> own-service inspection actions, and the transactional defensive patch action.
> At each decision step, observe current match/round state, allowed opponents,
> recent checker status for its own service, recent attack results, prior patch
> outcomes, remaining action budget, and remaining model budget. Ask the model
> for one or a small bounded sequence of typed actions, validate them, execute
> them, persist actual outcomes, and repeat according to the configured round
> policy. Never let offensive actions target self or defensive actions target
> another team. Add a deterministic fallback policy that prioritizes service
> recovery, then known unpatched vulnerabilities, then recon. Add fake-provider
> tests proving the agent attacks an opponent, submits a flag, detects its own
> vulnerability, applies a valid patch, preserves SLA, and changes future
> decisions based on previous results.

**Acceptance criteria**

- AttackDefenseAgent has one stable identity and both ATTACK and DEFEND
  capabilities.
- It uses a model decision loop, not a fixed one-shot completion.
- Actual action outcomes influence later plans.
- Captured flags are submitted through the existing gameserver authority.
- Defensive patching uses only the transaction from AI-013.
- Self-attack and cross-team defense are rejected.
- Provider failure or budget exhaustion activates deterministic fallback.
- Fake-provider tests cover a complete attack and defense sequence.

**Validation**

```bash
python3 -B tests/attack_defense_agent_test.py
python3 -B tests/actions_test.py
python3 -B tests/model_planner_test.py
./tests/attack_defense_e2e_test.sh --local
./scripts/run-tests.sh --fast
```

### AI-015 - Add Operator UI For Both AI Agents And Cost Visibility

**Priority:** P1 / `agents`, `frontend`, `operations`

**Dependencies:** AI-014

**Description**

Make both agents visible and controllable as product functionality. The UI
should show evidence of autonomous iteration without exposing secrets or raw
reasoning.

**Agent prompt**

> Extend the visualizer Bot panel or add a focused Agents view. Present
> AttackDefenseAgent and ChallengeGeneratorAgent as distinct agent types with
> separate creation forms, lifecycle states, provider/model selection from
> server-approved options, budget limits, and stop/cancel controls. For
> AttackDefenseAgent show team, current mode, round, current tool, recent attack
> and defense outcomes, captures, submissions, patch attempts, rollbacks, model
> calls, token usage, and cost. For ChallengeGeneratorAgent show request, seed,
> current attempt, current ChallengeSpec summary, validation stages, revisions,
> publication result, model usage, and cost. Display budget exhaustion and
> fallback state clearly. Render safe structured decisions and tool results, not
> chain-of-thought, provider keys, planning tokens, raw flags, or full source.
> Preserve the existing scripted bot workflow. Add frontend types and tests or
> build-time fixtures for API states, empty states, errors, cancellation, and
> completed runs.

**Acceptance criteria**

- The UI clearly displays two distinct AI agent types.
- Operators can start/stop AttackDefenseAgent and start/cancel
  ChallengeGeneratorAgent.
- Current action, result, model usage, and budget state are visible.
- Published challenge ID can be selected or copied into arena configuration
  through an explicit operator workflow.
- Existing bot deployment UI remains usable.
- No secret or raw flag is rendered.
- `npm run build` succeeds.

**Validation**

```bash
cd visualizer
npm ci
npm run build
```

### AI-016 - Add Deterministic Two-Agent End-To-End Coverage

**Priority:** P0 / `agents`, `integration`, `ci`

**Dependencies:** AI-015

**Description**

Prove the complete product workflow without external model availability.

**Agent prompt**

> Add a deterministic fixture-mode end-to-end test using the fake provider. The
> test must start a ChallengeGeneratorAgent run, observe at least one failed
> validation and revision, publish a challenge, select that immutable challenge
> for a two-team arena, start a match, deploy AttackDefenseAgent to team 2,
> observe an accepted flag captured from team 1, observe a successful defensive
> patch on team 2, prove team 2 CHECK/PUT/GET remain valid, prove the matching
> exploit no longer captures team 2's flag, export telemetry, and verify that
> events exist for two distinct agent identities. Add bounded timeouts and useful
> failure logs. Register fixture mode in scripts/run-tests.sh and CI. Add a real
> Docker variant for native Linux that uses the fake provider and cleans all
> resources.

**Acceptance criteria**

- Required CI proves two distinct product agents.
- Challenge generation includes an iterative revision.
- The published challenge is the one distributed to both teams.
- AttackDefenseAgent earns an accepted attack result.
- AttackDefenseAgent commits a patch without losing SLA.
- Telemetry export distinguishes both agents and their tool histories.
- The test requires no external API key or internet.
- Real Docker mode is bounded and leaves no arena resources.

**Validation**

```bash
./tests/two_agent_e2e_test.sh --local
./scripts/run-tests.sh

# Native Linux Docker validation:
./tests/two_agent_e2e_test.sh
```

### AI-017 - Add Optional Local Ollama Provider And Small-Model Profile

**Priority:** P1 / `agents`, `local-model`, `offline`

**Dependencies:** AI-016

**Description**

Provide a no-API-cost demonstration path using a small local model. This task is
after the complete fake/OpenAI workflow so local-model quirks cannot redefine
the internal contracts.

**Agent prompt**

> Implement an optional Ollama-compatible provider adapter behind the existing
> model gateway. Do not start or download a model automatically. Add
> configuration for endpoint and model name, startup/readiness diagnostics, JSON
> structured-output parsing, timeout, context-size limits, usage accounting when
> available, and conservative accounting when unavailable. Add a documented
> small-model profile that uses shorter observations, one tool call per plan,
> strict schema repair at most once, and deterministic fallback. Keep the model
> name configurable rather than hardcoding a vendor version. Add mocked adapter
> tests and an opt-in local smoke script that first verifies the endpoint and
> prints the exact configured budget/action bounds.

**Acceptance criteria**

- Ollama is optional and does not affect default startup.
- No model is downloaded implicitly.
- Local-model responses pass the same validation as OpenAI responses.
- Hallucinated tools and malformed arguments are rejected safely.
- Missing usage metadata cannot bypass call/action limits.
- The documented local profile can run at least one agent planning step.

**Validation**

```bash
python3 -B tests/ollama_provider_test.py
./scripts/run-tests.sh --fast

# Optional:
SANDCASTLE_ALLOW_REAL_MODEL=1 ./scripts/smoke-ollama-agent.sh
```

### AI-018 - Finalize Documentation, Demo Script, And Requirement Evidence

**Priority:** P0 / `agents`, `documentation`, `education`

**Dependencies:** AI-017

**Description**

Make the implementation reproducible for evaluators and explicitly prove that
the requirement refers to two AI agents inside the product.

**Agent prompt**

> Update README.md, docs/architecture.md, docs/THREAT_MODEL.md, and bot
> documentation for the two product agents. Add a concise organizer guide that
> covers `.env` secret setup, provider selection, hard cost limits, fake mode,
> optional OpenAI smoke mode, optional Ollama mode, challenge generation,
> publication, arena selection, AttackDefenseAgent deployment, telemetry export,
> cleanup, and troubleshooting. Add a bounded demo script that checks
> prerequisites and walks through the deterministic two-agent scenario without
> external keys by default. Add a separate explicit flag for a real-model demo;
> print the configured maximum cost before requesting confirmation or requiring
> the non-interactive allow variable. Document that ChallengeGeneratorAgent runs
> before matches and AttackDefenseAgent plays during matches. Include a
> requirement-evidence table mapping each agent to its objective, observations,
> model, tools, iterative loop, persisted evidence, and visible UI behavior.
> Ensure all documented commands are current and testable.

**Acceptance criteria**

- Documentation names exactly two required AI product agents.
- Evaluators can identify each agent's objective, model call, tools, memory, and
  iterative behavior.
- Default demo mode uses the fake provider and incurs no API cost.
- Real-model mode shows and enforces a maximum cost.
- Security documentation covers provider keys, generated code, model output,
  build isolation, and defensive patch access.
- Cleanup and reset semantics are documented.
- The full local test suite passes.

**Validation**

```bash
./scripts/demo-two-agents.sh --check
./scripts/demo-two-agents.sh --fake
./scripts/run-tests.sh
git diff --check
```

## Required Execution Order

Implement and merge the tasks in exactly this order:

1. AI-001 - contracts and configuration
2. AI-002 - provider-neutral gateway and fake provider
3. AI-003 - persistent hard budgets
4. AI-004 - OpenAI provider
5. AI-005 - authenticated planning API
6. AI-006 - first-class agent runs and identity
7. AI-007 - memory, result feedback, and telemetry
8. AI-008 - deterministic challenge renderer
9. AI-009 - isolated challenge validator
10. AI-010 - publication and arena selection
11. AI-011 - ChallengeGeneratorAgent
12. AI-012 - team-local defensive tools
13. AI-013 - transactional patch validation
14. AI-014 - AttackDefenseAgent
15. AI-015 - operator UI
16. AI-016 - deterministic two-agent end-to-end test
17. AI-017 - optional local-model provider
18. AI-018 - final documentation and demonstration

Do not begin a task until all its dependencies have passed their listed
validation commands. A task may be split into multiple commits, but each task
should remain one reviewable PR where practical.

## Proof-Of-Concept Exit Checklist

- [ ] `ChallengeGeneratorAgent` is a first-class persisted AI agent.
- [ ] `ChallengeGeneratorAgent` completes more than one decision/tool iteration.
- [ ] It produces a deterministic `ChallengeSpec`.
- [ ] It cannot publish a challenge that fails vulnerable or patched validation.
- [ ] A published challenge can be selected for arena generation.
- [ ] `AttackDefenseAgent` is a first-class persisted AI agent.
- [ ] It attacks an opponent and submits at least one accepted flag.
- [ ] It patches its own service through bounded team-local tools.
- [ ] Its committed patch preserves CHECK/PUT/GET and blocks the exploit.
- [ ] Failed patches roll back automatically.
- [ ] Both agents use the same provider-neutral model gateway.
- [ ] Fake provider mode passes in CI without network access.
- [ ] At least one opt-in real-model smoke run is documented and budget-bounded.
- [ ] Provider keys exist only in the organizer controller.
- [ ] Hard per-call, per-run, per-match, and daily limits are enforced locally.
- [ ] Telemetry export proves two distinct agent identities and iterative loops.
- [ ] The UI displays both agents, their actions, outcomes, and model usage.
- [ ] The default demonstration incurs no provider cost.
- [ ] `./scripts/run-tests.sh` passes.

## Deferred Work

The following work is deliberately outside this proof of concept:

- unrestricted generation of arbitrary applications or languages;
- model-generated Docker or Compose files without deterministic templates;
- arbitrary shell tools;
- autonomous installation of packages or local models;
- generation during an active scored round;
- public multi-tenant hosting;
- adversarially safe execution of arbitrary generated code;
- challenge novelty or difficulty guarantees beyond the supported templates;
- automatic vulnerability discovery against unknown services;
- fine-tuning or training models;
- direct agent-to-agent natural-language conversation.

Add these only after the two-agent proof of concept is reproducible, observable,
budget-bounded, and covered by the required end-to-end test.
