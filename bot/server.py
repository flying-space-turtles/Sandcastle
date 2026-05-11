#!/usr/bin/env python3
"""Sandcastle CTF event server.

Polls bot container log files and serves a simple JSON API consumed by the
visualizer frontend.

Endpoints
---------
  GET /api/state  →  { "botTeams": [2,3,4], "events": [...last 200...] }

Usage
-----
  python3 server.py 2 3 4            # teams 2, 3, 4 are bots
  python3 server.py --port 5001 2 3  # custom port (default: 5001)
"""

import json
import re
import subprocess

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ── Shared state ──────────────────────────────────────────────────────────────

BOT_TEAMS: list[int] = []
EVENTS: list[dict] = []       # newest last, capped at MAX_EVENTS
EVENTS_LOCK = threading.Lock()
MAX_EVENTS = 200

# ── Log parsing ───────────────────────────────────────────────────────────────

def _parse_line(line: str, team_id: int) -> dict | None:
    """Return an event dict from a single bot.log line, or None if uninteresting."""
    line = ANSI_RE.sub('', line).strip()
    if not line or not line.startswith('['):
        return None

    ts = time.time()
    tag = line[:3]  # [+], [-], [~], [!], [*]
    body = line[4:].strip()

    # Flag capture: [+]   [path_traversal] FLAG: FLAG{...}
    if tag == '[+]' and 'FLAG{' in body:
        flag_match = re.search(r'FLAG\{[a-f0-9]{32}\}', body)
        method_match = re.search(r'\[(\w+)\]', body)
        flag = flag_match.group(0) if flag_match else ''
        method = method_match.group(1) if method_match else 'unknown'
        return {
            'ts': ts,
            'type': 'flag',
            'attacker': f'team{team_id}',
            'method': method,
            'flag': flag,
            'msg': body.strip(),
        }

    # Ping UP: [+] ping teamN ip UP
    if tag == '[+]' and body.startswith('ping '):
        parts = body.split()
        victim = parts[1] if len(parts) > 1 else '?'
        return {
            'ts': ts,
            'type': 'ping_up',
            'attacker': f'team{team_id}',
            'victim': victim,
            'msg': body,
        }

    # Ping DOWN: [-] ping teamN ip DOWN
    if tag == '[-]' and body.startswith('ping '):
        parts = body.split()
        victim = parts[1] if len(parts) > 1 else '?'
        return {
            'ts': ts,
            'type': 'ping_down',
            'attacker': f'team{team_id}',
            'victim': victim,
            'msg': body,
        }

    # Attack start: [*] ── Attacking teamN (http://...) ──
    if tag == '[*]' and 'Attacking team' in body:
        m = re.search(r'Attacking (team\d+)', body)
        victim = m.group(1) if m else '?'
        return {
            'ts': ts,
            'type': 'probe',
            'attacker': f'team{team_id}',
            'victim': victim,
            'msg': f'Attacking {victim}',
        }

    # No flag on an exploit attempt: [!]   [method] no flag found
    if tag == '[!]' and 'no flag found' in body:
        method_match = re.search(r'\[(\w+)\]', body)
        method = method_match.group(1) if method_match else 'unknown'
        return {
            'ts': ts,
            'type': 'fail',
            'attacker': f'team{team_id}',
            'msg': f'{method} — no flag',
        }

    # Watchdog / service restart: [!] lines that mention restart/down
    if tag == '[!]' and any(w in body.lower() for w in ('restart', 'down', 'starting')):
        return {
            'ts': ts,
            'type': 'watchdog',
            'attacker': f'team{team_id}',
            'msg': body,
        }

    # Sleep tick: [*] Sleeping N s ...
    if tag == '[*]' and 'Sleeping' in body:
        return {
            'ts': ts,
            'type': 'sleep',
            'attacker': f'team{team_id}',
            'msg': body,
        }

    return None


# Per-team cursor: number of log lines already processed
_log_cursors: dict[int, int] = {}


def _poll_team(team_id: int) -> list[dict]:
    """Read new lines from a team's /tmp/bot.log via docker exec."""
    cname = f'team{team_id}-ssh'
    cursor = _log_cursors.get(team_id, 0)
    try:
        result = subprocess.run(
            ['docker', 'exec', cname, 'cat', '/tmp/bot.log'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        lines = result.stdout.splitlines()
        new_lines = lines[cursor:]
        _log_cursors[team_id] = len(lines)

        events = []
        for line in new_lines:
            ev = _parse_line(line, team_id)
            if ev:
                events.append(ev)
        return events

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _poller():
    """Background thread: poll all bot teams every 5 seconds."""
    global EVENTS
    while True:
        new_events = []
        for team_id in BOT_TEAMS:
            new_events.extend(_poll_team(team_id))

        if new_events:
            with EVENTS_LOCK:
                EVENTS.extend(new_events)
                EVENTS = EVENTS[-MAX_EVENTS:]

        time.sleep(5)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence access logs
        pass

    def _send_json(self, code: int, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/state':
            with EVENTS_LOCK:
                snapshot = list(EVENTS)
            self._send_json(200, {'botTeams': BOT_TEAMS, 'events': snapshot})
        else:
            self._send_json(404, {'error': 'not found'})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    args = sys.argv[1:]
    port = 5001

    if '--port' in args:
        idx = args.index('--port')
        port = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    BOT_TEAMS = [int(a) for a in args if a.isdigit()]

    if not BOT_TEAMS:
        print('Usage: python3 server.py [--port N] <team_id> [team_id ...]')
        print('Example: python3 server.py 2 3 4')
        sys.exit(1)

    print(f'[*] Sandcastle event server  →  http://localhost:{port}/api/state')
    print(f'[*] Watching bot teams: {BOT_TEAMS}')

    threading.Thread(target=_poller, daemon=True).start()

    server = HTTPServer(('localhost', port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[*] Stopped.')
