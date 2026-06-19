# Sandcastle Vision

## One-Sentence Product

Sandcastle is a local, reproducible Attack & Defense arena where autonomous
software agents defend their own vulnerable services, attack opponents, submit
captured flags, and are scored over timed rounds.

## Why It Exists

The project is an experimentation platform for agentic software engineering in
an adversarial environment. It should make it cheap to answer questions such as:

- Can an agent inspect and patch an unfamiliar vulnerable service?
- Can an agent preserve service functionality while closing exploit paths?
- Can an agent discover, adapt, and execute attacks against changing opponents?
- How do different planners, models, tools, and memory strategies compare?
- Can a complete A&D match be reproduced and evaluated automatically?

## Current State

The repository currently provides an infrastructure prototype, not a complete
arena.

Implemented:

- generated per-team SSH gateways and vulnerable Linux machines
- deterministic team addresses on a Docker bridge network
- mutable per-team copies of an example vulnerable Flask service
- a scripted, pluggable bot runtime with recon and exploit actions
- a React topology visualizer and local bot-control bridge
- a firewall/activity-feed prototype
- setup, start, stop, reset, and cleanup scripts

Not implemented:

- gameserver and authoritative competition state
- round/tick engine
- reliable flag generation, planting, expiry, and submission
- checker framework and SLA results
- scoring and scoreboard
- automatic full-arena startup and health verification
- robust traffic enforcement and event capture
- safe isolation for untrusted participants or agents
- production-grade agent execution, credentials, telemetry, and evaluation

The current prioritized audit is in
[`docs/PROJECT_AUDIT_AND_BACKLOG.md`](docs/PROJECT_AUDIT_AND_BACKLOG.md).

## Target User Experience

An organizer should be able to run:

```bash
./scripts/arena.sh up --teams 4
./scripts/arena.sh status
```

and receive a healthy arena with:

- four equivalent team environments
- one or more vulnerable services running for every team
- a gameserver running timed rounds
- flags planted and checked each round
- authenticated flag submission
- live scores and service status
- optional attacker/defender agents assigned to teams
- an event stream and an auditable match history

A complete smoke match should run without manual SSH commands.

## Core Match Loop

For every round:

1. The gameserver creates a unique flag for each team and service.
2. A service-specific checker plants the flag through a legitimate service
   workflow.
3. The checker validates availability, functionality, and flag integrity.
4. Team agents inspect, patch, restart, monitor, and attack.
5. Captured flags are submitted to the gameserver with team authentication.
6. The gameserver rejects invalid, duplicate, expired, and self-owned flags.
7. Scores and service states are calculated and published.
8. All important actions are persisted for replay and evaluation.

## Target Architecture

### Arena Control Plane

The control plane owns configuration, lifecycle, and authoritative state:

- arena configuration and team registry
- round scheduler
- checker execution
- flag lifecycle
- submission API
- scoring
- operator controls
- persisted events and match results

### Team Execution Plane

Each team owns:

- a participant/agent workspace
- one or more vulnerable services
- team-scoped credentials
- attacker and defender processes with explicit capabilities

The execution plane must not rely on accidental access to the host Docker
daemon. Local trusted mode may use the Docker socket temporarily, but the
capability must be explicit and replaceable.

### Service Plugin Contract

Every challenge service should provide:

- image/build definition
- service metadata and ports
- `put` operation for planting a flag
- `get` operation for validating the planted flag
- benign SLA checks
- reference exploit or exploit test
- reset/seed behavior
- documented patch boundaries

HTTP must not be assumed; TCP binary services are a first-class requirement.

### Agent Contract

Agents should receive:

- team identity and allowed targets
- service source and service metadata
- scoped tools and credentials
- current round information
- structured observations and previous results

Agents should produce:

- proposed or applied code changes
- service lifecycle actions
- attack attempts
- captured flags
- structured logs, costs, timing, and outcome metadata

Planners decide what to do. Typed actions perform the work. The gameserver, not
the agent, remains authoritative for flags and scores.

## Engineering Invariants

- A clean checkout has one documented path to a working arena.
- Team count and arena configuration have one source of truth.
- Generated files are reproducible and never edited as canonical source.
- Startup is idempotent and removes or reports stale runtime resources.
- A reported healthy arena has every required service actually running.
- Checkers exercise legitimate behavior, not only `/health`.
- Flags are unique, authenticated, time-bounded, and never trusted from clients.
- Bots cannot target their own team and cannot award themselves points.
- Security boundaries are documented honestly and verified by tests.
- Every P0 workflow has an automated end-to-end test.
- The platform fails loudly when required host networking features are absent.
- Match results are reproducible from configuration, seed, source revision, and
  persisted events.

## Initial Scope

The first complete milestone is a trusted, Linux-only, single-host arena:

- 2-8 teams
- one bundled vulnerable HTTP service
- SQLite persistence
- one gameserver process
- simple configurable rounds
- authenticated HTTP flag submission
- UP/DOWN/MUMBLE/CORRUPT checker states
- a basic live scoreboard
- scripted bots with an external planner hook

This milestone is explicitly not safe for hostile human participants.

## Later Scope

After the trusted MVP is reliable:

- isolated per-team Docker daemons or microVMs
- multiple services per team
- TCP binary challenges
- model-backed planners and tool policies
- procedural challenge generation
- match replay and benchmark datasets
- remote workers and multi-host execution

## Non-Goals For The MVP

- Internet-scale hosting
- strong multi-tenant security
- arbitrary unreviewed challenge images
- automatic vulnerability generation
- sophisticated tournament formats
- hiding all infrastructure details from agents

## Definition Of A Complete MVP

The MVP is complete when a clean Linux host can run an automated two-team match
for at least ten rounds and prove all of the following:

- all team services start without manual intervention
- flags rotate and expire correctly
- valid exploits can capture and submit flags
- duplicate, invalid, expired, and self-owned submissions are rejected
- patching a vulnerability stops the matching exploit
- breaking legitimate behavior produces an SLA penalty
- scores are deterministic from persisted events
- the scoreboard reflects current and historical state
- stop, restart, and reset preserve or clear state as documented
- CI runs a reduced end-to-end match

## Guidance For Coding Agents

Before changing code:

1. Read this file, `README.md`, and the relevant task in
   `docs/PROJECT_AUDIT_AND_BACKLOG.md`.
2. Confirm whether the task changes control-plane, team-plane, service-plugin,
   or agent behavior.
3. Preserve generated-source boundaries: edit `scripts/setup.sh`, not generated
   `docker-compose.yml`, unless the task explicitly changes that convention.
4. Add focused tests and include the exact verification commands.
5. Do not claim isolation, firewall enforcement, or arena readiness without a
   runtime test that proves it.

