# Sandcastle Docker Visualizer

React + React Flow module for rendering the Sandcastle Docker topology.

## Run

```bash
cd visualizer
npm install
npm run dev
```

The app loads the repository root `docker-compose.yml` by default. Use
`Yaml Mode` to paste or upload another Compose file, then render it into the
diagram.

## Bot Mode

Bot Mode talks to the local Sandcastle bot bridge:

```bash
python3 bot/bot_api.py
```

From there you can create a bot profile, choose actions and a planner, then
deploy that bot into selected team SSH containers. Deployed bots act on behalf
of their team from inside the CTF network.

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
