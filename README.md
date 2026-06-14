# Sandcastle

## Quick Start

Sandcastle reads its topology, ports, credentials, and match settings from
[`config/arena.env`](config/arena.env).

### 1. Configure The Operator Token

`ARENA_OPERATOR_TOKEN` is the organizer credential used by the UI and API to
start, pause, finish, and restart matches.

The repository contains this local-development default:

```text
sandcastle-local-operator-token-change-me
```

It is not unique, is not regenerated when the arena starts, and must not be
used for a shared or exposed event. Generate a token once and write it into the
configuration:

```bash
OPERATOR_TOKEN="$(openssl rand -hex 32)"
sed -i "s/^ARENA_OPERATOR_TOKEN=.*/ARENA_OPERATOR_TOKEN=${OPERATOR_TOKEN}/" config/arena.env
```

The configured token remains the same across restarts until you replace it.
Print it later with either command:

```bash
sed -n 's/^ARENA_OPERATOR_TOKEN=//p' config/arena.env
./scripts/setup.sh --show-access
```

Team submission tokens are separate credentials generated from
`ARENA_TEAM_TOKEN_PATTERN`. The organizer token is not a team token.

### 2. Prepare The Host

Sandcastle is strictest and best supported on native Linux Docker Engine. Docker
Desktop/macOS is allowed when the Docker runtime can prove the firewall path from
inside the `sandcastle-firewall` container. Check host-side Docker orchestration,
then run the read-only doctor:

```bash
./scripts/firewall-preflight.sh --check
./scripts/doctor.sh
```

### 3. Start The Complete Arena

This is the main startup command:

```bash
./scripts/arena.sh up
```

It validates and generates configuration, builds and starts the team
containers, vulnerable applications, firewall, gameserver, and bot controller,
then waits for health checks. Do not run a separate `docker compose up`.

Useful lifecycle commands:

```bash
./scripts/arena.sh status
./scripts/arena.sh restart
./scripts/arena.sh down
```

### 4. Start The Operator UI

The backend arena is started by `arena.sh`; the Vite development UI is a
separate process:

```bash
cd visualizer
npm ci
npm run dev
```

Open `http://localhost:5173`, select **Match**, open **Match controls**, and
paste `ARENA_OPERATOR_TOKEN`.

### 5. Start Or Restart A Match

For a new arena, click **Start match** in the UI. From the terminal:

```bash
OPERATOR_TOKEN="$(sed -n 's/^ARENA_OPERATOR_TOKEN=//p' config/arena.env)"

curl -s -X POST http://localhost:8000/api/match/start \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
```

To start a clean match after one has already run:

1. Finish the current running or paused match.
2. Restart it, which deletes its rounds, flags, submissions, checker results,
   and scores.
3. Start it again to create a clean round 1.

```bash
curl -s -X POST http://localhost:8000/api/match/finish \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

curl -s -X POST http://localhost:8000/api/match/restart \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

curl -s -X POST http://localhost:8000/api/match/start \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
```

The restart endpoint accepts only `FINISHED` or `FAILED` matches. In the UI,
the equivalent sequence is **Finish match**, **Restart match**, then
**Start match**.

---

Sandcastle is a local Docker-based Attack & Defense CTF prototype for testing
software agents that patch their own services and attack opponents.

The repository currently provides team environments, an intentionally
vulnerable service, scripted bots, a topology visualizer, a network monitor, a
persistent gameserver core, a typed checker framework, and deterministic
competition scoring with a live scoreboard and operator console.

- Product direction: [`VISION.md`](VISION.md)
- Current audit and prioritized agent backlog:
  [`docs/PROJECT_AUDIT_AND_BACKLOG.md`](docs/PROJECT_AUDIT_AND_BACKLOG.md)
- Infrastructure details: [`docs/architecture.md`](docs/architecture.md)
- Security model and escape paths: [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)

## Current Components

| Component | Purpose | Current status |
|---|---|---|
| `config/arena.env` | Canonical topology and runtime defaults | Implemented and validated by setup |
| `scripts/arena.sh` | Complete arena lifecycle and health contract | Implemented |
| `teamN-ssh` | Host-facing team gateway | Implemented |
| `teamN-vuln` | Mutable Linux machine with service source and Docker CLI | Implemented |
| `teamN-vuln-app` | Per-team vulnerable Flask service | Generated, started, and health-checked automatically |
| `services/example-vuln` | TurtleNotes challenge template and exploits | Implemented |
| `bot/` | Scripted runtime, managed deployment controller, telemetry, and submissions | Implemented |
| `firewall/` | Source-masking proxy and WebSocket activity feed | Enforced and smoke-tested on native Linux |
| `visualizer/` | Live scoreboard, operator controls, topology, event, and bot UI | Implemented |
| `gameserver/` | Match state, rounds, flags, submissions, scoring, and recovery | Implemented |
| Service checkers | PUT, GET, CHECK contract and TurtleNotes plugin | Implemented |
| Scoring/standings | Replayable attack, defense, and SLA scoring APIs | Implemented |

