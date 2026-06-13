# Sandcastle Project Audit And Agent Backlog

Audit date: 2026-06-10

This document is both a current-state audit and the canonical implementation
backlog. Each task is written so its **Agent prompt** can be pasted into a coding
agent and its title, priority, labels, description, dependencies, and acceptance
criteria can be copied into Linear.

Read [`../VISION.md`](../VISION.md) before starting a task.

## Executive Audit

Sandcastle is currently a useful A&D infrastructure and bot prototype, but it is
not yet an arena. It can create team containers, run an intentionally vulnerable
service manually, and execute a scripted cross-team exploit. It cannot run an
authoritative competition because there is no gameserver, round engine, checker
framework, submission API, scoring system, or scoreboard.

The highest-value next work is lifecycle reliability and a minimal gameserver.
Adding smarter agents or more challenge types before those foundations will
produce demos that cannot be evaluated consistently.

## Verified Architecture

| Area | Current implementation | Assessment |
|---|---|---|
| Topology | Generated root Compose with `teamN-ssh`, `teamN-vuln`, and a host-network firewall | Prototype works, state can drift |
| Team workspace | `services/example-vuln` copied to ignored `teams/generated/teamN/example-vuln` | Good mutable-workspace model |
| Vulnerable app | Flask notes service with SQLi, command injection, path traversal, and `/internal/plant` | Good first challenge |
| App lifecycle | Team runs nested Compose from inside `teamN-vuln` | Manual and fragile |
| Bot | Python action/planner runtime deployed into `teamN-ssh` | Offensive smoke path works |
| Bot control | Local HTTP bridge plus React Bot panel | Useful prototype, unauthenticated local control |
| Firewall | Host `iptables` redirect, TCP proxy, packet sniffers, WebSocket feed | Not enforcing/observing traffic on the audited host |
| Visualizer | Static Compose parser plus firewall and bot modes | Builds successfully; not an authoritative runtime UI |
| Competition | Architecture notes only | Missing |
| Tests | Syntax checks, Compose validation, visualizer build, limited CI lint | No behavioral or end-to-end coverage |

## Runtime Findings

The following were observed on the audit host:

- The committed Compose file described four teams.
- Marked generated workspaces existed for teams 1-3.
- Empty, unmarked generated directories existed for teams 4-5.
- Five team gateway/vulnerable-machine pairs were still running.
- Existing vulnerable app containers were stopped.
- Cross-team service access failed until an app was started manually.
- A clean Team 3 app start, health check, and Team 2 path-traversal bot attack
  succeeded and captured a flag.
- Restarting an old Team 1 app failed because its `network_mode:
  container:team1-vuln` still referenced a removed parent container namespace.
- The Python watchdog failed because bots run in `teamN-ssh`, while Docker CLI,
  Docker socket, and service source exist only in `teamN-vuln`.
- The firewall NAT redirect rule remained at zero packets after successful
  cross-team HTTP traffic. The audited host did not expose
  `net.bridge.bridge-nf-call-iptables`, so the documented source masking and
  activity feed were not active.
- Firewall logs showed repeated `No buffer space available` failures in the
  ICMP conntrack listener.

These findings prove the individual pieces are promising, but startup status is
not equivalent to arena health.

## Strengths To Preserve

- Deterministic team addressing is easy for agents and checkers to understand.
- Canonical challenge templates are separated from mutable per-team copies.
- The sample service has explicit vulnerabilities and reference exploits.
- Bot actions and planners already have a small extension contract.
- The visualizer builds and provides a useful operator-facing starting point.
- Scripts are readable and the generated Compose convention is clear.

## Primary Friction Points

