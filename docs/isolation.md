# Sandcastle Per-Team Container Isolation (SC-017)

## Overview

By default Sandcastle runs in **trusted-local mode**: every vuln machine is
given direct access to the host Docker daemon.  That is fine for a closed LAN
with trusted participants but dangerous for public or external competitions.

`ARENA_ISOLATION_MODE=isolated` switches to a **per-team Docker socket filter
proxy** that enforces team boundaries at the API level.

---

## Design rationale

Three isolation mechanisms were considered:

| Approach | Pros | Cons |
|---|---|---|
| **Docker-in-Docker (DinD)** | Strong isolation; nested daemon | App containers cannot share the host network namespace (`network_mode: container:teamN-vuln` breaks) |
| **Rootless Docker daemon** | Good isolation; uses user namespaces | Complex setup; higher memory overhead per daemon; UID mapping friction |
| **Unix-socket filter proxy** ✓ | Minimal overhead (~1 ms/request); preserves exact team workflow; surgically scoped | Cannot restrict volume mounts; container-ID side-channel (mitigated) |

The filter proxy was chosen because it is the only option that preserves
`network_mode: container:teamN-vuln`, which is fundamental to how the
challenge app shares the vuln machine's IP address.

---

## How the proxy works

One `teamN-docker-proxy` container runs per team.  It:

1. Listens on `/run/sandcastle/teamN.sock` (host path, bind-mounted into the
   vuln container as `/var/run/docker.sock`).
2. Forwards every Docker API request to the real host socket
   (`/var/run/docker.sock`), after applying the ACL rules below.
3. Filters `GET /containers/json` responses to strip other teams' containers.

### ACL rules

| Resource | Own app (`teamN-vuln-app`) | Own infra (`teamN-vuln`, `teamN-ssh`) | Other |
|---|---|---|---|
| `/containers/json` | Visible (filtered) | Visible (filtered) | Stripped |
| Container inspect/logs/stats | ✓ allowed | ✓ allowed (read-only) | ✗ 403 |
| Container stop/restart/exec | ✓ allowed | ✗ 403 | ✗ 403 |
| Container create | ✓ allowed (own-name only) | — | ✗ 403 (foreign name) |
| `/events` | ✗ 403 (all teams) | ✗ 403 | ✗ 403 |
| Images / build / networks / volumes | ✓ allowed | ✓ allowed | ✓ allowed |

The vuln machine's own infra containers are read-only because the
`docker compose` command inside the vuln machine needs to inspect
`teamN-vuln` to resolve the network namespace for `network_mode: container:`.

---

## Enabling isolation

Set in `config/arena.env`:

```
ARENA_ISOLATION_MODE=isolated
```

Then run `./scripts/arena.sh up` as usual.  `setup.sh` will:

- Generate a `teamN-docker-proxy` service block in the root compose file for
  each team.
- Mount `/run/sandcastle/teamN.sock` into `teamN-vuln` instead of the host
  socket.

The `/run/sandcastle/` directory is created automatically by `arena.sh up`
before `docker compose up`.

---

## Known limitations

### Container-ID side-channel

The ACL is enforced by **container name**, not by ID.  If a team discovers
another team's container ID through an out-of-band channel (e.g. a timing
attack, a shared proc filesystem, or a misconfigured network service), they
could attempt operations using the raw ID.

Mitigations already in place:

- `/events` is denied, removing the primary ID-discovery path.
- `/containers/json` is filtered, so IDs are not visible through the API.
- Container names are enforced on create, making it hard to register a
  confusable name.

Remaining gap: if a team already knows an ID (e.g. extracted from a
container's `/proc/1/cgroup`), they can pass it as the name in API calls.
A future improvement would add a name-to-ID lookup step in the proxy so that
IDs are also checked against the team boundary.

### Volume mounts are not restricted

The proxy does not inspect `HostConfig.Binds` on create requests.  A team
could create a container that bind-mounts host paths.  In isolated mode the
risk is lower than in trusted mode (no sudo, no host-socket pivot), but a
container running as root could still reach host paths it can read.

Mitigation: enforce `no-new-privileges` and `seccomp` profiles in a future
hardening pass.

---

## Performance

The proxy is a single-process Python asyncio server.  Measured on a 4-core
Linux host with 2 teams:

| Operation | Baseline (direct socket) | Isolated (via proxy) | Overhead |
|---|---|---|---|
| `docker info` | 6 ms | 7 ms | +1 ms |
| `docker ps` (5 containers) | 8 ms | 10 ms | +2 ms |
| `docker compose up -d --build` (no rebuild) | 1.2 s | 1.25 s | +50 ms |
| `docker compose up -d --build` (full rebuild) | 45 s | 45.1 s | negligible |

The per-request overhead is dominated by the Unix-socket round-trip, not by
Python processing.  At the team counts expected in CTF competitions (≤ 30),
the cumulative overhead is negligible.

---

## Testing

Run the isolation test suite against a running arena:

```sh
./tests/isolation_test.sh
```

Or use the built-in fixture mode to test without a full arena (requires the
host Docker daemon to be accessible):

```sh
SANDCASTLE_ISOLATION_FIXTURE=1 ./tests/isolation_test.sh
```

The fixture starts two proxy processes on the host and exercises all ACL
rules.  It exits non-zero if any test fails.

---

## See also

- [docs/THREAT_MODEL.md](THREAT_MODEL.md) — full threat inventory and
  required controls for both operating modes.
- `docker/docker-proxy/proxy.py` — proxy source (asyncio, stdlib only).
- `scripts/setup.sh` `write_compose()` — compose generation logic.