## Requirements

The current networking implementation targets a native Linux Docker host.
Docker Desktop on macOS or Windows is permitted only when the firewall container
runtime proof and smoke test pass; native Linux remains the strict supported path.

Required:

- native Linux with `br_netfilter`
- Docker Engine with the `docker compose` plugin
- Bash
- permission to access `/var/run/docker.sock`

Optional operator tool:

- Node.js 22+ and npm for visualizer development

The current trusted-local mode mounts the host Docker socket into every
`teamN-vuln` container and the localhost-only bot controller. This is not a
security boundary. Do not expose the arena to untrusted participants.

For production-like tests with untrusted teams, generate Docker-in-Docker mode:

```bash
./scripts/setup.sh --dind
./scripts/arena.sh up
./tests/dind_isolation_test.sh
```

DinD gives every team its own `docker:dind` daemon, so teams cannot list or
control other teams' app containers through Docker. It is heavier than trusted
mode: each team gets a privileged sidecar, separate Docker storage, slower cold
builds, and more moving parts. Keep trusted mode for the fastest local
development loop.

**Before running a shared or exposed event, read
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).** It inventories every
privileged resource, defines the operating modes and their guarantees,
documents known escape paths, and lists the controls required before moving
to an untrusted competition. The doctor script (`./scripts/doctor.sh`) links
to this document for every warning it emits.

## Check Host And Arena Readiness

Run the read-only doctor before setup and whenever the arena behaves
unexpectedly:

```bash
./scripts/doctor.sh
```

The doctor checks:

- native Linux, Docker Engine, Compose, and Docker-socket access
- required SSH, firewall WebSocket, and proxy ports
- CTF subnet conflicts
- generated workspace completeness and team-count consistency
- running or stopped orphan team containers
- vulnerable-machine Docker access and vulnerable-app health
- firewall bridge-netfilter support, redirect rule, and packet counter
- managed bot controller prerequisites and health

Results are classified as:

- `PASS`: the check is satisfied
- `WARN`: trusted-local risk, optional component, or behavior not yet proven
- `FAIL`: a blocker; the command exits with status `1`

Every warning or failure includes a remediation. For automation:

```bash
./scripts/doctor.sh --format tsv
```

The TSV columns are `status`, `check_id`, `message`, and `remediation`.
The doctor never starts, stops, creates, deletes, or reconfigures arena
resources.

## Arena Configuration

Edit [`config/arena.env`](config/arena.env) to change the arena topology. It is
the canonical source for team count, network, service and host ports,
credentials, startup timeout, bot defaults, round duration, flag expiry, and
checker concurrency. `ARENA_OPERATOR_TOKEN` protects match-control mutations.

Keep it as simple `KEY=VALUE` entries. `scripts/setup.sh` validates the values
and rejects port collisions, unsupported subnet layouts, missing templates,
and incomplete required fields.

`./scripts/setup.sh --teams N` is a convenience that persists
`ARENA_TEAM_COUNT=N` before generation. Other topology changes should be made
directly in the config file.

`./scripts/setup.sh --dind` persists `ARENA_ISOLATION_MODE=dind` before
generation.

Routine setup output hides development passwords. To print SSH commands,
credentials, internal app targets, health commands, and local control URLs:

```bash
./scripts/setup.sh --show-access
```

## Round Controls

The gameserver creates rounds automatically while the match is `RUNNING`. Each
round persists one unique flag per team/service, then runs bounded PUT, CHECK,
and GET checker operations. See
[`docs/round-engine.md`](docs/round-engine.md) for lifecycle and recovery rules.

```bash
OPERATOR_TOKEN="$(sed -n 's/^ARENA_OPERATOR_TOKEN=//p' config/arena.env)"

# Start the match. The scheduler creates round 1 within about one second.
curl -s -X POST http://localhost:8000/api/match/start \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

curl -s -X POST http://localhost:8000/api/match/pause \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
curl -s -X POST http://localhost:8000/api/rounds/step \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
curl -s http://localhost:8000/api/rounds/current
```

Single-step requires a paused match and does not resume automatic scheduling.
Start, pause, resume, step, finish, and the generic state endpoint all require
the operator Bearer token.

## Flag Submission

