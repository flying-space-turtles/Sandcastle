# Vulnerable Services

`services/example-vuln` is the bundled vulnerable service template. Running
`scripts/setup.sh` copies that template into each generated team directory:

```text
teams/team<N>/service/
```

The top-level `docker-compose.yml` then builds every team service from its own
copy. You do not need `VULN_IMAGE` for the generated team topology.

```bash
./scripts/setup.sh --teams 4
docker compose up --build
```

To add a different challenge, put its template under `services/`, update
`scripts/setup.sh` to copy that template, then regenerate the topology.