1. There is no single source of truth for team count or runtime state.
2. `start.sh` does not start the services agents are supposed to attack.
3. Empty generated directories are treated as valid workspaces.
4. Orphan containers survive topology changes.
5. Nested app containers are coupled to replaceable parent container IDs.
6. Firewall startup does not prove firewall effectiveness.
7. Bot defensive actions require capabilities the bot container does not have.
8. Flags can be planted by the sample app, but no authority owns their lifecycle.
9. Health checks do not validate legitimate service behavior.
10. CI does not exercise a match, service exploit, bot deployment, or firewall.
11. Secrets, credentials, ports, and team counts are hardcoded in several places.
12. The Docker socket model is unsafe for untrusted agents and participants.

## Priority Model

- **P0 / Urgent:** required for a reproducible arena MVP
- **P1 / High:** required for a credible agent experimentation platform
- **P2 / Medium:** expands challenge variety, operations, or usability
- **P3 / Low:** optimization or advanced research after the core loop works

## Milestones

### M0 - Deterministic Infrastructure

Complete SC-001 through SC-005. A clean two-team environment starts, verifies,
and stops automatically.

### M1 - Playable Arena

Complete SC-006 through SC-011. Flags, rounds, checks, submissions, scores, and
a basic scoreboard work without bots.

### M2 - Agent Arena

Complete SC-012 through SC-015. Bots submit flags, planners receive structured
context, and all actions are measurable.

### M3 - Safer And Broader Platform

Complete SC-016 onward. Isolation, resource controls, more services, and
procedural experiments become practical.

## P0 Tasks

### SC-001 - Add An Arena Doctor And Readiness Contract **Linear:** P0 / `infra`, `developer-experience`, `diagnostics`

**Dependencies:** none

**Status:** Implemented on 2026-06-10 in `scripts/doctor.sh`, with
fixture-driven coverage in `tests/doctor_test.sh`.

**Agent prompt**

> Implement a read-only `scripts/doctor.sh` that determines whether Sandcastle
> can run correctly on the current host. Validate Docker and Compose access,
> Linux/host-network assumptions, required ports, subnet conflicts, generated
> team workspace completeness, Compose/team-count consistency, orphan
> Sandcastle containers, Docker-socket access, app health, firewall rule
> existence and packet counters, and bot API prerequisites. Produce concise
> PASS/WARN/FAIL output and a non-zero exit code for blockers. Do not mutate
> runtime state. Update README troubleshooting and add shell-level tests for
> parsable checks where possible.

**Acceptance criteria**

- `./scripts/doctor.sh` is safe to run before and after startup.
- It detects an empty generated service directory.
- It detects configured/running team-count drift and orphan containers.
- It detects that the firewall redirect has seen no packets.
- It distinguishes trusted-local warnings from hard blockers.
- Each failure includes a concrete remediation command or documentation link.

**Validation**

```bash
bash -n scripts/doctor.sh
./scripts/doctor.sh
```

### SC-002 - Create One Arena Configuration Source And Deterministic Generation

**Linear:** P0 / `infra`, `configuration`, `generation`

**Dependencies:** SC-001

**Status:** Implemented on 2026-06-10 with canonical configuration in
`config/arena.env` and fixture-driven coverage in `tests/setup_test.sh`.

**Agent prompt**

> Replace scattered team-count, port, subnet, credential, and round defaults
> with one validated arena configuration source. Refactor `scripts/setup.sh`,
> `scripts/start.sh`, bot defaults, and generated Compose to consume it.
> Generation must verify required files rather than treating a directory as a
> valid workspace. Clearly separate marked generated directories from
> participant-owned data. Preserve team patches unless an explicit destructive
> flag is passed. Detect or remove orphan top-level team containers when the
> configured team count decreases. Keep generated files reproducible.

**Acceptance criteria**

- One config defines team count, network, service port, and host SSH base port.
- Empty or partial generated workspaces are repaired or rejected explicitly.
- Reducing team count cannot silently leave active orphan teams.
- Re-running generation with unchanged inputs produces no tracked diff.
- Destructive overwrite requires an explicit flag and warning.
- Bot and visualizer defaults no longer assume four teams independently.

**Validation**

