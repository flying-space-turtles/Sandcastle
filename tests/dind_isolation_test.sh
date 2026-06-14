#!/usr/bin/env bash
# Validate Docker-in-Docker team isolation against a running arena.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

PASS=0
FAIL=0

pass() { printf '[PASS] %s\n' "$1"; ((PASS++)) || true; }
fail() { printf '[FAIL] %s\n' "$1"; ((FAIL++)) || true; }

container_state() {
    docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null || true
}

team_docker() {
    local team="$1"
    shift
    docker exec "team${team}-vuln" docker "$@"
}

expect_running() {
    local name="$1"
    if [[ "$(container_state "${name}")" == "running" ]]; then
        pass "${name} is running"
    else
        fail "${name} is not running"
    fi
}

arena_config_load "${ROOT}" || exit 1
if [[ "${ARENA_ISOLATION_MODE}" != "dind" ]]; then
    echo "ERROR: ARENA_ISOLATION_MODE must be dind for this test." >&2
    echo "Run ./scripts/setup.sh --dind && ./scripts/arena.sh up first." >&2
    exit 1
fi
((ARENA_TEAM_COUNT >= 2)) || {
    echo "ERROR: at least two teams are required for DinD isolation testing." >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || {
    echo "ERROR: Docker CLI is not installed." >&2
    exit 1
}
docker info >/dev/null 2>&1 || {
    echo "ERROR: Docker daemon is not reachable." >&2
    exit 1
}

echo ""
echo "=== Sandcastle DinD isolation tests ==="
echo ""

for team in 1 2; do
    expect_running "team${team}-ssh"
    expect_running "team${team}-vuln"
    expect_running "team${team}-dind"

    if docker inspect \
        --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Destination}}{{end}}{{end}}' \
        "team${team}-vuln" | grep -q /var/run/docker.sock; then
        fail "team${team}-vuln must not mount the host Docker socket"
    else
        pass "team${team}-vuln has no host Docker socket mount"
    fi

    if docker exec "team${team}-vuln" sh -lc \
        'case "${DOCKER_HOST:-}" in unix://*) test -S "${DOCKER_HOST#unix://}" ;; *) exit 1 ;; esac && docker info >/dev/null'; then
        pass "team${team}-vuln can reach its team DinD daemon"
    else
        fail "team${team}-vuln cannot reach its team DinD daemon"
    fi
done

list1="$(team_docker 1 ps -a --format '{{.Names}}' 2>/dev/null || true)"
if grep -q '^team2-' <<< "${list1}"; then
    fail "team1 nested daemon must not list team2 containers"
else
    pass "team1 nested daemon does not list team2 containers"
fi
if grep -q '^team1-vuln-app$' <<< "${list1}"; then
    pass "team1 nested daemon lists its own app"
else
    fail "team1 nested daemon does not list team1-vuln-app"
fi

list2="$(team_docker 2 ps -a --format '{{.Names}}' 2>/dev/null || true)"
if grep -q '^team1-' <<< "${list2}"; then
    fail "team2 nested daemon must not list team1 containers"
else
    pass "team2 nested daemon does not list team1 containers"
fi
if grep -q '^team2-vuln-app$' <<< "${list2}"; then
    pass "team2 nested daemon lists its own app"
else
    fail "team2 nested daemon does not list team2-vuln-app"
fi

for team in 1 2; do
    target="http://${ARENA_NETWORK_PREFIX}.${team}.3:${ARENA_SERVICE_PORT}/health"
    if docker exec team1-vuln curl -fsS --max-time 3 "${target}" >/dev/null; then
        pass "team${team} service is reachable at ${target}"
    else
        fail "team${team} service is not reachable at ${target}"
    fi
done

if docker exec team1-vuln sh -lc \
    'cd "$HOME/example-vuln" && docker compose up -d --build --remove-orphans >/dev/null'; then
    pass "team1 can rebuild its own app inside DinD"
else
    fail "team1 cannot rebuild its own app inside DinD"
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
echo ""

if ((FAIL > 0)); then
    exit 1
fi
