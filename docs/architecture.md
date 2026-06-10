# Infrastructure Architecture

This repository models the container layout for a local Attack & Defense CTF
and includes a template vulnerable service that is copied into each generated
team directory. It does not yet include competition logic such as a gameserver,
checkers, submissions, scoreboards, or scoring. See `../VISION.md` for the
target product and `PROJECT_AUDIT_AND_BACKLOG.md` for the implementation plan.

## Topology

The topology values below show the committed defaults. Canonical values live
in `../config/arena.env`; `scripts/setup.sh` validates them and regenerates the
root and per-team Compose files.

```text
ctf-network (bridge, 10.10.0.0/16)

  team1-ssh   10.10.1.2   host port 2201 -> 22
  team1-vuln  10.10.1.3   vulnerable Linux machine
  team1-vuln-app
               10.10.1.3   shares team1-vuln networking

  team2-ssh   10.10.2.2   host port 2202 -> 22
  team2-vuln  10.10.2.3   vulnerable Linux machine
  team2-vuln-app
               10.10.2.3   shares team2-vuln networking

  ...

  teamN-ssh   10.10.N.2   host port 2200+N -> 22
  teamN-vuln  10.10.N.3   vulnerable Linux machine
  teamN-vuln-app
               10.10.N.3   shares teamN-vuln networking

  sandcastle-firewall
               host net     masks team-to-team TCP source IPs
```

Docker Compose creates the shared bridge network and assigns deterministic IP
addresses so future checkers, gameservers, and teams can use stable targets.

The firewall prototype intends to redirect team-to-team TCP traffic through a
host transparent proxy. This depends on bridge traffic traversing host
`iptables` PREROUTING. The current implementation does not fail when that host
capability is absent, so destination source masking and activity events must be
verified from the redirect rule packet counter. Making this deterministic is
tracked as SC-004.

## Generated Services

`config/arena.env` is the source of truth for arena values.
`scripts/setup.sh` owns how those values become generated team directories and
`docker-compose.yml`. The generated file contains:

- one `team<N>-vuln` machine per team built from `docker/vuln/Dockerfile`
- one `team<N>-ssh` service per team built from `docker/ssh/Dockerfile`
- one bind mount from `teams/generated/team<N>/example-vuln` to
  `/home/team<N>/example-vuln` in the vulnerable machine
- a mounted Docker socket in each vulnerable machine for local app orchestration
- one `firewall` service built from `firewall/Dockerfile`

No gameserver or scoreboard is generated in this iteration.

The generated root Compose defines SSH gateways, vulnerable machines, and the
firewall. Each `team<N>-vuln-app` remains a nested Compose project so teams can
rebuild their own patched source.

`scripts/arena.sh up` is the organizer lifecycle. It starts the root project,
waits for parent machines, removes any old app containers that reference stale
parent container IDs, starts every nested app project with forced recreation,
and waits for active `/health` checks.

## Vulnerable App Slot Contract

Future vulnerable app templates should be safe to copy per team. Setup copies
the selected template into ignored generated workspaces, so teams can SSH from
`team<N>-ssh` into `team<N>-vuln`, patch their own source, and run
`docker compose up -d --build` without changing another team's source. The app
container is named `team<N>-vuln-app`, shares the `team<N>-vuln` network
namespace, gets `TEAM_ID`, `TEAM_NAME`, `SERVICE_PORT`, and `SECRET_KEY`, uses
`sandcastle_team<N>-data` for `/app/data`, and is reachable at
the configured team service IP and port.

Marked generated workspaces are repairable and preserve existing files.
Unmarked directories are participant-owned and are rejected unless the
operator explicitly selects destructive overwrite. Reducing team count also
requires explicit handling of stale higher-numbered containers.

`arena.sh down` removes containers but preserves source and named app data.
`arena.sh restart` applies the same preservation before startup.
`arena.sh reset` additionally removes `sandcastle_team<N>-data` volumes while
preserving the generated source tree.

## Iteration Path

The next layers can be added independently:

- a sample vulnerable application under `services/`
- app-specific checker and flag planting contracts
- a gameserver API and persistence layer
- a scoreboard or operator dashboard

Keeping these layers separate makes the infrastructure reusable while the
challenge and scoring models are still changing.
