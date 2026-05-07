# Infrastructure Architecture

This repository is currently an infrastructure-only scaffold. It models the
container layout for a local Attack & Defense CTF without bundling the
vulnerable services or competition logic.

## Topology

```text
ctf-network (bridge, 10.10.0.0/16)

  team1-ssh   10.10.1.2   host port 2201 -> 22
  team1-vuln  10.10.1.3   image: ${VULN_IMAGE}

  team2-ssh   10.10.2.2   host port 2202 -> 22
  team2-vuln  10.10.2.3   image: ${VULN_IMAGE}

  ...

  teamN-ssh   10.10.N.2   host port 2200+N -> 22
  teamN-vuln  10.10.N.3   image: ${VULN_IMAGE}
```

Docker Compose creates the shared bridge network and assigns deterministic IP
addresses so future checkers, gameservers, and teams can use stable targets.

## Generated Services

`scripts/gen_compose.py` is the source of truth for `docker-compose.yml`.
The generated file contains:

- one persistent `team<N>-data` volume per team
- one `team<N>-vuln` service per team using the externally supplied
  `VULN_IMAGE`
- one `team<N>-ssh` service per team built from `teams/ssh/Dockerfile`
- a mounted Docker socket in each SSH gateway for local orchestration

No gameserver or scoreboard is generated in this iteration.

## Vulnerable App Slot Contract

Future vulnerable app images should be safe to reuse across all teams. Compose
passes `TEAM_ID`, `TEAM_NAME`, and `SERVICE_PORT`, and mounts `/app/data` for
team-specific persistence. The recommended service port is `8080`, but the
current infrastructure does not depend on any protocol.

## Iteration Path

The next layers can be added independently:

- a sample vulnerable application under `services/`
- app-specific checker and flag planting contracts
- a gameserver API and persistence layer
- a scoreboard or operator dashboard

Keeping these layers separate makes the infrastructure reusable while the
challenge and scoring models are still changing.
