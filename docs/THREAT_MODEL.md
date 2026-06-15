# Sandcastle Threat Model

This document inventories every privileged resource in the Sandcastle
Attack &amp; Defense CTF infrastructure, defines the two operating modes with
their distinct trust guarantees, maps known escape paths, and lists the
controls required before moving from trusted-local development to an
untrusted competition with real participants.

Read this document before running a shared or exposed event.
See `docs/architecture.md` for topology details and
`docs/PROJECT_AUDIT_AND_BACKLOG.md` (SC-017, SC-018) for the planned isolation
work.

---

## Operating Modes

### Trusted-Local Mode (current)

All participants are members of the same organization and trust one
another. The organizer controls the host, the Docker daemon, and all
credentials. No participant has been given a secret they could use to
compromise the infrastructure beyond their own team slot.

**Guarantee:** organizer-level containment only. A determined participant
with SSH access to their team gateway *can* reach the host Docker daemon
through the Docker socket mounted into their vulnerable machine. This is a
deliberate design trade-off documented below.

**When it is acceptable:** on a private LAN with participants who are
trusted not to attack the infrastructure.

**When it is NOT acceptable:** any public event, CTF with external
teams, cloud-hosted arena, or scenario where a participant could benefit
from disrupting the infrastructure rather than winning on merit.

### Untrusted-Competition Mode

Each team's containers are isolated by a mechanism — rootless daemon,
Docker-in-Docker sidecar, or microVM — that prevents one team from
listing, stopping, exec-ing into, or patching another team's containers
or any infrastructure container. The Docker socket (or equivalent) is
scoped per-team and does not reach the host daemon. Resource limits
(SC-018) prevent one team's abuse from degrading others.

`ARENA_ISOLATION_MODE=dind` implements the strongest current Sandcastle
option: each team uses its own official `docker:dind` daemon. The lighter
`isolated` mode uses a per-team filter proxy in front of the host daemon and
is useful for guardrails, but it is not the same boundary as a separate daemon.
Run `./tests/dind_isolation_test.sh` on the target host before any untrusted
event.

---

## Privilege Inventory

### Docker Socket

| Container | Mount | Required for | Risk in trusted mode | Risk if untrusted |
|---|---|---|---|---|
| `teamN-vuln` in `trusted` | `/var/run/docker.sock` (rw) | Team rebuilds its app with `docker compose up -d --build` | Full host Docker daemon control = root-equivalent on the host | **CRITICAL** — any team can stop, delete, or exec into any other team's containers and all infrastructure |
| `teamN-vuln` in `isolated` | filtered Docker proxy socket | Same team workflow with API ACLs | Team operations are filtered by name but still reach the host daemon through organizer-controlled proxy code | Safer than trusted, but not a separate daemon boundary |
| `teamN-vuln` in `dind` | team-local DinD Unix socket | Team rebuilds its app in a nested daemon | No host Docker API in the vulnerable machine | Preferred current mode for untrusted participants |
| `bot-controller` | `/var/run/docker.sock` (rw) | Bot `WatchdogAction` restarts team containers | Organizer-controlled only; still a privileged mount | If participant-controlled, same severity as above |

**Rationale:** The Docker socket in `teamN-vuln` is the simplest way to
let teams patch and rebuild their vulnerable app from inside the container.
It is acceptable in trusted-local mode and must be replaced or scoped
before untrusted use.

**Required control for untrusted mode:** Use `ARENA_ISOLATION_MODE=dind` or a
future equivalent where the team's Docker API cannot reach other teams or
organizer infrastructure.

---

### Host Networking

| Container | Network mode | Required for | Risk |
|---|---|---|---|
| `sandcastle-firewall` | `network_mode: host` | Install `iptables` PREROUTING rule; bind transparent proxy on host port; raw socket ICMP/UDP capture | Full visibility into all host network interfaces; can install or modify any iptables rule; can listen on any host port |

**Rationale:** The transparent proxy approach requires the firewall to see
bridged CTF traffic at the host kernel level. There is no way to do this
without host networking or equivalent kernel access.

