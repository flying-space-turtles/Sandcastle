# Infrastructure Architecture

This repository models the container layout for a local Attack & Defense CTF
and includes a template vulnerable service that is copied into each generated
team directory. It does not include competition logic such as checkers,
scoreboards, or scoring.

## Topology

```text
ctf-network (bridge, 10.10.0.0/16)

  team1-ssh   10.10.1.2   host port 2201 -> 22
  team1-vuln  10.10.1.3   build: teams/team1/service

  team2-ssh   10.10.2.2   host port 2202 -> 22
  team2-vuln  10.10.2.3   build: teams/team2/service

  ...

  teamN-ssh   10.10.N.2   host port 2200+N -> 22
  teamN-vuln  10.10.N.3   image: ${VULN_IMAGE}
```

Docker Compose creates the shared bridge network and assigns deterministic IP
addresses so future checkers, gameservers, and teams can use stable targets.

## Generated Services

`scripts/setup.sh` is the source of truth for generated team directories and
`docker-compose.yml`. The generated file contains:

- one persistent `team<N>-data` volume per team
- one `team<N>-vuln` service per team built from `teams/team<N>/service`
- one `team<N>-ssh` service per team built from `teams/team<N>/ssh/Dockerfile`
- a mounted Docker socket in each SSH gateway for local orchestration

No gameserver or scoreboard is generated in this iteration.

## Vulnerable App Slot Contract

Future vulnerable app templates should be safe to copy per team. Compose passes
`TEAM_ID`, `TEAM_NAME`, `SERVICE_PORT`, and `SECRET_KEY`, and mounts `/app/data`
for team-specific persistence. The recommended service port is `8080`, but the
current infrastructure does not depend on any protocol beyond the bundled
template's health endpoint.

## Iteration Path

The next layers can be added independently:

- a sample vulnerable application under `services/`
- app-specific checker and flag planting contracts
- a gameserver API and persistence layer
- a scoreboard or operator dashboard

Keeping these layers separate makes the infrastructure reusable while the
challenge and scoring models are still changing.
