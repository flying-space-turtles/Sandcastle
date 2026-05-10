# Vulnerable Services

`services/example-vuln` is the bundled vulnerable service template. Running
`scripts/setup.sh` copies that template into each generated team directory:

```text
teams/generated/team<N>/service/
```

The top-level `docker-compose.yml` then builds every team service from its own
copy, and the matching SSH container bind-mounts that same copy at
`/home/team<N>/service`. You do not need `VULN_IMAGE` for the generated team
topology.

```bash
./scripts/setup.sh --teams 4
docker compose up --build
```

To add a different challenge, put its template under `services/`, update
the generated teams from that template, then regenerate the topology:

```bash
./scripts/setup.sh --teams 4 --template services/my-broken-service --overwrite-services
```

Omit `--overwrite-services` when you want to preserve existing team copies and
only create missing teams.