**Required control for untrusted mode:** The firewall is organizer
infrastructure and must not be reachable or controllable by participants.
The WebSocket feed (`WS_PORT`, default 6789) should be bound to a
non-participant interface or protected by authentication. The transparent
proxy port (`PROXY_PORT`, default 15000) should not be reachable from CTF
containers.

---

### Linux Capabilities

| Container | Capabilities | Required for | Risk |
|---|---|---|---|
| `teamN-ssh` | `NET_ADMIN` | Participants may use `iptables`, `ip`, or `tc` to patch their own network stack | Can modify routing and firewall rules inside the container network namespace; cannot escape to host if `network_mode` is bridge |
| `teamN-vuln` | `NET_ADMIN` | Same, plus Docker socket operations require socket group membership | Same as above |
| `sandcastle-firewall` | `NET_ADMIN`, `NET_RAW` | Install iptables rules; open raw sockets for packet capture | Required; firewall is organizer-controlled |

**Note:** `NET_ADMIN` in a container with a bridge network does not grant
access to the host network stack. The risk is scoped to the container's
network namespace unless combined with the Docker socket (which does allow
host escape).

**Required control for untrusted mode:** Audit whether participants need
`NET_ADMIN` in their containers. If not, remove it. If they do (e.g., to
simulate a realistic Linux box), ensure the Docker socket is absent so the
capability cannot be chained into a host escape.

---

### Bind Mounts

| Container | Mount | Direction | Required for | Risk |
|---|---|---|---|---|
| `teamN-vuln` | `./teams/generated/teamN/example-vuln` → `/home/teamN/example-vuln` | read-write | Teams edit and rebuild their challenge service source on the host filesystem | A participant can write arbitrary files into the host-side path; if Docker socket is also present, they can build and run arbitrary images |
| `gameserver` | `./config/arena.env` → `/app/config/arena.env` | read-only | Gameserver reads arena topology and scoring parameters | Read-only; no write path back to host |
| `bot-controller` | `./config/arena.env` → `/app/config/arena.env` | read-only | Bot controller reads arena topology | Same as above |

**Rationale:** The per-team writable mount is how teams modify their
service source. It enables the key competition mechanic (patching) and is
intentional in trusted-local mode.

**Required control for untrusted mode:** The writable bind mount to the
host is acceptable if the source tree is fully participant-owned and no
other host path is reachable from the same directory tree. Verify that
`../` traversal from inside the container cannot reach arena configuration
or other teams' source through the mount. The Docker socket must be absent
for this to be safe.

---

### Credentials

| Credential | Default value | Storage | Who needs it | Risk if leaked |
|---|---|---|---|---|
| SSH passwords | `team{N}pass` (from `ARENA_TEAM_PASSWORD_PATTERN`) | Baked into Docker image at build time as `TEAM_PASS` ARG | Team participants | Access to that team's SSH gateway and vulnerable machine |
| Team submission tokens | `sandcastle-team{N}-submission-token-change-me` | Plain text in `config/arena.env`; PBKDF2 hash stored in gameserver DB | Each team's bot or player | Allows submitting flags as that team; cannot affect other credentials |
| Operator token | `sandcastle-local-operator-token-change-me` | Plain text in `config/arena.env`; used directly in Bearer comparison | Arena organizer | Full match control: start, pause, finish, step, restart |
| Checker master secret | `sandcastle-local-checker-secret-change-me` | Plain text in `config/arena.env`; injected into gameserver as env var | Gameserver only; checkers derive per-team credentials from it | HMAC-SHA256 key; if known, attacker can derive all checker credentials and impersonate checkers for any team/service |
| Docker build ARGs | `TEAM_PASS`, `TEAM_USER`, `TEAM_UID`, `TEAM_NAME` | Docker image layer history | Anyone with Docker access on the host | SSH credentials retrievable from image history with `docker history --no-trunc` |

**Required action before any shared event:**