Teams submit captured flags with their configured team ID and Bearer token. The
database stores only a salted PBKDF2 hash of each token.

```bash
curl -s -X POST http://localhost:8000/api/flags/submit \
  -H "Authorization: Bearer ${TEAM_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"team_id":1,"flag":"FLAG{0123456789abcdef0123456789abcdef}"}'
```

Responses always include a machine-readable `code`: `ACCEPTED`, `DUPLICATE`,
`SELF_OWNED`, `EXPIRED`, `MALFORMED`, `UNKNOWN`, `UNAUTHORIZED`, or
`RATE_LIMITED`. Rate-limited responses include `Retry-After`. Run
`./scripts/setup.sh --show-access` to print local development tokens.

## Scoring

The gameserver projects submissions and completed checker results into
immutable attack, defense, and SLA score events. Current and per-round results
are available from:

```bash
curl -s http://localhost:8000/api/standings
curl -s http://localhost:8000/api/rounds/1/scores
```

See [`docs/scoring.md`](docs/scoring.md) for component definitions, configurable
weights, replay rules, and deterministic tie ordering.

## Quick Start

The following starts the configured prototype.

### 1. Start The Complete Arena

```bash
./scripts/arena.sh up
```

To persist and start a different team count:

```bash
./scripts/arena.sh up --teams 2
```

`up` performs the complete organizer lifecycle:

- validates `config/arena.env` and generated workspaces
- reconciles stale containers outside the configured topology
- generates `docker-compose.yml` and per-team app Compose files
- builds and starts gateways, vulnerable machines, and the firewall
- removes stale app containers before attaching them to current parent
  containers
- builds and starts every `teamN-vuln-app`
- waits up to `ARENA_STARTUP_TIMEOUT_SECONDS` for every app `/health`
- proves cross-team TCP interception, source masking, and WebSocket emission

Startup exits non-zero if infrastructure, any required app, or firewall
enforcement is not healthy. Override the app timeout for one run with
`--timeout SEC`.

### 2. Inspect Status

```bash
./scripts/arena.sh status
```

Status reports each gateway, vulnerable machine, app container, active app
health result, and the firewall. It exits non-zero when the arena is not fully
ready. For automation:

```bash
./scripts/arena.sh status --format tsv
```

### Generation And Patch Preservation

`arena.sh up` calls `scripts/setup.sh`. Marked generated workspaces preserve
existing patches and repair missing required files. Unmarked directories are
treated as participant-owned and setup refuses to rewrite them.

To intentionally replace configured service copies from the canonical
template, use `./scripts/setup.sh --overwrite-services`. This is destructive to
team patches and prints an explicit warning.

## Team Access And Patching

With the default configuration, each team has:

| Team | Gateway | Vulnerable machine/app | Host SSH port | Credentials |
|---|---|---|---:|---|
| Team N | `10.10.N.2` | `10.10.N.3:8080` | `2200 + N` | `teamN` / `teamNpass` |

Example for Team 1:

```bash
ssh -p 2201 team1@localhost
ssh team1@team1-vuln
cd ~/example-vuln
vim app/app.py
docker compose up -d --build --force-recreate
curl http://team1-vuln:8080/health
```

The source directory is a bind mount from
`teams/generated/team1/example-vuln`. Rebuilding Team 1 does not change another
team's source.

The vulnerable app persists SQLite and flag data in a named volume:

```text
sandcastle_teamN-data
```

## Run A Bot

Bots run inside `teamN-ssh` and attack from that team's network identity. Run
`./scripts/arena.sh up` first.

```bash
./bot/deploy.sh \
  --actions recon.health,exploit.path_traversal,exploit.cmdi,exploit.sqli \
  --planner recon_first \
  2
```

Inspect status and logs:

```bash
./bot/deploy.sh --status
./bot/deploy.sh --logs 2
```

Stop the bot:

```bash
./bot/deploy.sh --stop 2
```

Captured flags are submitted automatically to the gameserver and recorded as
structured deployment events. The `maintain.watchdog` action remains limited
because `teamN-ssh` does not have Docker control.

More detail: [`bot/bot.md`](bot/bot.md).

## Run The Scoreboard And Operator Console

After `./scripts/arena.sh up`, start the UI:

```bash
cd visualizer
npm ci
npm run dev
```

Open `http://localhost:5173`.

The scoreboard polls the authoritative gameserver and shows match state, round
timing, component scores, and checker results. Paste the operator token printed
by `./scripts/setup.sh --show-access` to use Start, Pause, Resume, Step, and
Finish. After finishing a match, use Restart match to clear its rounds and
scores, return it to CREATED, and then use Start match for a clean round 1.