```bash
./scripts/setup.sh --teams 2
docker compose config --quiet
./scripts/setup.sh --teams 4
git diff --check
```

### SC-003 - Make Full Arena Startup And App Lifecycle Idempotent

**Linear:** P0 / `infra`, `lifecycle`, `services`

**Dependencies:** SC-002

**Status:** Implemented on 2026-06-10 in `scripts/arena.sh`, with lifecycle
coverage in `tests/arena_test.sh`.

**Agent prompt**

> Make the standard startup command bring up the complete current arena,
> including every generated vulnerable app. Handle the nested
> `network_mode: container:teamN-vuln` dependency safely when parent containers
> are recreated. Add bounded health waits and a status command that reports
> infrastructure and app health separately. Define stop, restart, and reset
> semantics for source, containers, and data volumes. Avoid manual per-team SSH
> steps in the normal organizer path.

**Acceptance criteria**

- One command starts gateways, vulnerable machines, firewall, and all apps.
- Re-running start after parent container recreation succeeds.
- Startup fails if any required app is unhealthy after the timeout.
- Stop preserves documented persistent data; reset removes documented data.
- Status reports each team gateway, machine, app, and health result.
- No stale network-namespace reference remains after restart.

**Validation**

```bash
./scripts/arena.sh up --teams 2
./scripts/arena.sh status
./scripts/arena.sh restart
./scripts/arena.sh down
```

### SC-004 - Replace Or Prove The Firewall And Activity Path

**Linear:** P0 / `networking`, `firewall`, `observability`

**Dependencies:** SC-001, SC-003

**Status:** Implemented on 2026-06-12 with fail-closed Linux preflight,
bounded firewall capture, and behavioral verification in
`scripts/smoke-network.sh`.

**Agent prompt**

> Redesign or harden the team-to-team traffic path so source masking and event
> capture are guaranteed on supported hosts. The current host-PREROUTING design
> starts successfully while its redirect counter remains zero. Choose an
> explicit supported architecture: configure and verify Linux bridge netfilter,
> route team traffic through a dedicated gateway/proxy topology, or replace the
> mechanism with another testable design. Fail startup when enforcement is
> required but inactive. Bound packet-capture resource use and handle netlink
> buffer pressure. Document supported operating systems and limitations.

**Acceptance criteria**

- A cross-team TCP request increments a verified enforcement counter or passes
  through an equivalent testable proxy.
- The destination observes the intended masked source identity.
- The WebSocket emits a matching event with original source and destination.
- Unsupported hosts fail readiness instead of silently bypassing enforcement.
- ICMP/UDP monitoring cannot spin or die silently on buffer exhaustion.
- Automated tests cover at least TCP enforcement and event emission.

**Validation**

```bash
./scripts/arena.sh up --teams 2
./scripts/smoke-network.sh
```

### SC-005 - Add A Full Competition-Lifecycle Integration Test

**Linear:** P0 / `testing`, `integration`, `ci`

**Dependencies:** SC-003, SC-004

**Status:** Implemented on 2026-06-12 in `tests/integration_test.sh` with a
local fixture mode (mock Docker, CI-safe) and a full Docker mode (real two-team
lifecycle). Developer convenience runner added as `scripts/run-tests.sh`. CI
jobs added in `.github/workflows/ci.yml`. Contributing guide added as
`CONTRIBUTING.md`.

**Agent prompt**

> Implement an automated two-team smoke test for the current infrastructure.
> It must generate the arena, start all components, verify SSH reachability and
> app health, plant or locate a test flag, run a cross-team reference exploit,
> verify the firewall/event path, restart a parent container, prove the app
> recovers, and tear everything down. Make failures leave useful logs. Add a CI
> job where the runner supports Docker and keep a faster local mode.

**Acceptance criteria**

- The test is non-interactive and has a bounded runtime.
- It proves the stale network-namespace regression is fixed.
- It proves at least one bot or reference exploit captures a flag.
- It verifies cleanup leaves no test containers, networks, or volumes.
- CI artifacts include Compose, app, and firewall logs on failure.

