# Sandcastle Per-Team Container Isolation (SC-017)

## Overview

By default Sandcastle runs in **trusted-local mode**: every vuln machine is
given direct access to the host Docker daemon.  That is fine for a closed LAN
with trusted participants but dangerous for public or external competitions.

Sandcastle now supports three Docker access modes:

| Mode | Docker access | Intended use |
|---|---|---|
| `trusted` | Direct host Docker socket in each vulnerable machine | Fast local development with trusted participants |
| `isolated` | Per-team filter proxy in front of the host Docker daemon | Lightweight guardrails while preserving the original networking model |
| `dind` | One `docker:dind` daemon per team | Production-like tests with untrusted participants |

---

## Design rationale

Three isolation mechanisms were considered:

| Approach | Pros | Cons |
|---|---|---|
| **Docker-in-Docker (DinD)** | Strong daemon isolation; teams cannot query one another's containers | Higher memory/disk use; privileged sidecars; requires service-port forwarding |
| **Rootless Docker daemon** | Good isolation; uses user namespaces | Complex setup; higher memory overhead per daemon; UID mapping friction |
| **Unix-socket filter proxy** ✓ | Minimal overhead (~1 ms/request); preserves exact team workflow; surgically scoped | Cannot restrict volume mounts; container-ID side-channel (mitigated) |

The filter proxy remains useful because it preserves
`network_mode: container:teamN-vuln`, which is the simplest way for the
challenge app to share the vuln machine's IP address. DinD uses a different
shape: the team-local DinD sidecar shares `teamN-vuln`'s network namespace, and
the app runs inside the team daemon on that shared host network. The service is
therefore available directly at `10.10.N.3:SERVICE_PORT` without a published
port or TCP forwarder.

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

For lightweight filtered-host isolation, set in `config/arena.env`:

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

For Docker-in-Docker production testing, run:

```sh
./scripts/setup.sh --dind
./scripts/arena.sh up
```

`setup.sh --dind` persists `ARENA_ISOLATION_MODE=dind`, generates one
`teamN-dind` service per team from the official `docker:27-dind` image, and
mounts that daemon's Unix socket into `teamN-vuln` as the team's only Docker
API. The sidecar shares the vulnerable machine's network namespace so nested
apps bind directly to the team's service IP. The host Docker socket is not
mounted into the vulnerable machine.

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

### Volume mounts are not restricted in `isolated`

The proxy does not inspect `HostConfig.Binds` on create requests.  A team
could create a container that bind-mounts host paths.  In isolated mode the
risk is lower than in trusted mode (no sudo, no host-socket pivot), but a
container running as root could still reach host paths it can read.

Mitigation: enforce `no-new-privileges` and `seccomp` profiles in a future
hardening pass.

### DinD resource and privilege cost

DinD gives each team its own daemon namespace, which is stronger than API
filtering but more expensive. Each `teamN-dind` sidecar is privileged and keeps
its own `/var/lib/docker` volume, so cold builds use more disk, memory, and
startup time. Use it for production-like staging and untrusted events, not for
the fastest local edit loop.

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

Run the DinD runtime checks against a running DinD arena:

```sh
./scripts/setup.sh --dind --teams 2
./scripts/arena.sh up
./tests/dind_isolation_test.sh
```

For a disposable cloud VM or self-hosted CI runner, use:

```sh
./scripts/staging-dind-smoke.sh
```

---

## See also

- [docs/THREAT_MODEL.md](THREAT_MODEL.md) — full threat inventory and
  required controls for both operating modes.
- `docker/docker-proxy/proxy.py` — proxy source (asyncio, stdlib only).
- `scripts/setup.sh` `write_compose()` — compose generation logic.
