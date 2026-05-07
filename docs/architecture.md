# Architecture

Detailed companion to the top-level `README.md`. Describes the data flow,
container topology, and the contract between gameserver and dashboard.

## Container topology

```
┌─────────────────────────────────────────────────────────────────────┐
│  ctf-network  (bridge, 10.10.0.0/16)                                │
│                                                                     │
│   ┌─────────────────┐                                               │
│   │   gameserver    │  10.10.0.2  :8080  (host :8080)               │
│   │   FastAPI       │                                               │
│   │   tick engine   │   ←── docker.sock (RO mount, host)            │
│   └────────┬────────┘                                               │
│            │ http (planter, SLA checker)                            │
│   ┌────────┴───────────────────────────────────────────────┐        │
│   │                                                         │        │
│   ▼                                                         ▼        │
│  team1-vuln :8080  10.10.1.3        team1-ssh :22 (host :2201)      │
│   Flask "notes" app                  Ubuntu + sshd + docker CLI     │
│   IDOR: GET /api/notes               docker.sock RO mount           │
│                                                                     │
│   …repeat for team2, team3, …                                       │
└─────────────────────────────────────────────────────────────────────┘

         ↑                                       ↑
   curl http://localhost:8080                ssh -p 2201 ctfuser@localhost
   (operator + dashboard)                    (participants)
```

## Tick lifecycle (one round)

```
┌─────────────────────────────────────────────────────────────────────┐
│ tick T = T-1 + TICK_DURATION                                        │
│                                                                     │
│  1. round++                                                         │
│  2. for each team in parallel:                                      │
│       FLAG = generate_flag()  (= "FLAG{" + 32 hex chars + "}")     │
│       register bot user on team-vuln                                │
│       POST /api/notes  { title:"round N secret", content: FLAG }    │
│       store flag in gameserver.sqlite (team_id, round, note_id)     │
│  3. for each team in parallel:                                      │
│       GET  /health                  → reachability                  │
│       POST /api/notes (canary)                                      │
│       GET  /api/note/<canary id>    → integrity                     │
│       GET  /api/note/<flag id>      → flag still present?           │
│       => SLAResult { UP | MUMBLE | CORRUPT | DOWN }                 │
│  4. expire any flag with round + FLAG_EXPIRY_ROUNDS <= current      │
│  5. compute per-team per-round score                                │
│  6. emit tick.end                                                   │
└─────────────────────────────────────────────────────────────────────┘
```

### Scoring per round

```
attack_pts  = number of distinct (submitter, flag) attack-submissions credited to this team this round
defense_pts = 1  if no attacker stole this team's flag this round
              0  otherwise
sla_pts     = 1 if SLAResult == UP else 0
sla_mult    = { UP: 1.0, MUMBLE: 0.5, CORRUPT: 0.5, DOWN: 0.0 }
total       = (attack_pts + defense_pts) * sla_mult
```

The cumulative scoreboard (`/api/scoreboard`) is the per-team SUM over all
rounds.

## Database (SQLite)

Persisted at `/app/data/gameserver.sqlite` inside the gameserver container
(volume `gameserver-data` in compose).

| Table          | Purpose                                                    |
|----------------|------------------------------------------------------------|
| `teams`        | id, name, ip, submission_token                             |
| `flags`        | id, team_id, round, value, note_id, planted_at, expired    |
| `submissions`  | id, submitter_team_id, flag_id, round, created_at          |
| `sla_checks`   | id, team_id, round, status, detail, created_at             |
| `scores`       | team_id, round, attack, defense, sla, total                |
| `events`       | id, kind, team_id?, round?, message, created_at            |
| `state`        | k/v: current round, paused flag, last_tick_at              |

## Dashboard ↔ gameserver contract

The dashboard polls `GET /api/state` every 2 s. The response is the union of
several projections so the UI can render in one render pass:

```jsonc
{
  "config": { "num_teams": 3, "tick_duration": 30, "flag_expiry_rounds": 5 },
  "round": 42,
  "paused": false,
  "last_tick_at": 1700000000.123,
  "now": 1700000020.456,
  "docker": { "available": true, "detail": "ok" },
  "teams": [
    {
      "id": 1, "name": "Team 1",
      "ip_address": "10.10.1.3",
      "service_url": "http://10.10.1.3:8080",
      "container": { "ssh": "team1-ssh", "vuln": "team1-vuln", "vuln_state": "running" },
      "latest_flag_round": 42,
      "sla_status": "UP",
      "sla_detail": "all checks passed",
      "submission_token": "<hex>"
    },
    …
  ],
  "scoreboard": [
    { "team_id": 1, "name": "Team 1", "attack": 0, "defense": 42, "sla": 42, "total": 42 },
    …
  ],
  "events": [ { "id": 1234, "kind": "tick.end", "team_id": null, "round": 42, "message": "Round 42 complete", "created_at": 1700000020.0 }, … ]
}
```

The dashboard never writes to the gameserver outside of these endpoints:

- `POST /api/admin/tick`
- `POST /api/admin/pause` | `POST /api/admin/resume`
- `POST /api/admin/team/{id}/{down|up|restart}`
- `POST /api/submit`

Vite's dev server proxies `/api/*` to `http://localhost:8080` so the
browser can call them without CORS configuration.

## Why polling?

The state size is tiny (~3–5 KB for 3 teams) and the natural cadence is one
tick (30 s). A 2 s poll interval gives sub-tick UI freshness without the
operational complexity of SSE/websockets. The UI also flashes the edges
between gameserver and team-vuln nodes whenever it observes the round
counter advance, so ticks are visually obvious despite polling.

## Trust boundary

The simulation runs entirely on a single host. Trust is therefore *advisory*
— the SSH boxes have a known weak password and the docker socket is
deliberately reachable from both the gameserver and the team SSH gateways.
Do not expose any of these ports publicly.
