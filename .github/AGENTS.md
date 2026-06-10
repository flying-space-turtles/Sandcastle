# Sandcastle AI Agent Instructions

## Read First

Before making a non-trivial change, read:

1. [`../VISION.md`](../VISION.md)
2. [`../README.md`](../README.md)
3. The assigned task in
   [`../docs/PROJECT_AUDIT_AND_BACKLOG.md`](../docs/PROJECT_AUDIT_AND_BACKLOG.md)

The backlog contains dependencies, scope, acceptance criteria, and validation
expectations for agent-ready tasks.

## Current Product State

Sandcastle is an Attack & Defense CTF infrastructure and bot prototype. It is
not yet a complete competition platform.

Implemented:

- generated per-team SSH gateways and vulnerable Linux machines
- mutable per-team copies of a vulnerable Flask service
- scripted bot actions and planner interfaces
- a topology/bot visualizer
- a firewall/activity-feed prototype

Missing:

- gameserver, rounds, checkers, submissions, scoring, and scoreboard
- deterministic full-arena startup and health verification
- proven firewall enforcement on every supported host
- safe isolation for untrusted agents or participants

Do not describe the project as a complete arena until the MVP exit criteria in
`VISION.md` are met.

## Source Boundaries

- `scripts/setup.sh` is the source of truth for generated team directories and
  the root `docker-compose.yml`.
- Do not edit generated `docker-compose.yml` as the canonical implementation.
- Canonical service templates live under `services/`.
- Mutable generated copies live under
  `teams/generated/team<N>/example-vuln/` and are ignored by Git.
- Existing generated copies may contain participant patches. Do not overwrite
  them unless the task explicitly requires destructive reset behavior.

## Current Runtime Model

Each team has:

- `team<N>-ssh` at `10.10.<N>.2`, exposed on host port `2200 + N`
- `team<N>-vuln` at `10.10.<N>.3`
- `team<N>-vuln-app`, started through nested Compose from
  `team<N>-vuln`

Credentials are currently `team<N>` / `team<N>pass`.

Important capability boundary:

- `team<N>-vuln` has Docker CLI, the host Docker socket, and the mutable service
  source.
- `team<N>-ssh` has none of those by default.
- Bots currently run in `team<N>-ssh`, so offensive network actions work but
  Docker-based maintenance actions do not.

Do not silently mount the unrestricted Docker socket into gateways as a quick
fix. Follow SC-013 and the threat-model tasks.

## Known Runtime Risks

- `start.sh` starts infrastructure but not the vulnerable app containers.
- Existing app containers can retain a stale parent network namespace.
- Empty generated directories can be mistaken for complete workspaces.
- Old team containers can remain as orphans after topology changes.
- Firewall container health does not prove its redirect rule receives traffic.

When a task touches these areas, add a behavioral test rather than relying only
on Compose syntax or process health.

## Change Guidance

- Infrastructure lifecycle: edit `scripts/` and generation logic first.
- Team images: edit `docker/ssh/Dockerfile` or `docker/vuln/Dockerfile`.
- Challenge behavior: edit canonical source under `services/`.
- Bot behavior: preserve the typed action/planner boundary under `bot/bot_lib/`.
- Competition authority: keep flags, rounds, submissions, and scores in the
  gameserver/control plane, not in bots or the visualizer.
- Runtime UI: consume authoritative APIs for health and score state; Compose
  parsing is configuration visualization only.

## Verification Baseline

Run the focused tests for the change plus the relevant baseline:

```bash
bash -n scripts/*.sh bot/*.sh
python3 -B -m py_compile \
  scripts/gen_compose.py \
  bot/*.py bot/bot_lib/*.py \
  firewall/firewall.py \
  services/example-vuln/app/app.py
docker compose config --quiet
cd visualizer && npm run build
```

For lifecycle, networking, bot, checker, or scoring changes, add or run a
behavioral test. A healthy process is not sufficient evidence.

## Clarification Policy

Use `.github/skills/clarification-skill.md` when a requirement would force a
product, security, or compatibility decision that is not already made in
`VISION.md` or the assigned backlog task.
