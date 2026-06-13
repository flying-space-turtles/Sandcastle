# Sandcastle Bot

The bot is a deployable team actor. You create a bot profile, choose its
actions, then deploy it into one or more `teamN-ssh` containers. Once running,
it acts as that team and targets the other teams from inside the CTF network.

## Pieces

| Path | Where | Role |
|---|---|---|
| `deploy.sh` | Host | Copies bot files into team SSH containers and starts/stops them |
| `bot_api.py` | `bot-controller` | Persistent deployment API, state, logs, and event inspection |
| `bot.py` | `teamN-ssh` | Runtime loop: loads config, asks a planner for tasks, runs actions |
| `bot_lib/actions.py` | `teamN-ssh` | Action registry: recon, exploits, probes, maintenance |
| `bot_lib/planners.py` | `teamN-ssh` | Planner registry and external planner hook |
| `bot.sh` | `teamN-ssh` | Older interactive shell helper |

The Python runtime is deliberately pluggable: actions are small classes with a
stable `run(ctx, target_team)` method, and planners produce ordered
`BotTask(target_team, action_id)` items. A future AI agent can slot in as a
planner without changing deploy or the visualizer flow.

> Current limitation: bots run in `teamN-ssh`, which does not have Docker CLI,
> the host Docker socket, or the generated service source. Offensive actions
> work, but `maintain.watchdog` cannot currently restart the vulnerable machine.
> The attacker/defender capability split is tracked as SC-013 in
> `docs/PROJECT_AUDIT_AND_BACKLOG.md`.

## Visualizer Flow

Start the platform:

```bash
./scripts/arena.sh up
```

In the visualizer, open `Bot`:

1. Create a bot profile.
2. Pick a planner and actions.
3. Choose opponent policy.
4. Select the team containers where the bot should run.
5. Deploy.

The controller address, team count, service port, IP pattern, and default loop
interval come from `config/arena.env`. It creates one durable deployment record
per selected team, invokes `bot/deploy.sh`, and archives telemetry when a
deployment stops or is replaced.

The controller is published on `127.0.0.1` and mounts the host Docker socket.
It therefore has Docker-host authority and must not be exposed outside the
trusted local operator environment.

Captured flags are immediately submitted to `/api/flags/submit`. Team
credentials are injected only into the runtime configuration inside the
selected SSH container. UI deployment records expose redacted flag
fingerprints and submission outcomes, never tokens or raw flags.

## CLI Quickstart

```bash
# Deploy the default recon-only bot profile to teams 2, 3, and 4.
cd bot
./deploy.sh 2 3 4

# Explicit recon-only bot.
./deploy.sh --actions recon.health --planner recon_first 2

# Attack bot with the example exploit chain.
./deploy.sh --actions recon.health,exploit.path_traversal,exploit.cmdi,exploit.sqli 2

# Deploy against selected target teams only.
./deploy.sh --target-policy selected --target-teams 1,3 2

# Check state and logs.
./deploy.sh --status
./deploy.sh --logs 2
```

Useful environment overrides:

| Variable | Default | Description |
|---|---:|---|
| `LOOP_INTERVAL` | `ARENA_BOT_LOOP_SECONDS` | Seconds between rounds |
| `WATCHDOG` | `false` | Run the maintenance watchdog before each round |

Topology-bound bot defaults are loaded from the canonical arena config, which
`deploy.sh` also copies into each target container.

## Built-In Actions

| Action ID | Category | Purpose |
|---|---|---|
| `recon.health` | Recon | Check `/health` |
| `exploit.path_traversal` | Exploit | Read `../flag.txt` through `/export` |
| `exploit.cmdi` | Exploit | Inject `cat /app/data/flag.txt` through diagnostics |
| `exploit.sqli` | Exploit | Bypass login and read admin notes |
| `probe.plant_endpoint` | Probe | Probe `/internal/plant` with a bad token |
| `maintain.watchdog` | Maintenance | Restart own vuln machine if Docker access is available |

See the machine-readable catalog:

```bash
python3 bot/bot.py --catalog
```

## External Planner Contract

Set `planner` to `module:object` in the JSON config or pass
`--planner module:object`. The imported object can be a class instance or a
class with:

```python
def plan(ctx, override_target=None):
    yield BotTask(target_team=2, action_id="recon.health")
    yield BotTask(target_team=2, action_id="exploit.path_traversal")
```

The `ctx` object exposes team identity, service URL helpers, HTTP helpers, and
the loaded bot config.
