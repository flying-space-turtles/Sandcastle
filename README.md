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
| `scripts/` | Generate and manage team infrastructure | Works, but app startup is still manual |
| `teamN-ssh` | Host-facing team gateway | Implemented |
| `teamN-vuln` | Mutable Linux machine with service source and Docker CLI | Implemented |
| `teamN-vuln-app` | Per-team vulnerable Flask service | Generated separately; not started by `start.sh` |
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

## Quick Start

The following starts a fresh four-team prototype.

### 1. Generate Team Workspaces

```bash
./scripts/setup.sh --teams 4
```

This writes the generated root `docker-compose.yml` and creates:

```text
teams/generated/team1/example-vuln
teams/generated/team2/example-vuln
teams/generated/team3/example-vuln
teams/generated/team4/example-vuln
```

Do not edit the root `docker-compose.yml` directly. Change
`scripts/setup.sh`, then regenerate it.

Existing team service copies are preserved so patches are not lost. To replace
all generated copies with the canonical template:

```bash
./scripts/setup.sh --teams 4 --overwrite-services
```

`--overwrite-services` deletes generated team patches. Use it only for a fresh
or intentionally reset arena.

### 2. Start The Infrastructure

```bash
./scripts/start.sh
docker compose ps
```

This starts:

- `teamN-ssh` at `10.10.N.2`
- `teamN-vuln` at `10.10.N.3`
- `sandcastle-firewall`

It does not currently start `teamN-vuln-app`.

### 3. Start Every Vulnerable App

Until the lifecycle work in SC-003 is implemented, start each generated app
from its vulnerable machine:

```bash
for team in 1 2 3 4; do
  docker exec "team${team}-vuln" bash -lc \
    "cd /home/team${team}/example-vuln && docker compose up -d --build --force-recreate"
done
```

`--force-recreate` avoids stale app containers that still reference an older
`teamN-vuln` network namespace.

### 4. Verify The Arena

```bash
for team in 1 2 3 4; do
  docker exec "team${team}-vuln" \
    curl -fsS "http://team${team}-vuln:8080/health"
  echo
done
```

Each request should return:

```json
{"status":"ok"}
```

Check app containers:

```bash
docker ps --filter label=sandcastle.role=vuln-app \
  --format 'table {{.Names}}\t{{.Status}}'
```

At this point the services can attack each other, but there is no gameserver or
score calculation.

## Team Access And Patching

Each team has:

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

Bots run inside `teamN-ssh` and attack from that team's network identity.
Start all vulnerable apps first.

```bash
NUM_TEAMS=4 ./bot/deploy.sh \
  --actions recon.health,exploit.path_traversal,exploit.cmdi,exploit.sqli \
  --planner recon_first \
  2
```

Inspect status and logs:

```bash
NUM_TEAMS=4 ./bot/deploy.sh --status
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
view of running container health. The Bot view uses
`http://localhost:7878`.

## Firewall And Activity Feed

The firewall container exposes a WebSocket feed at:

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

## Stop, Reset, And Clean Up

Stop infrastructure and app containers while preserving named data volumes:

```bash
./scripts/stop.sh
```

Rebuild and restart after deleting app data volumes:

```bash
./scripts/reset.sh
```

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

The app container references an old `teamN-vuln` container. Recreate it:

```bash
docker exec team1-vuln bash -lc \
  'cd /home/team1/example-vuln && docker compose up -d --build --force-recreate'
```

### Generated team directory exists but is empty

For a disposable arena, restore all generated service copies:

```bash
./scripts/setup.sh --teams 4 --overwrite-services
```

This removes generated patches.

### Old teams still run after reducing team count

The current start path does not reliably remove orphans. For a disposable local
arena:

```bash
./scripts/cleanup.sh --keep-images
./scripts/setup.sh --teams 4
./scripts/start.sh
```

### Firewall UI connects but shows no traffic

Check the firewall rule packet counter as described above. Container health
alone does not prove bridge traffic is being intercepted.

## Repository Layout

```text
.
├── VISION.md
├── README.md
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
├── services/
│   └── example-vuln/
├── teams/generated/                # ignored mutable team copies
└── visualizer/
```

## Development Checks

```bash
bash -n scripts/*.sh bot/*.sh
./tests/doctor_test.sh
python3 -B -m py_compile \
  scripts/gen_compose.py \
  bot/*.py bot/bot_lib/*.py \
  firewall/firewall.py \
  services/example-vuln/app/app.py
docker compose config --quiet
cd visualizer && npm ci && npm run build
```

The missing end-to-end competition test is tracked as SC-005.
