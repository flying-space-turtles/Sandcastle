#!/usr/bin/env bash
# SC-017 isolation tests.
#
# Tests that the per-team Docker socket filter proxy enforces team boundaries.
# Requires ARENA_ISOLATION_MODE=isolated and a running arena, OR the
# SANDCASTLE_ISOLATION_FIXTURE=1 env variable to start a lightweight local
# fixture (two proxy processes + fake containers) instead.
#
# Usage:
#   ./tests/isolation_test.sh               # against a running arena
#   SANDCASTLE_ISOLATION_FIXTURE=1 ./tests/isolation_test.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS=0
FAIL=0

pass() { printf '[PASS] %s\n' "$1"; ((PASS++)) || true; }
fail() { printf '[FAIL] %s\n' "$1"; ((FAIL++)) || true; }

# docker CLI pointed at a team-scoped proxy socket
docker_team() {
    local team="$1"; shift
    docker -H "unix:///run/sandcastle/team${team}.sock" "$@"
}

# Expect a command to succeed (exit 0)
expect_allow() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        pass "${label}"
    else
        fail "${label} (expected allow, got deny/error)"
    fi
}

# Expect a command to fail (non-zero exit)
expect_deny() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        fail "${label} (expected deny, got allow)"
    else
        pass "${label}"
    fi
}

# ── Fixture mode ──────────────────────────────────────────────────────────────

FIXTURE_PIDS=()

cleanup_fixture() {
    for pid in "${FIXTURE_PIDS[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    rm -f /run/sandcastle/team1.sock /run/sandcastle/team2.sock
}

start_fixture() {
    mkdir -p /run/sandcastle
    local proxy="${ROOT}/docker/docker-proxy/proxy.py"

    for team in 1 2; do
        TEAM_ID="${team}" \
        PROXY_SOCKET="/run/sandcastle/team${team}.sock" \
        HOST_SOCKET="/var/run/docker.sock" \
        python3 "${proxy}" &
        FIXTURE_PIDS+=($!)
    done

    trap cleanup_fixture EXIT

    # Wait for both sockets to appear (up to 10 seconds)
    local deadline=$(( $(date +%s) + 10 ))
    for team in 1 2; do
        while [[ ! -S "/run/sandcastle/team${team}.sock" ]]; do
            if (( $(date +%s) > deadline )); then
                echo "ERROR: proxy socket for team${team} never appeared" >&2
                exit 1
            fi
            sleep 0.1
        done
    done

    echo "[*] Fixture proxies running (PIDs: ${FIXTURE_PIDS[*]})"
}

# ── Pre-flight ────────────────────────────────────────────────────────────────

if [[ "${SANDCASTLE_ISOLATION_FIXTURE:-0}" == "1" ]]; then
    start_fixture
fi

for team in 1 2; do
    if [[ ! -S "/run/sandcastle/team${team}.sock" ]]; then
        echo "ERROR: /run/sandcastle/team${team}.sock not found." >&2
        echo "Start the arena with ARENA_ISOLATION_MODE=isolated, or use" >&2
        echo "SANDCASTLE_ISOLATION_FIXTURE=1 to start a local fixture." >&2
        exit 1
    fi
done

echo ""
echo "=== Sandcastle isolation tests ==="
echo ""

# ── Suite 1: container list filtering ─────────────────────────────────────────

echo "--- Suite 1: container list filtering ---"

# Team 1's list must not contain team2 containers
list1=$(docker_team 1 ps -a --format '{{.Names}}' 2>/dev/null || true)
if echo "${list1}" | grep -q "^team2-"; then
    fail "team1 list: team2 containers must not appear"
else
    pass "team1 list: no team2 containers visible"
fi

# Team 2's list must not contain team1 containers
list2=$(docker_team 2 ps -a --format '{{.Names}}' 2>/dev/null || true)
if echo "${list2}" | grep -q "^team1-"; then
    fail "team2 list: team1 containers must not appear"
else
    pass "team2 list: no team1 containers visible"
fi

# /events must be denied
expect_deny "team1: /events denied" \
    docker_team 1 events --since 0s --until 0s

# ── Suite 2: cross-team container operations ───────────────────────────────────

echo ""
echo "--- Suite 2: cross-team container operations ---"

