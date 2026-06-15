# Sandcastle Operator Guide

This guide keeps the operational details out of the project README while
preserving the commands needed to run, inspect, and develop the local arena.

## Requirements

Sandcastle is strictest and best supported on native Linux Docker Engine.
Docker Desktop on macOS or Windows is allowed only when the Docker runtime can
prove the firewall path from inside the `sandcastle-firewall` container.

Required:

- native Linux with `br_netfilter`
- Docker Engine with the `docker compose` plugin
- Bash
- permission to access `/var/run/docker.sock`

Optional operator tool:

- Node.js 22+ and npm for visualizer development

The default trusted-local mode mounts Docker access into team infrastructure.
Do not expose it to untrusted participants. For production-like tests, use
Docker-in-Docker mode:

```bash
./scripts/setup.sh --dind
./scripts/arena.sh up
./tests/dind_isolation_test.sh
```

## Quick Start

Sandcastle reads topology, ports, credentials, and match settings from
[`config/arena.env`](../config/arena.env).

### 1. Configure The Operator Token

`ARENA_OPERATOR_TOKEN` is the organizer credential used by the UI and API to
start, pause, finish, and restart matches. Replace the local-development
default before any shared or exposed event:

```bash
OPERATOR_TOKEN="$(openssl rand -hex 32)"
sed -i "s/^ARENA_OPERATOR_TOKEN=.*/ARENA_OPERATOR_TOKEN=${OPERATOR_TOKEN}/" config/arena.env
```

Print the configured token later with either command:

```bash
sed -n 's/^ARENA_OPERATOR_TOKEN=//p' config/arena.env
./scripts/setup.sh --show-access
```

Team submission tokens are separate credentials generated from
`ARENA_TEAM_TOKEN_PATTERN`.

### 2. Prepare The Host

```bash
./scripts/firewall-preflight.sh --check
./scripts/doctor.sh
```

### 3. Start The Complete Arena

```bash
./scripts/arena.sh up
```

Useful lifecycle commands:

```bash
./scripts/arena.sh status
./scripts/arena.sh restart
./scripts/arena.sh down
```

To persist and start a different team count:

```bash
./scripts/arena.sh up --teams 2
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

```bash
curl -s -X POST http://localhost:8000/api/match/finish \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

curl -s -X POST http://localhost:8000/api/match/restart \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

curl -s -X POST http://localhost:8000/api/match/start \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
```

The restart endpoint accepts only `FINISHED` or `FAILED` matches.

## What `arena.sh up` Does

`up` performs the complete organizer lifecycle:

- validates `config/arena.env` and generated workspaces
- reconciles stale containers outside the configured topology
- generates `docker-compose.yml` and per-team app Compose files
- builds and starts gateways, vulnerable machines, gameserver, bot controller,
  and firewall
- removes stale app containers before attaching them to current parent
  containers
- builds and starts every `teamN-vuln-app`
- waits up to `ARENA_STARTUP_TIMEOUT_SECONDS` for every app `/health`
- proves cross-team TCP interception, source masking, and WebSocket emission

Startup exits non-zero if infrastructure, any required app, or firewall
enforcement is not healthy. Override the app timeout for one run with
`--timeout SEC`.

## Readiness And Doctor Checks

Run the read-only doctor before setup and whenever the arena behaves
unexpectedly:

```bash
./scripts/doctor.sh
```

For automation:

```bash
./scripts/doctor.sh --format tsv
```

The TSV columns are `status`, `check_id`, `message`, and `remediation`. The
doctor never starts, stops, creates, deletes, or reconfigures arena resources.

## Arena Configuration

Edit [`config/arena.env`](../config/arena.env) to change the arena topology.
It is the canonical source for team count, network, service and host ports,
credentials, startup timeout, bot defaults, round duration, flag expiry, and
checker concurrency.

Convenience commands:

```bash
./scripts/setup.sh --teams N
./scripts/setup.sh --dind
./scripts/setup.sh --show-access
```

Routine setup output hides development passwords. `--show-access` prints SSH
commands, credentials, internal app targets, health commands, and local control
URLs.

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
team's source. The vulnerable app persists SQLite and flag data in a named
volume:

```text
sandcastle_teamN-data
```

## Round Controls

The gameserver creates rounds automatically while the match is `RUNNING`. Each
round persists one unique flag per team/service, then runs bounded PUT, CHECK,
and GET checker operations. See [round-engine.md](round-engine.md) for
lifecycle and recovery rules.

```bash
OPERATOR_TOKEN="$(sed -n 's/^ARENA_OPERATOR_TOKEN=//p' config/arena.env)"

curl -s -X POST http://localhost:8000/api/match/pause \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
curl -s -X POST http://localhost:8000/api/rounds/step \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"
curl -s http://localhost:8000/api/rounds/current
```

Single-step requires a paused match and does not resume automatic scheduling.

## Flag Submission And Scoring

Teams submit captured flags with their configured team ID and Bearer token:

```bash
curl -s -X POST http://localhost:8000/api/flags/submit \
  -H "Authorization: Bearer ${TEAM_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"team_id":1,"flag":"FLAG{0123456789abcdef0123456789abcdef}"}'
```

Current and per-round results:

```bash
curl -s http://localhost:8000/api/standings
curl -s http://localhost:8000/api/rounds/1/scores
```

See [scoring.md](scoring.md) for component definitions, configurable weights,
replay rules, and deterministic tie ordering.

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

More detail: [`bot/bot.md`](../bot/bot.md).

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

Remove all Sandcastle containers, networks, volumes, and images:

```bash
./scripts/cleanup.sh
```

Keep built images:

```bash
./scripts/cleanup.sh --keep-images
```

Cleanup is destructive to generated runtime data volumes.

## Troubleshooting

Start with:

```bash
./scripts/doctor.sh
```

Use the stable check ID in each result to identify the failing layer.

### Permission denied on `/var/run/docker.sock`

Your user cannot access the Docker daemon. Fix Docker permissions for the host
before running Sandcastle. Do not make the socket world-writable.

### App fails with `joining network namespace ... No such container`

The app container references an old `teamN-vuln` container. Recreate the full
topology:

```bash
./scripts/arena.sh restart
```

### Generated team directory exists but is empty

For a marked generated workspace, rerun setup to restore missing required files
while preserving existing files:

```bash
./scripts/setup.sh
```

If the directory is unmarked, inspect it first. To intentionally discard its
contents and replace it from the template:

```bash
./scripts/setup.sh --overwrite-services
```

This is destructive and prints an explicit warning.

### Firewall UI connects but shows no traffic

Run the behavioral verifier:

```bash
./scripts/smoke-network.sh
```

If preflight fails, run `./scripts/firewall-preflight.sh --check`. If the smoke
test fails, inspect `docker compose logs firewall`; container health alone does
not prove bridge traffic is being intercepted.

## Development Checks

```bash
python3 -m pip install -r requirements-dev.txt
./scripts/run-tests.sh
```

Use fast mode to skip only the visualizer build:

```bash
./scripts/run-tests.sh --fast
```

For a disposable native Linux VM or self-hosted CI runner that can run
privileged containers:

```bash
./scripts/staging-dind-smoke.sh
```

For PR-label-gated staging deployments to an Oracle VPS, see
[staging-deploy.md](staging-deploy.md).
