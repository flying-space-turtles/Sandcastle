# Sandcastle Operator Console

React operator UI for the live gameserver scoreboard, checker state, match
controls, configured Docker topology, traffic feed, and bot controls.

## Run

```bash
cd visualizer
npm install
npm run dev
```

The scoreboard is the default view and polls the authoritative gameserver API.
By default it uses same-origin browser routes. Vite proxies `/api/...` to the
gameserver, `/bot-api/...` to the bot controller, and `/firewall-ws` to the
firewall WebSocket during local development. The production nginx image proxies
the same paths to the corresponding Compose services or host-network listener.
Set `VITE_GAMESERVER_API_URL`, `VITE_BOT_API_URL`, or `VITE_FIREWALL_WS_URL` at
build time only when a deployment needs explicit browser-visible origins.
Operator actions require the token printed by:

```bash
./scripts/setup.sh --show-access
```

The topology view loads the repository root `docker-compose.yml`. It is
configuration metadata, not live container health. The old YAML editing and
raw inspector modes were removed to keep the console focused on arena
operations.

## Bot Deployments

The bot controller starts automatically with `./scripts/arena.sh up`. The Bots
view shows active and historical deployments, structured action events,
captures, submission outcomes, configuration, and archived raw logs.

Create a deployment, choose actions and a planner, then select the team SSH
containers where it should run. Captured flags are submitted automatically to
the authoritative gameserver using credentials injected inside the target
container; credentials are never returned to the browser.

Bot and firewall endpoints, team count, service port, and generated target IPs
come from the repository root `config/arena.env`.

## Data Model

The parser normalizes Compose metadata into React Flow nodes and edges:

- services become machine nodes with team, IP, environment, label, port, and
  Dockerfile metadata where available
- Compose networks become colored group nodes
- SSH containers and vulnerable app containers are laid out as sparse team
  pairs inside their network
- team SSH-to-vulnerable-app ownership edges stay visible by default
- cross-team attack paths, `depends_on`, and `links` are revealed on hover to
  keep the idle canvas uncluttered