The topology view parses generated Compose configuration and is explicitly not
live container health. YAML editing and the raw parser inspector are not part
of the operator console.

The Bots view uses the Compose-managed controller started by
`./scripts/arena.sh up`; no additional Python process is required.

## Firewall And Activity Feed

With the default configuration, the firewall container exposes a WebSocket
feed at:

```text
ws://localhost:6789
```

The current implementation depends on team bridge traffic traversing the Docker
runtime namespace where `sandcastle-firewall` runs. The host script only verifies
Docker orchestration prerequisites; the firewall container validates bridge
netfilter visibility, installs the redirect rule, and binds proxy/WebSocket
ports.

`arena.sh up` starts the firewall, verifies the running container, and then
executes:

```bash
./scripts/smoke-network.sh
```

The smoke test sends a nonce-bearing TCP request from Team 1 to a temporary
listener in Team 2. It fails unless the redirect counter increases, Team 2 sees
the configured gateway as the masked source, and the WebSocket feed emits the
matching original source and destination. The probe does not modify service
source or persistent app data.

The firewall bounds its event queue and ICMP de-duplication cache. Packet and
netlink receive-buffer overflow is logged and capture continues rather than
terminating silently.

## Lifecycle Semantics

Stop all app and infrastructure containers while preserving source patches and
named app data volumes:

```bash
./scripts/arena.sh down
```

Recreate the entire running topology while preserving source and app data:

```bash
./scripts/arena.sh restart
```

Delete vulnerable-app data volumes, preserve source patches, and start clean:

```bash
./scripts/arena.sh reset
```

`scripts/start.sh`, `scripts/stop.sh`, and `scripts/reset.sh` remain
compatibility wrappers for `arena.sh up`, `down`, and `reset`.

Remove all Sandcastle containers, networks, volumes, and images:

```bash
./scripts/cleanup.sh
```

Keep built images:

```bash
./scripts/cleanup.sh --keep-images
```

Cleanup is destructive to generated runtime data volumes.

## Common Problems

Start troubleshooting with:

```bash
./scripts/doctor.sh
```

Use the stable check ID in each result to identify the failing layer.

### Permission denied on `/var/run/docker.sock`

Your user cannot access the Docker daemon. Fix Docker permissions for the host
before running Sandcastle. Do not work around this by making the socket
world-writable.

### App fails with `joining network namespace ... No such container`

The app container references an old `teamN-vuln` container. Recreate the full
topology:

```bash
./scripts/arena.sh restart
```

### Generated team directory exists but is empty

For a marked generated workspace, rerun setup to restore missing required
files while preserving existing files:

```bash
./scripts/setup.sh
```

If the directory is unmarked, inspect it first. To intentionally discard its
contents and replace it from the template:

```bash
./scripts/setup.sh --overwrite-services
```

This is destructive and prints an explicit warning.

### Old teams still run after reducing team count

The standard lifecycle reconciles stale containers when starting the requested
topology:

```bash
./scripts/arena.sh up --teams 4
```

### Firewall UI connects but shows no traffic

Run the behavioral verifier:

```bash
./scripts/smoke-network.sh
```

If preflight fails, run
`./scripts/firewall-preflight.sh --check`. If the smoke test fails, inspect
`docker compose logs firewall`; container health alone does not prove bridge
traffic is being intercepted.

## Repository Layout

```text
.
├── VISION.md
├── README.md
├── config/arena.env                # canonical arena configuration
├── docker-compose.yml              # generated topology
├── bot/                            # team bot runtime and deployment controller
├── context/context.md              # broad original A&D design notes
├── docker/
│   ├── ssh/Dockerfile
│   └── vuln/Dockerfile
├── docs/
│   ├── architecture.md
│   ├── THREAT_MODEL.md
│   └── PROJECT_AUDIT_AND_BACKLOG.md
├── firewall/
├── scripts/
│   └── arena.sh                    # organizer lifecycle entry point
├── services/
│   └── example-vuln/
├── teams/generated/                # ignored mutable team copies
└── visualizer/
```

## Development Checks

```bash
python3 -m pip install -r requirements-dev.txt
shellcheck --version
./scripts/run-tests.sh
```

Use the fast mode to skip only the visualizer build:

```bash
./scripts/run-tests.sh        # all checks including visualizer build
./scripts/run-tests.sh --fast # skip the visualizer build
```

For a disposable native Linux VM or self-hosted CI runner that can run
privileged containers, use the production-like DinD smoke:

```bash
./scripts/staging-dind-smoke.sh
```

For the full Docker integration test (SC-005) on a native Linux host:

```bash
./scripts/firewall-preflight.sh --check
./tests/integration_test.sh
```