```bash
# Rotate all default credentials in config/arena.env
OPERATOR_TOKEN="$(openssl rand -hex 32)"
CHECKER_SECRET="$(openssl rand -hex 32)"
sed -i "s/^ARENA_OPERATOR_TOKEN=.*/ARENA_OPERATOR_TOKEN=${OPERATOR_TOKEN}/" config/arena.env
sed -i "s/^ARENA_CHECKER_SECRET=.*/ARENA_CHECKER_SECRET=${CHECKER_SECRET}/" config/arena.env
# Also replace ARENA_TEAM_TOKEN_PATTERN and ARENA_TEAM_PASSWORD_PATTERN with
# randomly generated per-team values before distributing them to teams.
```

**Required control for untrusted mode:** Credentials must be unique per
event, per team, and never committed to version control. The checker
master secret must not be visible to participants. Team passwords should
be distributed only to the team they belong to. Image build ARGs should
be removed from Docker history or the images rebuilt with a
`--no-cache` and ARG scrubbing approach.

---

### Exposed Ports

| Port | Bound address | Service | Authentication | Risk if exposed to participants |
|---|---|---|---|---|
| `2200+N` (e.g., 2201, 2202) | `127.0.0.1` by default via `ARENA_SSH_BIND_HOST` | SSH gateway for team N | Password (`TEAM_PASS`) | Password brute-force if deliberately rebound to a public interface; participant reaches their own team's gateway, which is expected |
| `8000` | `0.0.0.0` | Gameserver HTTP API | Bearer token (operator) or team submission token | Operator endpoints are token-protected; flag submission is team-scoped; unauthenticated read-only endpoints expose standings |
| `7878` | `127.0.0.1` | Bot controller HTTP API | None | Localhost-only; no authentication; anyone on the host can trigger bot actions or read bot state |
| `4173` | `127.0.0.1` by default via `ARENA_VISUALIZER_BIND_HOST` | Visualizer/operator console | Browser-held operator token for privileged actions | Bind only to trusted interfaces; use SSH forwarding for staging |
| `6789` | `0.0.0.0` (via host networking) | Firewall WebSocket event feed | None | Any host that can reach the port receives the full event stream including source IPs of all cross-team traffic |
| `15000` | `0.0.0.0` (via host networking) | Transparent proxy | N/A (transparent) | Participants who discover this port can send traffic that appears to come from the firewall host |

**Required control for untrusted mode:**
- Bind the gameserver to the internal CTF network or an operator-only
  interface, not `0.0.0.0`, unless public access is intended.
- Keep `ARENA_SSH_BIND_HOST=127.0.0.1` unless team SSH access is routed through
  a VPN, bastion, or other controlled ingress path.
- Add authentication or IP allowlist to the WebSocket event feed.
- The bot API must remain on `127.0.0.1` or be moved behind an operator
  network segment.
- The transparent proxy port should not be reachable from CTF containers.

---

### Agent and Bot Command Execution

The bot controller (`sandcastle-bot-controller`) runs inside Docker and
has access to the host Docker socket. It executes the following
potentially privileged operations:

| Action | Mechanism | Scope |
|---|---|---|
| `WatchdogAction.run` | `docker_post(/containers/{name}/restart)` via Unix socket to `/var/run/docker.sock` | Can restart any container by name; container name is constructed from `my_team` which comes from `detect_my_team()` reading the hostname |
| `ping_team` | `subprocess.run(["ping", "-c", "1", "-W", "2", ...])` | Launches a subprocess inside the bot-controller container |
| Flag submission | `urllib.request` HTTP to gameserver | Sends a Bearer token over the internal network |

**Risk:** The `my_team` field in `BotContext` is derived from the
container hostname. If a bot container's hostname is changed or
spoofed, `WatchdogAction` would attempt to restart a different team's
container. Because the bot controller currently has the unrestricted host
Docker socket, this could be used to restart any named container.

**Required control for untrusted mode:** Bot containers must not have
access to the host Docker socket. Defensive actions (watchdog, service
restart) must be routed through a team-local service-control API that
validates the requesting team identity and limits scope to that team's
containers (SC-013).

---

### Challenge Escape Risks

The TurtleNotes vulnerable application (`services/example-vuln`) contains
three **intentional** vulnerabilities:

