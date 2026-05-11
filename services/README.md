# Vulnerable Services

`services/example-vuln` is the bundled vulnerable service template. Running
`scripts/setup.sh` copies that template into each generated team directory:

```text
teams/generated/team<N>/example-vuln/
```

The top-level `docker-compose.yml` builds a small `team<N>-vuln` Linux machine
for each team and bind-mounts that team's copy at
`/home/team<N>/example-vuln`. From inside that vulnerable machine, teams run
`docker compose up -d --build` to start `team<N>-vuln-app`.

```bash
./scripts/setup.sh --teams 4
docker compose up --build
ssh -p 2201 team1@localhost
ssh team1@team1-vuln
cd ~/example-vuln
docker compose up -d --build
```

To add a different challenge, put its template under `services/`, update
the generated teams from that template, then regenerate the topology:

```bash
./scripts/setup.sh --teams 4 --template services/my-broken-service --overwrite-services
```

Omit `--overwrite-services` when you want to preserve existing team copies and
only create missing teams.
