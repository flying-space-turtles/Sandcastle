#!/usr/bin/env bash
# Validate Docker-in-Docker team isolation against a running arena.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

PASS=0
FAIL=0
HEALTH_TIMEOUT="${ARENA_DIND_ISOLATION_HEALTH_TIMEOUT_SECONDS:-30}"
HEALTH_POLL_SECONDS="${ARENA_DIND_ISOLATION_POLL_SECONDS:-1}"

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

shell_quote() {
    local value="$1"
    printf "'%s'" "${value//\'/\'\\\'\'}"
}

team_service_dir() {
    local team="$1"
    local username

    username="$(arena_config_render_team_value "${ARENA_TEAM_USERNAME_PATTERN}" "${team}")"
    printf '/home/%s/example-vuln\n' "${username}"
}

require_positive_int() {
    local name="$1"
    local value="${!name}"

    [[ "${value}" =~ ^[0-9]+$ ]] || {
        echo "ERROR: ${name} must be a positive integer." >&2
        exit 1
    }
    value="$((10#${value}))"
    ((value > 0)) || {
        echo "ERROR: ${name} must be a positive integer." >&2
        exit 1
    }
    printf -v "${name}" '%s' "${value}"
}

expect_running() {
    local name="$1"
    if [[ "$(container_state "${name}")" == "running" ]]; then
        pass "${name} is running"
    else
        fail "${name} is not running"
    fi
}

service_diagnostics() {
    local source_container="$1"
    local target="$2"

    {
        echo "--- DinD service reachability diagnostics ---"
        echo "source: ${source_container}"
        echo "target: ${target}"
        echo "--- ${source_container}: route to target ---"
        docker exec "${source_container}" sh -lc \
            "ip route get $(shell_quote "${target}") || true"
        echo "--- ${source_container}: curl target ---"
        docker exec "${source_container}" curl -v --max-time 5 \
            "http://${target}:${ARENA_SERVICE_PORT}/health" || true
        echo "--- team2-vuln: listeners ---"
        docker exec team2-vuln sh -lc \
            "ss -lntp || true"
        echo "--- sandcastle-firewall logs ---"
        docker logs --tail=120 sandcastle-firewall || true
    } >&2
}

wait_for_service_health() {
    local source_container="$1"
    local target="$2"
    local attempts=$((HEALTH_TIMEOUT / HEALTH_POLL_SECONDS + 1))
    local attempt

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if docker exec "${source_container}" curl -fsS --max-time 3 \
            "http://${target}:${ARENA_SERVICE_PORT}/health" >/dev/null; then
            return 0
        fi
        ((attempt < attempts)) && sleep "${HEALTH_POLL_SECONDS}"
    done

    service_diagnostics "${source_container}" "${target}"
    return 1
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
require_positive_int HEALTH_TIMEOUT
require_positive_int HEALTH_POLL_SECONDS

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
    if wait_for_service_health "team${team}-vuln" "${ARENA_NETWORK_PREFIX}.${team}.3"; then
        pass "team${team} service is reachable from its own vulnerable machine at ${target}"
    else
        fail "team${team} service is not reachable from its own vulnerable machine at ${target}"
    fi
done

target="http://${ARENA_NETWORK_PREFIX}.2.3:${ARENA_SERVICE_PORT}/health"
if wait_for_service_health team1-vuln "${ARENA_NETWORK_PREFIX}.2.3"; then
    pass "team1 can reach team2 service through the CTF network at ${target}"
else
    fail "team1 cannot reach team2 service through the CTF network at ${target}"
fi

team1_service_dir="$(team_service_dir 1)"
if docker exec team1-vuln sh -lc \
    "cd $(shell_quote "${team1_service_dir}") && docker compose up -d --build --remove-orphans >/dev/null"; then
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