### SC-006 - Implement The Gameserver Core And Persistent Match State

**Linear:** P0 / `gameserver`, `backend`, `database`

**Dependencies:** SC-002, SC-003

**Agent prompt**

> Add a minimal gameserver service using the repository's Python conventions.
> Use SQLite for the trusted local MVP. Model matches, teams, services, rounds,
> flags, checker results, submissions, and score events. Provide migrations or
> deterministic schema initialization. Implement explicit match states such as
> CREATED, RUNNING, PAUSED, FINISHED, and FAILED. Expose health and read-only
> match-state APIs. Integrate the gameserver into generated Compose and arena
> lifecycle scripts without giving it unnecessary host privileges.

**Acceptance criteria**

- Gameserver state survives container restart.
- Match state transitions are validated and idempotent.
- Team/service registry comes from arena configuration.
- Database constraints prevent duplicate authoritative records.
- Health distinguishes process liveness from database readiness.
- Unit tests cover schema and state transitions.

### SC-007 - Define And Implement The Service Checker Plugin Contract

**Linear:** P0 / `gameserver`, `checkers`, `service-contract`

**Dependencies:** SC-006

**Agent prompt**

> Define a typed checker/plugin contract that supports HTTP and future TCP
> services. A plugin must expose metadata plus PUT, GET, and benign CHECK
> operations with timeouts and structured UP, DOWN, MUMBLE, and CORRUPT
> outcomes. Implement the contract for TurtleNotes using legitimate user
> workflows where practical. Keep reference exploits separate from checkers.
> Document how a new service supplies its checker.

**Acceptance criteria**

- Checker results are structured and persisted.
- Timeouts and exceptions map to deterministic statuses.
- TurtleNotes checks more than `/health`.
- GET proves a previously planted flag remains retrievable.
- Checker credentials are team/service scoped.
- Unit tests cover every checker status.

### SC-008 - Implement Round Scheduling And Flag Lifecycle

**Linear:** P0 / `gameserver`, `rounds`, `flags`

**Dependencies:** SC-006, SC-007

**Agent prompt**

> Implement the gameserver tick engine. Each round must create unique flags per
> team and service, call checker PUT operations, schedule CHECK/GET operations
> with bounded concurrency, expire flags after configurable rounds, and persist
> all outcomes. Use a deterministic clock abstraction in tests. Ensure retries
> cannot create duplicate rounds or flags. Add pause, resume, and single-step
> controls for operators.

**Acceptance criteria**

- Round numbers are monotonic and unique per match.
- Flags are cryptographically random and unique.
- Plant failures and checker failures do not corrupt round state.
- Flag expiry is enforced from persisted round data.
- Restarting during a round resumes or fails it deterministically.
- Tests use a fake clock and cover retry/idempotency behavior.

### SC-009 - Implement Authenticated Flag Submission

**Linear:** P0 / `gameserver`, `api`, `security`

**Dependencies:** SC-008

**Agent prompt**

> Add an authenticated flag-submission API. Use team-scoped credentials from
> arena configuration, store only appropriately protected secrets, validate the
> flag format, and reject unknown, expired, duplicate, and self-owned flags.
> Make concurrent duplicate submissions safe at the database layer. Add basic
> per-team rate limiting and structured response codes suitable for bots.

**Acceptance criteria**

- Valid opponent flags are accepted once per attacker.
- Duplicate, self-owned, expired, malformed, and unknown flags have distinct
  machine-readable outcomes.
- Concurrent duplicate requests award points once.
- Logs never expose team tokens or full sensitive headers.
- API tests cover authentication, rate limiting, and all outcomes.

### SC-010 - Implement Deterministic Scoring

**Linear:** P0 / `gameserver`, `scoring`

**Dependencies:** SC-007, SC-009

**Agent prompt**

