#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARENA="${ROOT}/scripts/arena.sh"
TMP_ROOT="$(mktemp -d)"
FIXTURE="${TMP_ROOT}/arena"
MOCK_BIN="${TMP_ROOT}/bin"
LOG_FILE="${TMP_ROOT}/lifecycle.log"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p \
    "${MOCK_BIN}" \
    "${FIXTURE}/config" \
    "${FIXTURE}/scripts/lib" \
    "${FIXTURE}/services/example-vuln" \
    "${FIXTURE}/teams/generated/team1/example-vuln/app" \
    "${FIXTURE}/teams/generated/team2/example-vuln/app"

cp "${ROOT}/scripts/lib/arena_config.sh" "${FIXTURE}/scripts/lib/arena_config.sh"

cat > "${FIXTURE}/config/arena.env" <<'EOF'
ARENA_TEAM_COUNT=2
ARENA_CTF_SUBNET=10.10.0.0/16
ARENA_CTF_GATEWAY=10.10.0.1
ARENA_SSH_BASE_PORT=2200
ARENA_SERVICE_PORT=8080
ARENA_TEAM_USERNAME_PATTERN=team{team}
ARENA_TEAM_PASSWORD_PATTERN=team{team}pass
ARENA_TEAM_TOKEN_PATTERN=sandcastle-team{team}-submission-token-change-me
ARENA_SERVICE_TEMPLATE=services/example-vuln
ARENA_FIREWALL_WS_PORT=6789
ARENA_FIREWALL_PROXY_PORT=15000
ARENA_FIREWALL_PROBE_PORT=18080
ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS=15
ARENA_FIREWALL_EVENT_QUEUE_SIZE=2048
ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES=4194304
ARENA_FIREWALL_RECENT_ICMP_LIMIT=4096
ARENA_BOT_API_HOST=127.0.0.1
ARENA_BOT_API_PORT=7878
ARENA_BOT_LOOP_SECONDS=60
ARENA_STARTUP_TIMEOUT_SECONDS=5
ARENA_ROUND_DURATION_SECONDS=120
ARENA_FLAG_EXPIRY_ROUNDS=5
EOF

printf 'services: {}\n' > "${FIXTURE}/docker-compose.yml"
for team in 1 2; do
    printf 'services: {}\n' \
        > "${FIXTURE}/teams/generated/team${team}/example-vuln/docker-compose.yml"
    printf 'print("team%s patch")\n' "${team}" \
        > "${FIXTURE}/teams/generated/team${team}/example-vuln/app/app.py"
done

cat > "${FIXTURE}/scripts/setup.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'setup %s\n' "$*" >> "${ARENA_TEST_LOG:?}"
EOF
chmod +x "${FIXTURE}/scripts/setup.sh"

cat > "${FIXTURE}/scripts/firewall-preflight.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'preflight %s\n' "$*" >> "${ARENA_TEST_LOG:?}"
[[ "${ARENA_TEST_PREFLIGHT_FAIL:-0}" != "1" ]]
EOF

cat > "${FIXTURE}/scripts/smoke-network.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'network-smoke\n' >> "${ARENA_TEST_LOG:?}"
[[ "${ARENA_TEST_SMOKE_FAIL:-0}" != "1" ]]
EOF

chmod +x \
    "${FIXTURE}/scripts/firewall-preflight.sh" \
    "${FIXTURE}/scripts/smoke-network.sh"

cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -u

printf 'docker %s\n' "$*" >> "${ARENA_TEST_LOG:?}"
scenario="${ARENA_TEST_SCENARIO:-healthy}"

case "${1:-}" in
    info)
        exit 0
        ;;
    compose)
        exit 0
        ;;
    inspect)
        if [[ "${2:-}" == "--format" ]]; then
            name="${4:-}"
            if [[ "${scenario}" == "infra-fail" && "${name}" == "team2-vuln" ]]; then
                echo "exited"
            elif [[ "${scenario}" == "missing-app" && "${name}" == "team2-vuln-app" ]]; then
                exit 1
            else
                echo "running"
            fi
        fi
        exit 0
        ;;
    exec)
        machine="${2:-}"
        if [[ "$*" == *"docker inspect --format"* ]]; then
            echo "running"
            exit 0
        fi
        if [[ "$*" == *"docker compose up"* ]]; then
            if [[ "${scenario}" == "dind-compose-fail" ]]; then
                exit 1
            fi
            exit 0
        fi
        if [[ "$*" == *"docker compose ps"* ]]; then
            echo "nested compose ps"
            exit 0
        fi
        if [[ "$*" == *"docker compose logs"* ]]; then
            echo "nested compose logs"
            exit 0
        fi
        if [[ "${scenario}" == "dind-forward-fail" && "$*" == *"curl -fsS --max-time 2 http://127.0.0.1:8080/health"* ]]; then
            exit 1
        fi
        if [[ "${scenario}" == "health-fail" && "${machine}" == "team2-vuln" ]]; then
            exit 1
        fi
        if [[ "${scenario}" == "final-status-fail" && "${machine}" == "sandcastle-bot-controller" ]]; then
            exit 1
        fi
        if [[ "${scenario}" == "visualizer-fail" && "${machine}" == "sandcastle-visualizer" ]]; then
            exit 1
        fi
        if [[ "${scenario}" == "firewall-runtime-fail" && "${machine}" == "sandcastle-firewall" ]]; then
            exit 1
        fi
        echo '{"status":"ok"}'
        exit 0
        ;;
    ps)
        printf '%s\n' team1-vuln-app team2-vuln-app
        exit 0
        ;;
    rm)
        exit 0
        ;;
    volume)
        case "${2:-}" in
            ls)
                printf '%s\n' sandcastle_team1-data sandcastle_team2-data
                ;;
            rm)
                ;;
        esac
        exit 0
        ;;
