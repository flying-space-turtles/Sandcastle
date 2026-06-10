# Sandcastle Bot

The bot is a deployable team actor. You create a bot profile, choose its
actions, then deploy it into one or more `teamN-ssh` containers. Once running,
it acts as that team and targets the other teams from inside the CTF network.

## Pieces

| Path | Where | Role |
|---|---|---|
| `deploy.sh` | Host | Copies bot files into team SSH containers and starts/stops them |
| `bot_api.py` | Host | Local HTTP bridge used by the visualizer Bot tab |
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

Start the platform, then start the local bot bridge:

```bash
./scripts/start.sh
python3 bot/bot_api.py
```

In the visualizer, open `Bot`:

1. Create a bot profile.
2. Pick a planner and actions.
3. Choose opponent policy.
4. Select the team containers where the bot should run.
5. Deploy.

The bridge listens on `http://localhost:7878` and only shells out to
`bot/deploy.sh`.

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
| `NUM_TEAMS` | `4` | Total teams visible to the bot |
| `LOOP_INTERVAL` | `60` | Seconds between rounds |
| `WATCHDOG` | `false` | Run the maintenance watchdog before each round |

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