> Implement a documented scoring policy from immutable score events. Start with
> configurable attack, defense, and SLA components suitable for local
> experiments. Scores must be reproducible by replaying persisted flags,
> submissions, and checker results. Do not update aggregate totals as the only
> source of truth. Expose current standings and per-round breakdown APIs.

**Acceptance criteria**

- Recalculation from events matches stored/displayed standings.
- Duplicate submissions cannot duplicate score.
- SLA and attack/defense components are independently visible.
- Ties have a documented deterministic ordering.
- Scoring parameters are stored with the match.
- Unit tests cover multi-team, duplicate, expiry, and failed-SLA cases.

## P1 Tasks

### SC-011 - Add A Minimal Scoreboard And Operator Console

**Linear:** P1 / `frontend`, `gameserver`, `operations`

**Dependencies:** SC-006, SC-008, SC-010

**Status:** Implemented on 2026-06-13 in the gameserver dashboard/operator APIs
and the visualizer scoreboard console.

**Agent prompt**

> Extend the existing visualizer or add a focused gameserver UI that shows match
> state, round timer, standings, score breakdown, and service/checker status.
> Add authenticated local operator controls for start, pause, resume, step, and
> finish. Consume authoritative gameserver APIs rather than parsing Compose for
> runtime state. Keep topology visualization as a separate concern.

**Acceptance criteria**

- Standings and service state update without a full page reload.
- Operator actions require an operator credential.
- UI clearly distinguishes configured topology from live runtime health.
- Failed API calls and stale data are visible.

### SC-012 - Connect Bots To Flag Submission And Match Context

**Linear:** P1 / `bots`, `gameserver`, `integration`

**Dependencies:** SC-009, SC-010

**Agent prompt**

> Extend `BotContext` with current round, allowed targets, team credential, and
> gameserver endpoint. Add a submission client and structured capture results.
> A captured flag should be submitted once, and the bot should record the
> gameserver outcome without printing secrets. Remove hardcoded team-count and
> service assumptions in favor of gameserver/config discovery.

**Acceptance criteria**

- Bot-captured flags reach the submission API automatically.
- Duplicate local captures do not create submission storms.
- Submission outcomes are visible in structured logs.
- Team credentials are scoped and not committed or printed.
- Bot tests use a fake gameserver.

### SC-013 - Split Offensive Bots From Defensive Service Control

**Linear:** P1 / `bots`, `architecture`, `security`

**Dependencies:** SC-003, SC-016

**Agent prompt**

> Resolve the broken watchdog capability. Offensive bots currently run in
> `teamN-ssh`, while Docker control and service source exist in `teamN-vuln`.
> Define separate attacker and defender capabilities, or introduce a narrow
> team-local service-control API. Do not solve this by silently mounting the
> unrestricted host Docker socket into every gateway. Remove or disable actions
> whose required capabilities are unavailable and make capability discovery
> explicit.

**Acceptance criteria**

- Every advertised action declares and checks required capabilities.
- Defensive restart/patch actions work in the supported architecture.
- Offensive agents cannot control other teams' containers.
- The old watchdog failure has a regression test.
- Documentation states where each bot role executes.

### SC-014 - Add A Model-Backed Agent Planner Adapter

**Linear:** P1 / `agents`, `bots`, `ai`

**Dependencies:** SC-012, SC-013, SC-015

**Agent prompt**

> Implement a provider-neutral planner adapter for model-backed agents while
> preserving the existing typed action registry. Give the planner structured
> observations, action schemas, budget limits, and previous results. Validate
> planner output before execution. Add a deterministic fake planner for tests.
> Keep model credentials on the host/control plane and define how plans are
> delivered to team execution safely.

**Acceptance criteria**

- Model output cannot invoke unregistered actions or invalid targets.
- Time, token/cost, and action budgets are enforceable.
- Planner failures do not crash the bot loop.
- Fake-planner tests cover valid, invalid, timeout, and retry cases.
- One documented example runs an agent through a full round.