esac

exit 0
EOF

cat > "${MOCK_BIN}/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

chmod +x "${MOCK_BIN}/docker" "${MOCK_BIN}/sleep"

run_arena() {
    local scenario="$1"
    shift
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${FIXTURE}" \
        SANDCASTLE_FIREWALL_PREFLIGHT="${FIXTURE}/scripts/firewall-preflight.sh" \
        SANDCASTLE_NETWORK_SMOKE="${FIXTURE}/scripts/smoke-network.sh" \
        SANDCASTLE_HEALTH_POLL_SECONDS=1 \
        ARENA_TEST_LOG="${LOG_FILE}" \
        ARENA_TEST_SCENARIO="${scenario}" \
        "${ARENA}" "$@"
}

assert_log() {
    local pattern="$1"
    if ! grep -Fq "${pattern}" "${LOG_FILE}"; then
        echo "Expected lifecycle log to contain: ${pattern}" >&2
        cat "${LOG_FILE}" >&2
        exit 1
    fi
}

source_before="$(
    find "${FIXTURE}/teams/generated" -path '*/app/app.py' -print0 |
        sort -z |
        xargs -0 sha256sum
)"

: > "${LOG_FILE}"
up_output="$(run_arena healthy up --teams 2 --timeout 2)"
grep -Fq "Complete arena is healthy" <<< "${up_output}"
grep -Fq "team1" <<< "${up_output}"
grep -Fq "healthy" <<< "${up_output}"
assert_log "setup --remove-orphan-containers --teams 2"
assert_log "network-smoke"
assert_log "docker compose -f ${FIXTURE}/docker-compose.yml up -d --build --remove-orphans"
assert_log "docker rm -f team1-vuln-app"
assert_log "docker compose -f ${FIXTURE}/teams/generated/team1/example-vuln/docker-compose.yml up -d --build --force-recreate --remove-orphans"

remove_line="$(grep -nF 'docker rm -f team1-vuln-app' "${LOG_FILE}" | head -n 1 | cut -d: -f1)"
up_line="$(
    grep -nF "docker compose -f ${FIXTURE}/teams/generated/team1/example-vuln/docker-compose.yml up" \
        "${LOG_FILE}" | head -n 1 | cut -d: -f1
)"
((remove_line < up_line)) || {
    echo "App container was not removed before nested Compose recreation" >&2
    exit 1
}

: > "${LOG_FILE}"
final_status_output="$(run_arena final-status-fail up --teams 2 --timeout 2)"
grep -Fq "Complete arena is healthy" <<< "${final_status_output}"

status_output="$(run_arena healthy status --format tsv)"
grep -Fq $'team1\tgateway\trunning\t-' <<< "${status_output}"
grep -Fq $'team2\tapp\trunning\thealthy' <<< "${status_output}"
grep -Fq -- $'-\tfirewall\trunning\t-' <<< "${status_output}"
grep -Fq -- $'-\tvisualizer\trunning\thealthy' <<< "${status_output}"

set +e
missing_output="$(run_arena missing-app status --format tsv)"
missing_rc=$?
set -e
((missing_rc != 0)) || {
    echo "Status should fail when an app container is absent" >&2
    exit 1
}
grep -Fq $'team2\tapp\tabsent\tnot-running' <<< "${missing_output}"

set +e
visualizer_output="$(run_arena visualizer-fail status --format tsv)"
visualizer_rc=$?
set -e
((visualizer_rc != 0)) || {
    echo "Status should fail when the visualizer health check fails" >&2
    exit 1
}
grep -Fq -- $'-\tvisualizer\trunning\tunhealthy' <<< "${visualizer_output}"

: > "${LOG_FILE}"
run_arena healthy down >/dev/null
assert_log "docker rm -f team1-vuln-app team2-vuln-app"
assert_log "docker compose -f ${FIXTURE}/docker-compose.yml down --remove-orphans"
if grep -Fq "docker volume rm" "${LOG_FILE}"; then
    echo "down removed a data volume" >&2
    exit 1
