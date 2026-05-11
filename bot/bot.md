# Sandcastle Bot

Three files. `deploy.sh` runs on the host; `bot.py` and `bot.sh` run inside a team's SSH container.

| File | Where | Role |
|------|-------|------|
| `deploy.sh` | Host | Copy bot files into containers, start/stop/status/logs |
| `bot.py` | `teamN-ssh` container | Python attack loops, watchdog |
| `bot.sh` | `teamN-ssh` container | Bash attacks, interactive menu |

The bot auto-detects its own team from the container hostname (`team2-ssh` → team 2) and attacks every other team's vuln service on `10.10.N.3:8080`.

---

## Quickstart

```bash
# From repo root — start the platform
./scripts/start.sh

# Deploy bots to teams 2, 3, 4 (team 1 stays human)
cd bot/
./deploy.sh 2 3 4

# Check they are running
./deploy.sh --status

# Watch live attack output for team 2
./deploy.sh --logs 2
```

---

## deploy.sh

```bash
./deploy.sh 2 3 4          # copy + start bot in those containers
./deploy.sh --copy-only 2  # copy files only, don't start
./deploy.sh --stop 2       # kill the bot in team2-ssh
./deploy.sh --status       # running/stopped for all teams
./deploy.sh --logs 2       # tail /tmp/bot.log from team2-ssh
```

**Env overrides:**

| Variable | Default | Description |
|----------|---------|-------------|
| `NUM_TEAMS` | `4` | Total teams |
| `LOOP_INTERVAL` | `60` | Seconds between attack rounds |
| `WATCHDOG` | `true` | Restart own vuln service if it goes down |

```bash
LOOP_INTERVAL=30 WATCHDOG=false ./deploy.sh 2 3 4
```

---

## One-off actions (from host)

```bash
# Ping sweep — see which vuln containers are reachable
docker exec team2-ssh python3 /tmp/bot.py --ping --teams 4

# Attack a single team right now
docker exec team2-ssh python3 /tmp/bot.py --attack-team 3

# Probe a team's flag-plant endpoint
docker exec team2-ssh python3 /tmp/bot.py --fake-flag 3
```

---

## Attack sequence

Each target is tried in order; stops at the first captured flag:

1. **Path traversal** — `GET /export?file=../flag.txt`
2. **Command injection** — `POST /admin/diagnostics` with `host=127.0.0.1; cat /app/data/flag.txt`
3. **SQL injection** — `POST /login` with `username=admin' --`, then read `/notes`

---

## Network layout

```
10.10.N.2  →  teamN-ssh   SSH container (host port 220N)
10.10.N.3  →  teamN-vuln  Flask app, port 8080 (internal only)
```

Human teams: `ssh -p 220N teamN@localhost` (password: `teamNpass`)