# team1 proxy must deny inspect of team2-vuln
expect_deny "team1 cannot inspect team2-vuln" \
    docker_team 1 inspect team2-vuln

# team1 proxy must deny stop of team2-vuln
expect_deny "team1 cannot stop team2-vuln" \
    docker_team 1 stop team2-vuln

# team1 proxy must deny exec into team2-vuln
expect_deny "team1 cannot exec into team2-vuln" \
    docker_team 1 exec team2-vuln id

# team2 proxy must deny inspect of team1-vuln
expect_deny "team2 cannot inspect team1-vuln" \
    docker_team 2 inspect team1-vuln

# team2 proxy must deny stop of team1-vuln
expect_deny "team2 cannot stop team1-vuln" \
    docker_team 2 stop team1-vuln

# ── Suite 3: own-container access ─────────────────────────────────────────────

echo ""
echo "--- Suite 3: own-container access ---"

# team1 may inspect its own vuln machine (read-only infra)
if docker_team 1 inspect team1-vuln >/dev/null 2>&1; then
    pass "team1 can inspect own team1-vuln"
else
    # Not running is also acceptable; only an explicit 403 is a failure
    deny_body=$(curl -s --unix-socket /run/sandcastle/team1.sock \
        "http://localhost/v1.44/containers/team1-vuln/json" 2>/dev/null || true)
    if echo "${deny_body}" | grep -q "access denied"; then
        fail "team1 denied inspect of own team1-vuln"
    else
        pass "team1 can inspect own team1-vuln (container not running — OK)"
    fi
fi

# team1 may inspect its own app container (if present)
if docker_team 1 inspect team1-vuln-app >/dev/null 2>&1; then
    pass "team1 can inspect own team1-vuln-app"
else
    deny_body=$(curl -s --unix-socket /run/sandcastle/team1.sock \
        "http://localhost/v1.44/containers/team1-vuln-app/json" 2>/dev/null || true)
    if echo "${deny_body}" | grep -q "access denied"; then
        fail "team1 denied inspect of own team1-vuln-app"
    else
        pass "team1 can inspect own team1-vuln-app (container not running — OK)"
    fi
fi

# ── Suite 4: write-op restriction on own infra ────────────────────────────────

echo ""
echo "--- Suite 4: write restrictions on own infra containers ---"

# team1 must NOT be able to stop its own vuln machine (infra = read-only)
expect_deny "team1 cannot stop own team1-vuln (infra write denied)" \
    docker_team 1 stop team1-vuln

# team1 must NOT be able to exec into its own vuln machine
expect_deny "team1 cannot exec into own team1-vuln (infra write denied)" \
    docker_team 1 exec team1-vuln id

# ── Suite 5: container create name check ──────────────────────────────────────

echo ""
echo "--- Suite 5: container create name enforcement ---"

# team1 must not be able to create a container named after team2
# (we just check the proxy response; we do not need a real image)
deny_body=$(curl -s -X POST \
    --unix-socket /run/sandcastle/team1.sock \
    -H "Content-Type: application/json" \
    -d '{"Image":"scratch","name":"team2-vuln-app"}' \
    "http://localhost/v1.44/containers/create?name=team2-vuln-app" 2>/dev/null || true)
if echo "${deny_body}" | grep -q "access denied"; then
    pass "team1 cannot create container named team2-vuln-app"
else
    fail "team1 create with team2 name was not denied (body: ${deny_body})"
fi

# team1 creating a container with its own name must reach the daemon
# (we expect an image-not-found error, not an access-denied from the proxy)
own_body=$(curl -s -X POST \
    --unix-socket /run/sandcastle/team1.sock \
    -H "Content-Type: application/json" \
    -d '{"Image":"no-such-image-xyzzy","name":"team1-vuln-app"}' \
    "http://localhost/v1.44/containers/create?name=team1-vuln-app" 2>/dev/null || true)
if echo "${own_body}" | grep -q "access denied"; then
    fail "team1 create with own name incorrectly denied by proxy"
else
    pass "team1 create with own name passes proxy (daemon response expected)"
fi

# ── Suite 6: version and ping endpoints ───────────────────────────────────────

echo ""
echo "--- Suite 6: always-allowed endpoints ---"

expect_allow "team1 can reach /_ping" \
    docker_team 1 info

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
echo ""

if ((FAIL > 0)); then
    exit 1
fi