| Vulnerability | Endpoint | Intended impact | Unintended escape risk |
|---|---|---|---|
| Path traversal | `GET /export?file=../flag.txt` | Read flag from app data directory | If the path traversal is not limited to the app container's filesystem, it could reach files on the vulnerable machine host (e.g., `/home/teamN/example-vuln/...`) via the bind-mounted source directory |
| Command injection | `POST /admin/diagnostics` with `host=...; cmd` | Execute arbitrary commands inside the app container | Commands run as the app user inside `teamN-vuln-app`; if the Docker socket is accessible from the app container, this becomes a full container escape |
| SQL injection | `POST /login` with `username=admin' --` | Bypass auth | Contained to the app's SQLite database |

**Current containment boundary:** `teamN-vuln-app` is a separate Docker
container that shares the network namespace of `teamN-vuln` but does not
inherit its volumes or capabilities by default. The path traversal is
bounded by the container's filesystem unless a bind mount crosses the
boundary.

**Verify:** Confirm the app container does not have the Docker socket
mounted (it should not, based on the current Compose definition). If the
app's Compose file is participant-editable (via the bind-mounted source),
a participant could add the Docker socket themselves, turning command
injection into a host escape.

**Required control for untrusted mode:** The app Compose template must
not be modifiable in ways that add privileged mounts. Validate the
generated `docker-compose.yml` at startup or use a policy that rejects
unknown volume mounts when starting the app.

