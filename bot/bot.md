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
| `service_control.py` | `teamN-vuln` | Team-local service-control API — health check and restart for the team's own app |

The Python runtime is deliberately pluggable: actions are small classes with a
stable `run(ctx, target_team)` method, and planners produce ordered
`BotTask(target_team, action_id)` items. A future AI agent can slot in as a
planner without changing deploy or the visualizer flow.

### Attacker / defender capability split

Offensive bots (`bot.py`) run in `teamN-ssh`, which has network access to all
teams but no Docker socket.  Defensive maintenance (watchdog, service restart)
requires Docker — available only in `teamN-vuln`.

The split is bridged by `service_control.py`, a small HTTP server started
automatically inside `teamN-vuln`.  It listens on port 7979 and accepts
requests **only from the team's own SSH container** (`10.10.N.2`).  The
watchdog action calls this API instead of touching the Docker socket directly,
so offensive agents running in `teamN-ssh` can never reach the Docker daemon.

```
teamN-ssh (bot.py)
  └─ HTTP → 10.10.N.3:7979/service/health     (read: is app running?)
  └─ HTTP → 10.10.N.3:7979/service/restart    (write: restart app)
         ↓ (only from 10.10.N.2)
teamN-vuln (service_control.py)
  └─ /var/run/docker.sock → docker restart teamN-vuln-app
```

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
| `maintain.watchdog` | Maintenance | Restart the team app via the service-control API if it is unhealthy |

See the machine-readable catalog:

```bash
python3 bot/bot.py --catalog
```

## Capabilities

Every action declares `required_capabilities` (a `frozenset`).  At startup the
bot probes what is available and logs the result.  If a required capability is
missing the action is skipped with a clear message rather than silently failing.

| Token | How acquired | Meaning |
|---|---|---|
| `network.attack` | Always | Can reach opponent service ports over ctf-network |
| `network.submit` | Always | Can submit flags to the gameserver |
| `docker.socket` | `/var/run/docker.sock` present | Direct Docker daemon access (only in `teamN-vuln`) |
| `service.control.local` | `GET 10.10.N.3:7979/ping` succeeds | Team-local service-control API reachable |

Probing happens once at `BotContext` construction.  Pass
`capabilities=frozenset()` when constructing `BotContext` in unit tests to
bypass the network probe.

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

## Model-Backed Agent Planner

Use the `model` planner to delegate action selection to an LLM. The planner
sends a structured observation to a bot-controller endpoint and receives a list
of tasks. LLM API keys **never enter team containers**.

### Credential boundary

```
teamN-ssh (bot.py)                    host (bot-controller)
  └─ RemoteModelPlannerAdapter            └─ /plan endpoint
       POST {observation, schemas}  →         └─ LLM_API_KEY (env, host only)
       ← [{target_team, action_id}]               └─ calls Claude / GPT / etc.
  PLAN_ENDPOINT  (URL, public)
  PLAN_TOKEN     (bearer, operator secret — not the LLM key)
```

The team container only needs two env vars:

| Variable | Description |
|---|---|
| `PLAN_ENDPOINT` | URL of the bot-controller `/plan` route |
| `PLAN_TOKEN` | Bearer token checked by the bot-controller (operator secret) |

Optional budget controls (all have safe defaults):

| Variable | Default | Description |
|---|---:|---|
| `PLAN_MAX_ACTIONS` | `20` | Max tasks accepted per round |
| `PLAN_TIMEOUT_SECONDS` | `10.0` | Wall-clock budget for each `/plan` call |
| `PLAN_MAX_TOKENS` | _(none)_ | Reject plan if `tokens_used` exceeds this |
| `PLAN_MAX_COST_USD` | _(none)_ | Reject plan if `cost_usd` exceeds this |

### Full round example

This example runs a bot through one complete round using a deterministic
`FakePlannerAdapter`. No arena and no API key are needed.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))  # add bot/ to path

from bot_lib.runtime import BotContext, BotConfig
from bot_lib.model_planner import (
    FakePlannerAdapter,
    ModelBackedPlanner,
    RawTask,
    PlannerOutput,
    BudgetConfig,
)

# 1. Build a context for team 1 in a 3-team game.
config = BotConfig(actions=["recon.health", "exploit.sqli"])
ctx = BotContext(
    config=config,
    num_teams=3,
    my_team=1,
    capabilities=frozenset({"network.attack", "network.submit"}),
)

# 2. Wire up a scripted fake adapter.
#    Each call() pops the next entry from the script.
#    An entry can be a PlannerOutput (success) or an exception class (failure).
script = [
    PlannerOutput(
        tasks=[
            RawTask(target_team=2, action_id="recon.health"),
            RawTask(target_team=3, action_id="exploit.sqli"),
        ],
        tokens_used=80,
        model_id="fake-v1",
    ),
]
adapter = FakePlannerAdapter(script=script)

# 3. Create the planner with a tight budget for demonstration.
budget = BudgetConfig(max_actions_per_round=5, max_plan_seconds=2.0)
planner = ModelBackedPlanner(adapter=adapter, budget=budget)

# 4. Run one round: iterate tasks and print them.
print("Targets:", planner.targets(ctx))
for task in planner.plan(ctx):
    print(f"  → team {task.target_team}: {task.action_id}")
```

Expected output:

```
Targets: [2, 3]
  → team 2: recon.health
  → team 3: exploit.sqli
```

To use a real LLM in production, replace steps 2-3 with:

```python
import os
from bot_lib.model_planner import RemoteModelPlannerAdapter, ModelBackedPlanner, BudgetConfig

adapter = RemoteModelPlannerAdapter(
    endpoint=os.environ["PLAN_ENDPOINT"],
    token=os.environ["PLAN_TOKEN"],
)
planner = ModelBackedPlanner(adapter=adapter)
```

Or simply set the env vars and call `load_planner("model")` from `planners.py`,
which calls `make_model_planner()` and reads all variables automatically.
