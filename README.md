# Sandcastle

Sandcastle is a local Docker-based Attack & Defense CTF prototype for testing
software agents that patch their own services and attack opponents.

The repository currently provides team environments, an intentionally
vulnerable service, scripted bots, a topology visualizer, and a network-monitor
prototype. It does **not** yet provide a gameserver, timed rounds, authoritative
flag submission, SLA scoring, or a scoreboard.

- Product direction: [`VISION.md`](VISION.md)
- Current audit and prioritized agent backlog:
  [`docs/PROJECT_AUDIT_AND_BACKLOG.md`](docs/PROJECT_AUDIT_AND_BACKLOG.md)
- Infrastructure details: [`docs/architecture.md`](docs/architecture.md)

## Current Components

| Component | Purpose | Current status |
|---|---|---|
| `config/arena.env` | Canonical topology and runtime defaults | Implemented and validated by setup |
| `scripts/arena.sh` | Complete arena lifecycle and health contract | Implemented |
| `teamN-ssh` | Host-facing team gateway | Implemented |
| `teamN-vuln` | Mutable Linux machine with service source and Docker CLI | Implemented |
| `teamN-vuln-app` | Per-team vulnerable Flask service | Generated, started, and health-checked automatically |
| `services/example-vuln` | TurtleNotes challenge template and exploits | Implemented |
| `bot/` | Scripted action/planner runtime and local control API | Offensive path works; watchdog is currently ineffective |
| `firewall/` | Source-masking proxy and WebSocket activity feed | Linux-host dependent and not yet fail-safe |
| `visualizer/` | React topology, event, and bot UI | Implemented |
| Gameserver/checkers/scoring | Competition authority | Not implemented |

## Requirements

The current networking implementation targets a native Linux Docker host.
Docker Desktop on macOS or Windows is not a supported firewall environment.

Required:

- Linux
- Docker Engine with the `docker compose` plugin
- Bash
- permission to access `/var/run/docker.sock`

Optional operator tools:

- Python 3.10+ for `bot/bot_api.py`
- Node.js 22+ and npm for the visualizer

The current trusted-local mode mounts the host Docker socket into every
`teamN-vuln` container. This is not a security boundary. Do not expose the
arena to untrusted participants.

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
- local bot API prerequisites and health

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
credentials, startup timeout, bot defaults, and future round defaults.

Keep it as simple `KEY=VALUE` entries. `scripts/setup.sh` validates the values
and rejects port collisions, unsupported subnet layouts, missing templates,
and incomplete required fields.

`./scripts/setup.sh --teams N` is a convenience that persists
`ARENA_TEAM_COUNT=N` before generation. Other topology changes should be made
directly in the config file.

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

Startup exits non-zero if infrastructure or any required app is not healthy.
Override the timeout for one run with `--timeout SEC`.

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

The offensive actions work against the bundled challenge. The
`maintain.watchdog` action does not currently work because `teamN-ssh` does not
have Docker control; this is tracked as SC-013.

More detail: [`bot/bot.md`](bot/bot.md).

## Run The Visualizer And Bot UI

Use two terminals from the repository root.

Terminal 1, local bot-control bridge:

```bash
python3 bot/bot_api.py
```

Terminal 2, visualizer:

```bash
cd visualizer
npm ci
npm run dev
```

Open `http://localhost:5173`.

The topology view parses the generated Compose file. It is not an authoritative
view of running container health. The Bot view and firewall feed use the host
and ports in `config/arena.env`.

## Firewall And Activity Feed

With the default configuration, the firewall container exposes a WebSocket
feed at:

```text
ws://localhost:6789
```

The current implementation depends on team bridge traffic traversing host
`iptables` PREROUTING. Verify that traffic is actually reaching the rule:

```bash
docker exec sandcastle-firewall \
  iptables -t nat -L PREROUTING -n -v --line-numbers
```

Generate a cross-team request, then check that the Sandcastle redirect rule's
packet counter increased. A zero counter means source masking and TCP activity
events are not active even if the firewall container reports healthy. This is a
known P0 issue tracked as SC-004.

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

Check the firewall rule packet counter as described above. Container health
alone does not prove bridge traffic is being intercepted.

## Repository Layout

```text
.
├── VISION.md
├── README.md
├── config/arena.env                # canonical arena configuration
├── docker-compose.yml              # generated topology
├── bot/                            # team bot runtime and local bridge
├── context/context.md              # broad original A&D design notes
├── docker/
│   ├── ssh/Dockerfile
│   └── vuln/Dockerfile
├── docs/
│   ├── architecture.md
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
bash -n scripts/*.sh bot/*.sh
./tests/doctor_test.sh
./tests/setup_test.sh
./tests/arena_test.sh
python3 -B -m py_compile \
  scripts/gen_compose.py \
  bot/*.py bot/bot_lib/*.py \
  firewall/firewall.py \
  services/example-vuln/app/app.py
docker compose config --quiet
cd visualizer && npm ci && npm run build
```

The missing end-to-end competition test is tracked as SC-005.