fi

: > "${LOG_FILE}"
run_arena healthy restart --timeout 2 >/dev/null
down_line="$(grep -nF "docker compose -f ${FIXTURE}/docker-compose.yml down" "${LOG_FILE}" | head -n 1 | cut -d: -f1)"
restart_up_line="$(grep -nF "docker compose -f ${FIXTURE}/docker-compose.yml up" "${LOG_FILE}" | head -n 1 | cut -d: -f1)"
restart_setup_line="$(grep -nF "setup --remove-orphan-containers" "${LOG_FILE}" | head -n 1 | cut -d: -f1)"
((restart_setup_line < down_line)) || {
    echo "restart did not validate generation before stopping" >&2
    exit 1
}
((down_line < restart_up_line)) || {
    echo "restart did not stop infrastructure before startup" >&2
    exit 1
}
if grep -Fq "docker volume rm" "${LOG_FILE}"; then
    echo "restart removed a data volume" >&2
    exit 1
fi

: > "${LOG_FILE}"
run_arena healthy reset --timeout 2 >/dev/null
assert_log "docker volume rm -f sandcastle_team1-data sandcastle_team2-data"
assert_log "docker compose -f ${FIXTURE}/docker-compose.yml up -d --build --remove-orphans"

set +e
unhealthy_output="$(run_arena health-fail up --timeout 1 2>&1)"
unhealthy_rc=$?
set -e
((unhealthy_rc != 0)) || {
    echo "Unhealthy app startup should have failed" >&2
    exit 1
}
grep -Fq "app health timeout" <<< "${unhealthy_output}"
grep -Fq "team2-vuln-app" <<< "${unhealthy_output}"

source_after="$(
    find "${FIXTURE}/teams/generated" -path '*/app/app.py' -print0 |
        sort -z |
        xargs -0 sha256sum
)"
[[ "${source_before}" == "${source_after}" ]] || {
    echo "Lifecycle commands modified participant source patches" >&2
    exit 1
}

: > "${LOG_FILE}"
set +e
runtime_failure="$(
    run_arena firewall-runtime-fail up --timeout 1 2>&1
)"
runtime_rc=$?
set -e
((runtime_rc != 0)) || {
    echo "Startup should fail when firewall runtime checks fail" >&2
    exit 1
}
grep -Fq "firewall enforcement rule or listeners are inactive" <<< "${runtime_failure}"
grep -Fq "docker compose -f ${FIXTURE}/docker-compose.yml up" "${LOG_FILE}"

: > "${LOG_FILE}"
set +e
smoke_failure="$(
    ARENA_TEST_SMOKE_FAIL=1 run_arena healthy up --timeout 1 2>&1
)"
smoke_rc=$?
set -e
((smoke_rc != 0)) || {
    echo "Startup should fail when the network smoke test fails" >&2
    exit 1
}
grep -Fq "firewall network smoke test failed" <<< "${smoke_failure}"

printf '\nARENA_ISOLATION_MODE=dind\n' >> "${FIXTURE}/config/arena.env"

: > "${LOG_FILE}"
ARENA_TEST_SMOKE_FAIL=1 run_arena healthy up --timeout 1 >/dev/null
if grep -Fq "network-smoke" "${LOG_FILE}"; then
    echo "DinD startup should skip the legacy firewall network smoke" >&2
    exit 1
fi
assert_log "docker exec team1-vuln docker exec team1-vuln-app python3 -c"
if grep -Fq "docker exec team1-vuln curl -fsS --max-time 2 http://127.0.0.1:8080/health" "${LOG_FILE}"; then
    echo "DinD app readiness should not depend on the team machine TCP forwarder" >&2
    exit 1
fi
if grep -Fq "docker exec team1-vuln docker exec team1-vuln-app curl" "${LOG_FILE}"; then
    echo "DinD health checks should not require curl inside the app container" >&2
    exit 1
fi

: > "${LOG_FILE}"
ARENA_TEST_SMOKE_FAIL=1 run_arena dind-forward-fail up --timeout 1 >/dev/null
assert_log "docker exec team1-vuln docker exec team1-vuln-app python3 -c"
if grep -Fq "app health timeout" "${LOG_FILE}"; then
    echo "DinD startup should not fail when the parent forwarder is not ready" >&2
    exit 1
fi

: > "${LOG_FILE}"
set +e
dind_failure="$(
    run_arena dind-compose-fail up --timeout 1 2>&1
)"
dind_rc=$?
set -e
((dind_rc != 0)) || {
    echo "Startup should fail when nested DinD compose fails" >&2
    exit 1
}
grep -Fq "nested DinD compose failed for team1" <<< "${dind_failure}"
grep -Fq "nested compose ps" <<< "${dind_failure}"
grep -Fq "nested compose logs" <<< "${dind_failure}"

echo "arena lifecycle tests: ok"