---

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────┐
│  Host machine (organizer-controlled)                    │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Organizer infrastructure (trusted always)        │  │
│  │  sandcastle-firewall (host net, NET_ADMIN/RAW)    │  │
│  │  sandcastle-gameserver (CTF network, port 8000)   │  │
│  │  sandcastle-bot-controller (localhost:7878)       │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌─────────────────────┐  ┌─────────────────────────┐  │
│  │  Team 1 slot        │  │  Team 2 slot            │  │
│  │  team1-ssh          │  │  team2-ssh              │  │
│  │  (10.10.1.2, NET_ADMIN)│  │  (10.10.2.2, NET_ADMIN) │  │
│  │  team1-vuln         │  │  team2-vuln             │  │
│  │  (10.10.1.3, NET_ADMIN + Docker socket [!])      │  │
│  │  team1-vuln-app     │  │  team2-vuln-app         │  │
│  │  (shared net ns)    │  │  (shared net ns)        │  │
│  └─────────────────────┘  └─────────────────────────┘  │
│                                                         │
│  [!] In trusted-local mode, the Docker socket in        │
│  teamN-vuln crosses the boundary into organizer space.  │
└─────────────────────────────────────────────────────────┘
```

**Boundary crossings in trusted-local mode:**
- `teamN-vuln` → host Docker daemon (via socket): intentional, accepted
- `teamN-vuln` → host filesystem under `teams/generated/teamN/` (via bind mount): intentional, scoped
- `sandcastle-firewall` → host network stack (via host networking): intentional, organizer-controlled

**Boundary crossings that must NOT exist in untrusted mode:**
- Any team container → host Docker daemon
- Team N container → Team M container (no cross-team control)
- Team container → organizer infrastructure containers

---

## Required Controls Per Mode

### Trusted-Local Mode Controls (current state)

| Control | Status | Notes |
|---|---|---|
| Rotate operator token before sharing arena | **Required** | Default value is public; see README |
| Rotate checker master secret before sharing arena | **Required** | Default value is public |
| Rotate team submission tokens | **Required** | Default values are public |
| Bind bot API to localhost only | Done | `127.0.0.1:7878` |
| Firewall startup verification | Done | Fails if `bridge-nf-call-iptables` is inactive |
| Network smoke test after startup | Done | Verifies redirect counter increments |
| Doctor script warns about Docker socket | Done | `WARN` level with remediation link |
| Startup banner identifies trusted-local mode | Done | `arena.sh up` prints security notice |
| Participants informed of mode limitations | Required | Share this document and README security section |

### Untrusted-Competition Mode Controls

| Control | Status | Blocking? |
|---|---|---|
| Replace shared Docker socket with team-scoped API | Implemented with `dind`; proxy available as `isolated` | Verify on target host |
| Per-team container isolation (rootless/DinD/microVM) | Implemented with DinD | Verify with `./tests/dind_isolation_test.sh` |
| CPU, memory, process, and disk limits per team | Not implemented | **Blocking** |
| Unique credentials per event, never committed | Not implemented | **Blocking** |
| Gameserver bound to operator-only interface | Not implemented | High |
| WebSocket event feed authentication or allowlist | Not implemented | High |
| App Compose template validation at startup | Not implemented | High |
| Docker image build ARG scrubbing | Not implemented | Medium |
| Rate limits on SSH authentication | Not implemented | Medium |
| Audit log for all operator API actions | Not implemented | Medium |

---

## Known Escape Paths

The following are documented escape paths from a participant in
trusted-local mode. These are **accepted risks** in that mode and
**blocking issues** for any untrusted deployment.

### 1. Docker Socket → Host Root

A participant who gains a shell inside `teamN-vuln` (via SSH or
exploitation of the vulnerable app) can use the mounted Docker socket to:

```bash
docker run --rm -v /:/host --privileged alpine chroot /host
```

This gives root on the host. The Docker socket is at
`/var/run/docker.sock` and the team user is added to the `docker` group
by the container entrypoint.

**Mitigation in trusted mode:** Organizer trust, documented limitation.
**Mitigation for untrusted mode:** Remove the Docker socket; use a
team-scoped build API (SC-013, SC-017).

### 2. Command Injection → Docker Socket (app container)

If a participant adds a Docker socket mount to the app's
`docker-compose.yml` (which is editable via the bind-mounted source), the
command injection vulnerability in `POST /admin/diagnostics` becomes a
host escape path.

**Mitigation in trusted mode:** Organizer trust.
**Mitigation for untrusted mode:** Validate app Compose files before
startup; reject unknown volume mounts.

### 3. Cross-Team Container Control via Bot Socket

The bot controller has the host Docker socket. If the bot controller's
HTTP API (`127.0.0.1:7878`) were reachable by participants (e.g., via an
SSRF in a challenge), they could trigger actions that affect other teams.

**Current exposure:** None — the port is bound to `127.0.0.1` only.
**Mitigation for untrusted mode:** Keep the bot API inaccessible to
participants; verify no challenge SSRF can reach `127.0.0.1:7878`.

### 4. Firewall Port Impersonation

The transparent proxy listens on `0.0.0.0:15000`. A participant who can
reach that port directly (e.g., by SSH port-forwarding from their gateway)
can send traffic that the proxy forwards to the destination with the
firewall host's IP as the masked source.

**Current exposure:** Limited — participants must know the port and find a
path to it. The CTF network PREROUTING rule redirects only intra-CTF TCP
traffic, not arbitrary host traffic.
**Mitigation for untrusted mode:** Bind proxy port to a non-participant
interface, or add a source IP allowlist.

### 5. Operator Token Brute-Force

The operator token is compared with a simple `hmac.compare_digest` call
(constant-time, good) but there is no rate limiting on the operator
endpoints. If the default token is not rotated, it is trivially known.

**Current exposure:** Default token is committed to the repository.
**Mitigation:** Always rotate before use (README Step 1). For untrusted
mode, additionally rate-limit operator endpoint authentication failures.

---

## Startup Gate

`scripts/arena.sh up` prints a security notice before starting the arena.
The notice identifies the current mode and states the limitation.
`scripts/doctor.sh` emits a `WARN` for the Docker socket in running
vulnerable machines and a `WARN` for the default operator token, each
with a link to this document.

If `SANDCASTLE_SKIP_TRUSTED_BANNER=1` is set, the startup banner is
suppressed (useful in CI). This variable does not disable the doctor
`WARN` entries.

---

## Document Maintenance

Update this document whenever a new privileged mount, capability, exposed
port, or credential is added. Every entry in the privilege inventory must
have an owner and a rationale. Run `./scripts/doctor.sh` after topology
changes to verify warnings remain accurate.

Related tasks: SC-013 (bot capability split), SC-017 (isolation mode),
SC-018 (resource limits).