### SC-015 - Persist Structured Match And Agent Telemetry

**Linear:** P1 / `observability`, `evaluation`, `agents`

**Dependencies:** SC-006

**Agent prompt**

> Define a versioned event schema for rounds, checker operations, submissions,
> scores, network observations, bot plans, actions, patches, command results,
> model usage, and errors. Persist events with match/team correlation IDs.
> Provide a JSON export suitable for replay and experiment analysis. Redact
> credentials and define retention limits for payloads.

**Acceptance criteria**

- Events have stable type, timestamp, match, round, team, and correlation data.
- Sensitive values are redacted at ingestion.
- A completed match can be exported without scraping logs.
- Metrics can compare agents by flags, SLA, patch time, actions, cost, and errors.

### SC-016 - Write And Enforce The Threat Model

**Linear:** P1 / `security`, `architecture`

**Dependencies:** SC-003

**Agent prompt**

> Write `docs/THREAT_MODEL.md` for trusted local development and future
> untrusted competition modes. Inventory Docker socket access, host networking,
> Linux capabilities, bind mounts, credentials, exposed ports, agent command
> execution, and challenge escape risks. Define explicit trust boundaries and
> required controls for each mode. Add startup banners or hard gates so users
> cannot mistake trusted mode for isolation.

**Acceptance criteria**

- Every privileged mount/capability has an owner and rationale.
- Trusted and untrusted modes have distinct guarantees.
- Known escape paths are documented.
- README and doctor output link to the threat model.

### SC-017 - Implement Per-Team Isolation Mode

**Linear:** P1 / `isolation`, `docker`, `security`

**Dependencies:** SC-016

**Agent prompt**

> Prototype and implement the selected per-team isolation design, such as
> rootless Docker daemons, Docker-in-Docker sidecars, or microVM-backed workers.
> Preserve the team workspace and service-plugin contracts while preventing one
> team from controlling host or opponent containers. Measure startup time and
> resource overhead. Keep trusted local mode available when useful.

**Acceptance criteria**

- A team cannot list, stop, mount, or exec into another team's containers.
- Team patches and app rebuilds still work.
- Network targeting and gameserver access remain controlled.
- Isolation tests demonstrate blocked cross-team control attempts.
- Performance trade-offs are documented.

### SC-018 - Add Resource Limits And Failure Containment

**Linear:** P1 / `infra`, `reliability`, `security`

**Dependencies:** SC-003, SC-016

**Agent prompt**

> Add configurable CPU, memory, process, disk, log, and network limits for team
> services and agents. Define what happens when a team exceeds a limit. Ensure
> one crashed or abusive team cannot stop rounds or exhaust the host. Surface
> limit violations in operator status and telemetry.

**Acceptance criteria**

- Limits are generated from arena configuration.
- OOM, restart loops, and disk exhaustion are visible.
- Gameserver and operator control plane retain reserved resources.
- Tests cover at least memory and restart-limit behavior.

### SC-019 - Expand CI And Component Test Coverage

**Linear:** P1 / `testing`, `ci`, `quality`

**Dependencies:** SC-005

**Agent prompt**

> Expand CI beyond script lint and frontend build. Add unit tests for bot config,
> planners, actions, bot API validation, firewall parsing/classification,
> checker plugins, gameserver state, submissions, and scoring. Add ShellCheck,
> lint all Python modules, validate every Dockerfile, and keep the reduced
> end-to-end match as a required check. Avoid tests that depend on arbitrary
> sleep durations.

**Acceptance criteria**

- All Python source is linted and formatted consistently.
- Bot, firewall, service, and gameserver behavior have focused tests.
- Every Dockerfile and generated Compose variant is validated.
- CI failures identify the failing layer and preserve useful logs.

## P2 Tasks

### SC-020 - Add A Vulnerable TCP Binary Service

**Linear:** P2 / `service`, `binary`, `challenge`

**Dependencies:** SC-007, SC-019

**Agent prompt**

