# Local Attack & Defense CTF Simulation

## Architecture for Running a Full A&D CTF on a Single Machine

This document provides a comprehensive blueprint for simulating an Attack & Defense (A&D) Capture The Flag competition entirely on a single computer. It covers network topology, machine simulation, vulnerable service management, flag lifecycle, scoring, and operational procedures.

> Current repository note: this document is a broad architecture blueprint. The
> implemented scaffold keeps canonical vulnerable service templates under
> `services/`, generates ignored per-team working copies under
> `teams/generated/team<N>/service`, builds `team<N>-vuln` from that same
> working copy, and uses one reusable SSH gateway image at
> `docker/ssh/Dockerfile`. Older examples below that mention committed
> `teams/team<N>` folders or a copied per-team SSH Dockerfile are conceptual,
> not the current repo model.

---

## Table of Contents

1. [Background: How a Real A&D CTF Works](#1-background-how-a-real-ad-ctf-works)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Network Simulation](#3-network-simulation)
4. [Machine Simulation (Team Containers)](#4-machine-simulation-team-containers)
5. [Vulnerable Application Management](#5-vulnerable-application-management)
6. [Flag Collection & Submission](#6-flag-collection--submission)
7. [Gameserver Design](#7-gameserver-design)
8. [Scoreboard & SLA Monitoring](#8-scoreboard--sla-monitoring)
9. [Directory Layout](#9-directory-layout)
10. [Orchestration & Startup](#10-orchestration--startup)
11. [Operational Runbook](#11-operational-runbook)
12. [Security Considerations](#12-security-considerations)
13. [Scaling & Performance](#13-scaling--performance)
14. [Existing Open-Source Frameworks](#14-existing-open-source-frameworks)
15. [Summary Table](#15-summary-table)

---

## 1. Background: How a Real A&D CTF Works

In a standard Attack & Defense CTF:

- **Teams** each receive an identical Linux machine (usually a VM) running one or more vulnerable services.
- All machines are connected via a **shared network** (typically a VPN such as WireGuard or OpenVPN).
- A central **gameserver** periodically plants **flags** (unique tokens) inside each team's services.
- Teams must simultaneously:
  - **Attack** other teams by exploiting vulnerabilities in their services to steal flags.
  - **Defend** their own services by patching vulnerabilities without breaking functionality.
- Stolen flags are **submitted** to the gameserver for points.
- The gameserver runs **SLA (Service Level Agreement) checks** to verify each team's services are still functional. If a service is down or broken, the team loses defense points.
- A **scoreboard** displays real-time standings.

The challenge of this proposal is to replicate all of the above on a single physical/virtual machine.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        HOST MACHINE                             │
│                                                                 │
│  ┌──────────────────── ctf-network (10.10.0.0/16) ───────────┐ │
│  │                                                             │ │
│  │  ┌─────────────┐  ┌─────────────┐      ┌─────────────┐    │ │
│  │  │ Gameserver  │  │   Team 1    │      │   Team N    │    │ │
│  │  │ 10.10.0.2   │  │ 10.10.1.0/24│ ... │ 10.10.N.0/24│    │ │
│  │  │             │  │             │      │             │    │ │
│  │  │ - Flag API  │  │ ┌─────────┐ │      │ ┌─────────┐ │    │ │
│  │  │ - Checker   │  │ │ SSH GW  │ │      │ │ SSH GW  │ │    │ │
│  │  │ - Scoreboard│  │ │ .1.2    │ │      │ │ .N.2    │ │    │ │
│  │  │ - Flag Plant│  │ └────┬────┘ │      │ └────┬────┘ │    │ │
│  │  └─────────────┘  │      │      │      │      │      │    │ │
│  │                    │ ┌────┴────┐ │      │ ┌────┴────┐ │    │ │
│  │                    │ │ Vuln    │ │      │ │ Vuln    │ │    │ │
│  │                    │ │ Service │ │      │ │ Service │ │    │ │
│  │                    │ │ .1.3    │ │      │ │ .N.3    │ │    │ │
│  │                    │ └─────────┘ │      │ └─────────┘ │    │ │
│  │                    └─────────────┘      └─────────────┘    │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  Host ports: 2201→Team1:22, 2202→Team2:22, 8080→Gameserver:80  │
└─────────────────────────────────────────────────────────────────┘
```

**Key principle:** Every team and the gameserver live in Docker containers on a shared Docker network. The Docker network replaces the VPN. Docker Compose orchestrates everything.

---

## 3. Network Simulation

### 3.1 Replacing the VPN with a Docker Bridge Network

In a real A&D CTF, all team machines are connected via a VPN (WireGuard, OpenVPN, etc.) so they can reach each other. On a single machine, a **Docker user-defined bridge network** provides the same functionality:

```bash
docker network create \
  --driver bridge \
  --subnet 10.10.0.0/16 \
  --gateway 10.10.0.1 \
  ctf-network
```

**Properties:**
- All containers attached to `ctf-network` can communicate with each other by IP.
- Containers are isolated from the host's network by default (just like a VPN isolates CTF traffic).
- You assign deterministic IPs to each container so teams know where to find each other (mimicking the IP allocation a real gameserver provides).

### 3.2 IP Addressing Scheme

| Entity | Subnet | IP Examples |
|---|---|---|
| Gameserver | `10.10.0.0/24` | `10.10.0.2` (gameserver), `10.10.0.3` (scoreboard) |
| Team 1 | `10.10.1.0/24` | `10.10.1.2` (SSH), `10.10.1.3` (vuln service) |
| Team 2 | `10.10.2.0/24` | `10.10.2.2` (SSH), `10.10.2.3` (vuln service) |
| Team N | `10.10.N.0/24` | `10.10.N.2` (SSH), `10.10.N.3` (vuln service) |

### 3.3 Simulating Network Conditions (Optional)

For added realism, you can simulate real-world network conditions inside containers using `tc` (traffic control):

```bash
# Inside a container: add 10ms latency and 1% packet loss
tc qdisc add dev eth0 root netem delay 10ms loss 1%
```

This requires the `NET_ADMIN` capability on the container:

```yaml
cap_add:
  - NET_ADMIN
```

### 3.4 Network Monitoring

To monitor traffic between teams (useful for organizers), run `tcpdump` on the Docker network:

```bash
# On the host
docker run --rm --net=ctf-network \
  nicolaka/netshoot tcpdump -i eth0 -w /captures/traffic.pcap
```

### 3.5 DNS (Optional)

For convenience, you can run a lightweight DNS server (e.g., `dnsmasq`) on the gameserver container to provide names like `team1.ctf`, `team2.ctf`, etc.

---

## 4. Machine Simulation (Team Containers)

### 4.1 What Each Team Gets

In a real A&D CTF, each team receives a Linux VM with:
- SSH access
- One or more vulnerable services running
- Ability to modify, restart, and manage those services

We replicate this with **two containers per team**:

1. **SSH Gateway Container** — An Ubuntu/Debian container running `openssh-server`. This is the team's "machine." They SSH into this container to inspect, patch, and manage their vulnerable service.

2. **Vulnerable Service Container(s)** — One or more containers running the actual challenge application (web app, binary service, etc.). Managed via Docker Compose from within the SSH container.

### 4.2 SSH Gateway Dockerfile

```dockerfile
FROM ubuntu:22.04

# Install essentials
RUN apt-get update && apt-get install -y \
    openssh-server \
    sudo \
    curl \
    wget \
    vim \
    nano \
    net-tools \
    iputils-ping \
    nmap \
    python3 \
    python3-pip \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI (to manage vuln service)
RUN curl -fsSL https://get.docker.com | sh

# Install Docker Compose plugin
RUN apt-get update && apt-get install -y docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Create team user
ARG TEAM_USER=ctfuser
ARG TEAM_PASS=changeme
RUN useradd -m -s /bin/bash ${TEAM_USER} \
    && echo "${TEAM_USER}:${TEAM_PASS}" | chpasswd \
    && usermod -aG sudo ${TEAM_USER} \
    && usermod -aG docker ${TEAM_USER}

# SSH config
RUN mkdir -p /run/sshd
RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
RUN sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

# Copy the vulnerable service source into the team's home directory
COPY vuln-service/ /home/${TEAM_USER}/service/
RUN chown -R ${TEAM_USER}:${TEAM_USER} /home/${TEAM_USER}/service

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D"]
```

### 4.3 Docker-in-Docker vs. Docker Socket Mount

The SSH container needs to run Docker commands to manage the vulnerable service. Two approaches:

**Option A: Docker Socket Mount (Recommended)**

Mount the host's Docker socket into the SSH container:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

- Pros: Simple, lightweight, no nested Docker daemon.
- Cons: All teams share the host Docker daemon — a malicious team could interfere with others. Mitigated by namespacing container names per team and using Docker authorization plugins.

**Option B: Docker-in-Docker (DinD)**

Run a full Docker daemon inside each SSH container:

```yaml
privileged: true
```

Or use the official `docker:dind` image as a sidecar.

- Pros: Full isolation — each team has their own Docker daemon.
- Cons: Higher resource usage, requires `privileged` mode.

**Recommendation:** For a trusted local simulation (e.g., training, practice), Option A is simpler. For untrusted participants, use Option B.

### 4.4 SSH Access from the Host

Map each team's SSH port to a unique host port:

```yaml
# Team 1
ports:
  - "2201:22"  # ssh -p 2201 ctfuser@localhost

# Team 2
ports:
  - "2202:22"  # ssh -p 2202 ctfuser@localhost
```

### 4.5 Resource Limits

Prevent a single team from hogging the host's resources:

```yaml
deploy:
  resources:
    limits:
      cpus: "2.0"
      memory: 2G
    reservations:
      cpus: "0.5"
      memory: 512M
```

---

## 5. Vulnerable Application Management

### 5.1 The Vulnerable Service

Each team's vulnerable service is a Docker Compose project located at `/home/ctfuser/service/` inside their SSH container. Example structure:

```
/home/ctfuser/service/
├── docker-compose.yml
├── Dockerfile
├── src/
│   ├── app.py            # The vulnerable application
│   ├── requirements.txt
│   └── templates/
│       └── index.html
├── data/
│   └── flag.txt          # Current flag (planted by gameserver)
└── README.md             # Service documentation
```

Example `docker-compose.yml`:

```yaml
version: "3.8"
services:
  webapp:
    build: .
    container_name: team1-vuln
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    networks:
      ctf-network:
        ipv4_address: 10.10.1.3
    restart: unless-stopped

networks:
  ctf-network:
    external: true
```

### 5.2 Running the Service

From inside the SSH container:

```bash
cd ~/service
docker compose up -d
```

The vulnerable service is now accessible to all other teams on the `ctf-network` at `10.10.1.3:8080`.

### 5.3 Patching the Service

Teams SSH into their container, modify the source code, and rebuild:

```bash
# Edit the vulnerable code
vim ~/service/src/app.py

# Rebuild and restart
cd ~/service
docker compose up -d --build
```

**Important constraint:** The patch must not break the service's core functionality, or the team will fail SLA checks and lose defense points.

### 5.4 Taking Down the Service

```bash
cd ~/service
docker compose down
```

**Warning:** This will cause the team to fail all SLA checks until the service is brought back up. In a real A&D CTF, being down costs more points than being exploited.

### 5.5 Inspecting Logs

```bash
cd ~/service
docker compose logs -f
```

### 5.6 Exposing to the Internet (Optional)

If you want external participants to connect to the simulation:

**Option A: Cloudflare Tunnel (recommended)**

```bash
# On the host
cloudflared tunnel --url http://localhost:8080
```

**Option B: ngrok**

```bash
ngrok tcp 2201  # Expose Team 1's SSH
ngrok http 8080 # Expose Gameserver scoreboard
```

**Option C: WireGuard VPN**

Set up a WireGuard server on the host and distribute client configs to remote participants. This is the closest to a real A&D CTF setup.

---

## 6. Flag Collection & Submission

### 6.1 Flag Format

Flags follow a standardized format to prevent false submissions:

```
FLAG{[a-f0-9]{32}}
```

Example: `FLAG{a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6}`

Each flag is unique, tied to a specific team and round.

### 6.2 Flag Lifecycle

```
┌─────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  Generate│────▶│  Plant   │────▶│  Steal   │────▶│  Submit  │
│  (GS)   │     │  (GS)    │     │  (Attacker)    │  (GS API)│
└─────────┘     └──────────┘     └──────────┘     └──────────┘
     │                                                   │
     │              Round N flags expire                  │
     └────────────── after M rounds ──────────────────────┘
```

**Step-by-step:**

1. **Generation (every tick):** The gameserver generates one unique flag per team per round. Flags are stored in the gameserver's database with metadata: `{flag, team_id, round, service_id, timestamp, status}`.

2. **Planting:** The gameserver writes the flag into each team's vulnerable service. Methods:
   - **File-based:** Write to a known path (e.g., `/app/data/flag.txt`) via `docker exec` or a mounted volume.
   - **API-based:** Call the service's API to store the flag as data (e.g., create a user with the flag as a password, post a message containing the flag). This is more realistic and harder to defend.
   - **Agent-based:** A small agent script inside the vuln container polls the gameserver for new flags and writes them locally.

3. **Stealing:** Attackers exploit vulnerabilities to extract the current flag from other teams' services.

4. **Submission:** Attackers submit stolen flags to the gameserver.

5. **Expiry:** Flags older than M rounds are no longer accepted (prevents hoarding).

### 6.3 Flag Planting Implementation

**Method 1: Docker Exec (simplest)**

The gameserver runs on the host (or has access to the Docker socket) and uses:

```bash
docker exec team1-vuln sh -c "echo 'FLAG{abc123...}' > /app/data/flag.txt"
```

**Method 2: Shared Volume**

Each team's flag directory is a Docker volume that both the gameserver and the vuln container can access:

```yaml
# Gameserver
volumes:
  - team1-flags:/flags/team1

# Team 1 vuln service
volumes:
  - team1-flags:/app/data
```

**Method 3: Service API (most realistic)**

The gameserver interacts with each team's service the same way a legitimate user would — e.g., creating an account, posting a message, uploading a file. The flag is embedded in this data. This is the approach used by serious CTFs like RuCTF and FaustCTF.

### 6.4 Flag Submission API

The gameserver exposes a submission endpoint:

**HTTP API:**

```
POST http://10.10.0.2:8080/api/submit
Content-Type: application/json

{
  "team_token": "team1-secret-token",
  "flag": "FLAG{a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6}"
}
```

**Responses:**

| Status | Meaning |
|---|---|
| `200 OK` | Flag accepted, points awarded |
| `400 Bad Request` | Malformed flag |
| `409 Conflict` | Flag already submitted by this team |
| `404 Not Found` | Flag not recognized or expired |
| `403 Forbidden` | Cannot submit your own flag |
| `429 Too Many Requests` | Rate limit exceeded |

**TCP Socket (alternative, closer to real CTFs):**

```bash
echo "FLAG{a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6}" | nc 10.10.0.2 31337
```

### 6.5 Submission Rules

- A team cannot submit their own flag.
- Each flag can only be submitted once per attacking team.
- Flags expire after a configurable number of rounds (e.g., 5 rounds).
- Rate limiting prevents brute-force submission (e.g., max 50 submissions per minute per team).

---

## 7. Gameserver Design

### 7.1 Components

The gameserver is the brain of the competition. It has four main components:

```
┌──────────────────────────────────────────┐
│              GAMESERVER                   │
│                                          │
│  ┌────────────┐    ┌────────────────┐    │
│  │ Tick Engine │    │ Submission API │    │
│  │ (scheduler) │    │ (HTTP/TCP)     │    │
│  └──────┬─────┘    └───────┬────────┘    │
│         │                  │             │
│  ┌──────┴─────┐    ┌──────┴────────┐    │
│  │ Flag Plant  │    │ SLA Checker   │    │
│  │ Module      │    │ Module        │    │
│  └──────┬─────┘    └───────┬────────┘    │
│         │                  │             │
│         └────────┬─────────┘             │
│           ┌──────┴──────┐                │
│           │  Database   │                │
│           │ (PostgreSQL │                │
│           │  or SQLite) │                │
│           └─────────────┘                │
│                                          │
│  ┌──────────────────────────────────┐    │
│  │         Scoreboard Web UI        │    │
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

### 7.2 Tick Engine

The tick engine drives the competition clock:

```python
import time
import threading

TICK_DURATION = 120  # seconds

def tick_loop():
    round_number = 0
    while competition_running:
        round_number += 1
        print(f"=== Round {round_number} ===")

        # 1. Generate and plant flags
        for team in teams:
            flag = generate_flag()
            plant_flag(team, flag)
            db.store_flag(flag, team.id, round_number)

        # 2. Run SLA checks (in parallel)
        threads = []
        for team in teams:
            t = threading.Thread(target=check_sla, args=(team, round_number))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # 3. Calculate scores
        calculate_scores(round_number)

        # 4. Update scoreboard
        update_scoreboard()

        # Wait for next tick
        time.sleep(TICK_DURATION)
```

### 7.3 Database Schema

```sql
CREATE TABLE teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,  -- for flag submission auth
    ip_address  TEXT NOT NULL
);

CREATE TABLE flags (
    id          INTEGER PRIMARY KEY,
    flag        TEXT NOT NULL UNIQUE,
    team_id     INTEGER REFERENCES teams(id),
    service_id  INTEGER,
    round       INTEGER NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expired     BOOLEAN DEFAULT FALSE
);

CREATE TABLE submissions (
    id              INTEGER PRIMARY KEY,
    flag_id         INTEGER REFERENCES flags(id),
    attacker_id     INTEGER REFERENCES teams(id),
    submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(flag_id, attacker_id)  -- prevent duplicate submissions
);

CREATE TABLE sla_checks (
    id          INTEGER PRIMARY KEY,
    team_id     INTEGER REFERENCES teams(id),
    service_id  INTEGER,
    round       INTEGER NOT NULL,
    status      TEXT NOT NULL,  -- 'up', 'down', 'corrupt', 'mumble'
    details     TEXT,
    checked_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE scores (
    team_id     INTEGER REFERENCES teams(id),
    round       INTEGER NOT NULL,
    attack_pts  REAL DEFAULT 0,
    defense_pts REAL DEFAULT 0,
    sla_pts     REAL DEFAULT 0,
    total       REAL DEFAULT 0,
    PRIMARY KEY (team_id, round)
);
```

### 7.4 Scoring Algorithm

A common scoring approach (used by FaustCTF, RuCTF):

- **Attack points:** For each stolen flag, the attacker gets `1 / (number of teams that also stole this flag)` points. This rewards finding exploits early.
- **Defense points:** For each round a team's flag is NOT stolen, they earn 1 defense point.
- **SLA points:** Each round, if the service passes the SLA check, the team earns 1 SLA point. The SLA multiplier affects total score: `total = (attack + defense) * (sla_percentage)`.

You can simplify for a local simulation:
- +1 point per stolen flag
- -1 point per flag stolen from you
- -5 points per failed SLA check

---

## 8. Scoreboard & SLA Monitoring

### 8.1 Scoreboard

The gameserver serves a web-based scoreboard:

```
http://10.10.0.2:8080/scoreboard
```

Or mapped to the host:

```
http://localhost:8080/scoreboard
```

Features:
- Real-time team rankings
- Score breakdown (attack / defense / SLA)
- Service status indicators (green = up, red = down, yellow = degraded)
- Round history graph
- Flag submission log (for organizers)

**Tech stack:** A simple Flask/FastAPI app with server-sent events (SSE) or WebSocket for live updates, or just auto-refreshing HTML.

### 8.2 SLA Checker

The SLA checker verifies that each team's service is functional every round. It acts as a "legitimate user" of the service.

**Check types:**

| Status | Meaning |
|---|---|
| `UP` | Service is reachable and fully functional |
| `DOWN` | Service is not reachable (connection refused, timeout) |
| `MUMBLE` | Service responds but behaves incorrectly (wrong output, errors) |
| `CORRUPT` | Service is reachable but previously planted flags are missing or corrupted |

**Checker script example:**

```python
import requests

def check_team_service(team_ip, port, expected_flag):
    try:
        # Check 1: Service is reachable
        resp = requests.get(f"http://{team_ip}:{port}/", timeout=5)
        if resp.status_code != 200:
            return "MUMBLE", "Unexpected status code"

        # Check 2: Core functionality works
        resp = requests.post(f"http://{team_ip}:{port}/api/note", 
                           json={"content": "test"}, timeout=5)
        if resp.status_code != 201:
            return "MUMBLE", "Cannot create notes"

        # Check 3: Previously planted flag is still accessible
        resp = requests.get(f"http://{team_ip}:{port}/api/note/1", timeout=5)
        if expected_flag not in resp.text:
            return "CORRUPT", "Flag not found in service data"

        return "UP", "All checks passed"

    except requests.ConnectionError:
        return "DOWN", "Connection refused"
    except requests.Timeout:
        return "DOWN", "Connection timeout"
    except Exception as e:
        return "MUMBLE", str(e)
```

---

## 9. Directory Layout

```
ctf-simulation/
│
├── README.md                        # This file
│
├── docker-compose.yml               # Top-level orchestrator
├── .env                             # Environment variables (team count, tick duration, etc.)
│
├── gameserver/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                      # Entry point: starts tick engine + web server
│   ├── config.py                    # Competition configuration
│   ├── models.py                    # Database models
│   ├── tick_engine.py               # Round management, flag generation & planting
│   ├── submission.py                # Flag submission API
│   ├── checker.py                   # SLA checker
│   ├── scoring.py                   # Score calculation
│   ├── templates/
│   │   ├── scoreboard.html          # Scoreboard UI
│   │   └── admin.html               # Admin panel
│   └── static/
│       ├── style.css
│       └── scoreboard.js
│
├── services/
│   └── example-vuln-service/        # Template vulnerable service
│       ├── Dockerfile
│       ├── docker-compose.yml
│       ├── src/
│       │   ├── app.py               # Vulnerable web application
│       │   ├── requirements.txt
│       │   └── templates/
│       │       └── index.html
│       ├── data/
│       │   └── .gitkeep
│       └── README.md                # Service documentation & hints
│
├── teams/                           # Generated per-team configurations
│   ├── team1/
│   │   ├── docker-compose.yml       # Team 1's SSH + vuln service
│   │   ├── ssh/
│   │   │   └── Dockerfile
│   │   └── vuln-service/            # Copy of the vulnerable service
│   │       ├── docker-compose.yml
│   │       ├── Dockerfile
│   │       └── src/
│   ├── team2/
│   │   └── ...
│   └── teamN/
│       └── ...
│
├── scripts/
│   ├── setup.sh                     # Generate team configs, build images
│   ├── start.sh                     # Start the entire competition
│   ├── stop.sh                      # Stop everything gracefully
│   ├── reset.sh                     # Reset scores, flags, rebuild services
│   └── gen_team.sh                  # Generate config for a single team
│
└── docs/
    ├── participant-guide.md         # Guide for team members
    ├── organizer-guide.md           # Guide for running the competition
    └── writing-checkers.md          # How to write SLA checkers
```

---

## 10. Orchestration & Startup

### 10.1 Top-Level Docker Compose

The root `docker-compose.yml` wires everything together:

```yaml
version: "3.8"

networks:
  ctf-network:
    driver: bridge
    ipam:
      config:
        - subnet: 10.10.0.0/16
          gateway: 10.10.0.1

services:
  gameserver:
    build: ./gameserver
    container_name: gameserver
    networks:
      ctf-network:
        ipv4_address: 10.10.0.2
    ports:
      - "8080:8080"     # Scoreboard & API
      - "31337:31337"   # Flag submission (TCP)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # To plant flags via docker exec
      - gameserver-data:/app/data
    environment:
      - TICK_DURATION=120
      - NUM_TEAMS=4
      - FLAG_EXPIRY_ROUNDS=5

  # --- Team 1 ---
  team1-ssh:
    build:
      context: ./teams/team1/ssh
      args:
        TEAM_USER: ctfuser
        TEAM_PASS: team1pass
    container_name: team1-ssh
    hostname: team1
    networks:
      ctf-network:
        ipv4_address: 10.10.1.2
    ports:
      - "2201:22"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - team1-service:/home/ctfuser/service
    depends_on:
      - gameserver
    cap_add:
      - NET_ADMIN   # For optional tc rules
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 2G

  team1-vuln:
    build: ./teams/team1/vuln-service
    container_name: team1-vuln
    networks:
      ctf-network:
        ipv4_address: 10.10.1.3
    volumes:
      - team1-flags:/app/data
    restart: unless-stopped

  # --- Team 2 (same pattern) ---
  team2-ssh:
    build:
      context: ./teams/team2/ssh
      args:
        TEAM_USER: ctfuser
        TEAM_PASS: team2pass
    container_name: team2-ssh
    hostname: team2
    networks:
      ctf-network:
        ipv4_address: 10.10.2.2
    ports:
      - "2202:22"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - team2-service:/home/ctfuser/service
    depends_on:
      - gameserver

  team2-vuln:
    build: ./teams/team2/vuln-service
    container_name: team2-vuln
    networks:
      ctf-network:
        ipv4_address: 10.10.2.3
    volumes:
      - team2-flags:/app/data
    restart: unless-stopped

  # ... repeat for team N ...

volumes:
  gameserver-data:
  team1-service:
  team1-flags:
  team2-service:
  team2-flags:
```

### 10.2 Setup Script

```bash
#!/bin/bash
# scripts/setup.sh — Generate team configurations

NUM_TEAMS=${1:-4}
TEMPLATE_DIR="services/example-vuln-service"

echo "[*] Setting up CTF simulation for ${NUM_TEAMS} teams..."

for i in $(seq 1 $NUM_TEAMS); do
    TEAM_DIR="teams/team${i}"
    mkdir -p "${TEAM_DIR}/ssh"
    mkdir -p "${TEAM_DIR}/vuln-service"

    # Copy vulnerable service template
    cp -r ${TEMPLATE_DIR}/* "${TEAM_DIR}/vuln-service/"

    # Generate SSH Dockerfile with team-specific credentials
    cat > "${TEAM_DIR}/ssh/Dockerfile" <<EOF
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y openssh-server sudo curl vim nano net-tools \\
    iputils-ping nmap python3 python3-pip git && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://get.docker.com | sh
RUN useradd -m -s /bin/bash ctfuser \\
    && echo "ctfuser:team${i}pass" | chpasswd \\
    && usermod -aG sudo ctfuser \\
    && usermod -aG docker ctfuser
RUN mkdir -p /run/sshd
COPY ../vuln-service/ /home/ctfuser/service/
RUN chown -R ctfuser:ctfuser /home/ctfuser/service
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D"]
EOF

    echo "[+] Team ${i} configured (SSH pass: team${i}pass, IP: 10.10.${i}.2)"
done

echo "[*] Generating docker-compose.yml..."
# (Generate the top-level docker-compose.yml dynamically based on NUM_TEAMS)

echo "[*] Setup complete. Run 'docker compose up -d' to start."
```

### 10.3 Starting the Competition

```bash
# 1. Generate team environments
./scripts/setup.sh 4

# 2. Build all images
docker compose build

# 3. Start everything
docker compose up -d

# 4. Verify
docker compose ps
curl http://localhost:8080/scoreboard
ssh -p 2201 ctfuser@localhost  # Test team 1 SSH
```

### 10.4 Stopping the Competition

```bash
# Graceful shutdown
docker compose down

# Full cleanup (remove volumes, images)
docker compose down -v --rmi all
```

---

## 11. Operational Runbook

### 11.1 Before the Competition

1. **Test the vulnerable service** — Ensure the challenge is solvable and the checker works.
2. **Test SSH access** — Verify each team can log in and run Docker commands.
3. **Test flag planting** — Verify flags appear in the correct location.
4. **Test flag submission** — Verify the submission API works correctly.
5. **Test SLA checks** — Verify the checker correctly detects UP/DOWN/MUMBLE/CORRUPT.
6. **Dry run** — Run 5-10 rounds with no participants to verify the tick engine.

### 11.2 During the Competition

- Monitor the gameserver logs: `docker compose logs -f gameserver`
- Monitor resource usage: `docker stats`
- Access the admin panel (if implemented) to pause/resume rounds, adjust scoring, etc.
- Watch for containers that have crashed: `docker compose ps`

### 11.3 Common Issues

| Issue | Solution |
|---|---|
| Team can't SSH | Check port mapping, verify `sshd` is running: `docker exec team1-ssh service ssh status` |
| Flag not planted | Check gameserver logs, verify Docker socket is mounted, check container names |
| SLA check always fails | Verify the checker matches the service's API, check network connectivity |
| Container OOM killed | Increase memory limits in `docker-compose.yml` |
| Teams interfering with each other | Switch to Docker-in-Docker (see Section 4.3) |

---

## 12. Security Considerations

### 12.1 Container Isolation

- **Docker socket access** is the biggest risk. If teams have access to the host Docker socket, they can potentially affect other teams' containers. For a trusted training environment this is acceptable. For untrusted participants, use DinD.
- Use **read-only filesystems** where possible (e.g., gameserver).
- Restrict **outbound internet access** from team containers if desired:

```bash
iptables -I DOCKER-USER -s 10.10.0.0/16 ! -d 10.10.0.0/16 -j DROP
```

### 12.2 Rate Limiting

- Limit flag submissions to prevent brute-force attempts.
- Limit SSH login attempts with `fail2ban` inside SSH containers.

### 12.3 Logging

- Log all flag submissions with timestamps and source IPs.
- Log all SLA check results.
- Optionally capture network traffic for post-competition analysis.

---

## 13. Scaling & Performance

### 13.1 Resource Requirements

| Teams | RAM (approx.) | CPU Cores | Disk |
|---|---|---|---|
| 2 | 4 GB | 4 | 10 GB |
| 4 | 8 GB | 4-6 | 20 GB |
| 8 | 16 GB | 6-8 | 40 GB |
| 16 | 32 GB | 8-12 | 80 GB |

Each team uses roughly 1-2 GB RAM (SSH container + vuln service) plus the gameserver overhead.

### 13.2 Optimization Tips

- Use **Alpine-based images** where possible to reduce memory and disk usage.
- Share the vulnerable service image across teams (only data volumes differ).
- Run SLA checks in parallel with `asyncio` or thread pools.
- Use SQLite for small competitions (< 8 teams), PostgreSQL for larger ones.
- If the host has limited RAM, reduce per-team memory limits and use lighter services.

### 13.3 Multi-Machine Extension

If you outgrow a single machine, you can:
1. Replace the Docker bridge network with a **WireGuard mesh** or **Docker Swarm overlay network**.
2. Distribute team containers across multiple machines.
3. Keep the gameserver centralized.
4. This is the natural progression toward a "real" A&D CTF infrastructure.

---

## 14. Existing Open-Source Frameworks

Instead of building everything from scratch, consider adapting these battle-tested projects:

### ForcAD
- **URL:** https://github.com/pomo-mondreganto/ForcAD
- **What it provides:** Complete A&D gameserver — flag rotation, submission API, SLA checker framework, scoreboard UI, admin panel.
- **How to use it:** ForcAD already runs in Docker. You would wire it to your team containers instead of real VMs. It expects checkers to be provided per service.
- **Best for:** Most straightforward option for a full-featured local simulation.

### saarCTF Infrastructure
- **URL:** https://github.com/MarkusBauer/saarern
- **What it provides:** Full A&D infrastructure including VPN management, gameserver, and scoreboard, used for the saarCTF competition.
- **How to use it:** Heavier setup, designed for multi-machine deployments, but can be adapted to run locally.
- **Best for:** If you want to study how a production A&D CTF works.

### CTFd with A&D Plugin
- **URL:** https://github.com/CTFd/CTFd
- **What it provides:** Primarily a Jeopardy-style CTF platform, but has community plugins for A&D scoring.
- **Best for:** If you want a polished web UI and are willing to write custom A&D logic.

### Tinfoil (Minimal)
- **URL:** Various community implementations on GitHub
- **What it provides:** Minimal A&D gameservers in Python, often < 500 lines.
- **Best for:** Learning how A&D scoring works, then building your own.

**Recommendation:** Start with **ForcAD** if you want a production-quality gameserver, and focus your effort on creating the team containers and vulnerable services.

---

## 15. Summary Table

| Aspect | Technology | Details |
|---|---|---|
| **Network** | Docker bridge network | `10.10.0.0/16`, replaces VPN |
| **Team Machines** | Docker containers | Ubuntu + SSH + Docker CLI |
| **Vuln Services** | Docker Compose per team | Managed from inside SSH container |
| **Flag Planting** | `docker exec` or shared volumes | Gameserver writes flags each tick |
| **Flag Submission** | HTTP REST API / TCP socket | `POST /api/submit` on gameserver |
| **SLA Checks** | Python checker scripts | Runs each tick, checks UP/DOWN/MUMBLE/CORRUPT |
| **Scoring** | Gameserver DB | Attack + Defense + SLA multiplier |
| **Scoreboard** | Web UI on gameserver | Real-time standings, service status |
| **Orchestration** | Top-level Docker Compose | Single `docker compose up -d` to start |
| **Internet Exposure** | Cloudflare Tunnel / ngrok / WireGuard | Optional, for remote participants |

---

## Next Steps

1. **Define the vulnerable service(s)** — What application(s) will teams attack/defend? (web app, binary, custom protocol, etc.)
2. **Write the SLA checker(s)** — One checker per service, verifying core functionality.
3. **Choose a gameserver** — Build custom or use ForcAD.
4. **Test with 2 teams** — Start small, verify the full flag lifecycle works.
5. **Scale up** — Add more teams and services as needed.

---

*This document provides the architectural foundation. Each section can be expanded into a full implementation guide based on the specific vulnerable services and competition rules desired.*