> Add a small intentionally vulnerable TCP binary service as the second service
> plugin. Include reproducible builds, a clear vulnerability, PUT/GET/CHECK
> checker operations, a reference exploit, patch guidance, and per-team data
> persistence. Prove the platform does not assume HTTP in bots, checkers,
> networking, or visualization.

**Acceptance criteria**

- The service participates in flag planting, checking, exploitation, and scoring.
- A minimal patch blocks the exploit while preserving checker behavior.
- CI runs its checker and reference exploit.

### SC-021 - Spike Procedural Vulnerable-Service Generation

**Linear:** P2 / `research`, `procedural-generation`, `challenge`

**Dependencies:** SC-007, SC-015, SC-019

**Agent prompt**

> Investigate procedural vulnerable-service generation as a time-boxed spike.
> Define what can vary safely: route names, data schemas, vulnerability
> parameters, decoys, binary constants, or service composition. Produce a small
> deterministic prototype from a seed plus a checker and exploit oracle. Do not
> merge generated challenges into the main match path until solvability,
> checker correctness, and reproducibility are proven.

**Acceptance criteria**

- The same seed reproduces identical source and behavior.
- Generated instances always include a passing checker and known exploit.
- The report documents diversity, leakage risks, and validation strategy.

### SC-022 - Add Participant, Organizer, Service-Author, And Agent Guides

**Linear:** P2 / `documentation`, `onboarding`

**Dependencies:** SC-011, SC-014

**Agent prompt**

> Split operational documentation by role. Add concise guides for organizers,
> participants, service/checker authors, and agent developers. Include clean
> setup, match operations, troubleshooting, backup/reset, service plugin
> examples, bot/planner examples, and security warnings. Keep README as the
> shortest verified entry point and link to deeper guides.

**Acceptance criteria**

- A new organizer can run a smoke match from a clean host.
- A service author can add a checker-backed challenge without reading core code.
- An agent author can implement a planner and run it in a test match.
- Commands are exercised by documentation tests or CI where practical.

## Existing Linear Task Triage

| Existing task | Recommended treatment |
|---|---|
| FLY-14 Add integration tests for the full competition lifecycle | Promote to P0 and implement as SC-005 |
| FLY-11 Implement Docker-in-Docker mode for full isolation | Split into SC-016 threat model and SC-017 isolation; do after trusted MVP |
| FLY-13 Create a binary vulnerable application (TCP) | Keep, but schedule as SC-020 after checker/plugin contracts |
| FLY-20 Investigate procedural vulnerable application creation | Keep as a time-boxed SC-021 spike after deterministic evaluation exists |
| FLY-18 Add smarter bots (Agents) | Reframe as SC-012 through SC-015; gameserver and telemetry come first |
| FLY-42 Improve Bots | Close or split into concrete SC-012, SC-013, SC-014, and SC-019 issues |

## Recommended Execution Order

1. SC-001, SC-002, SC-003
2. SC-004 and SC-005
3. SC-006, SC-007, SC-008
4. SC-009 and SC-010
5. SC-011 and SC-012
6. SC-013, SC-015, then SC-014
7. SC-016, SC-017, SC-018
8. SC-019, SC-020, SC-021, SC-022

## MVP Exit Checklist

- [ ] Clean checkout to healthy two-team arena is one command.
- [ ] No manual app startup is required.
- [ ] No orphan or partial team silently counts as healthy.
- [ ] Team-to-team enforcement and events are proven.
- [ ] Ten automated rounds complete.
- [ ] PUT, GET, and CHECK run for every team.
- [ ] Flag submissions enforce auth, ownership, expiry, and deduplication.
- [ ] Scores replay deterministically.
- [ ] Scoreboard shows live and historical state.
- [ ] A bot captures and submits at least one flag.
- [ ] A defensive patch blocks an exploit without failing SLA.
- [ ] CI runs a reduced end-to-end match.
- [ ] Trusted-local security limitations are explicit.
