#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
FIXTURE="${TMP_ROOT}/arena"
MOCK_BIN="${TMP_ROOT}/bin"
COUNTER_FILE="${TMP_ROOT}/counter"
DOCKER_LOG="${TMP_ROOT}/docker.log"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p \
    "${MOCK_BIN}" \
    "${FIXTURE}/config" \
    "${FIXTURE}/scripts/lib" \
    "${FIXTURE}/services/example-vuln"

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
ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS=2
ARENA_FIREWALL_EVENT_QUEUE_SIZE=2048
ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES=4194304
ARENA_FIREWALL_RECENT_ICMP_LIMIT=4096
ARENA_BOT_API_HOST=127.0.0.1
ARENA_BOT_API_PORT=7878
ARENA_BOT_LOOP_SECONDS=60
ARENA_STARTUP_TIMEOUT_SECONDS=120
ARENA_ROUND_DURATION_SECONDS=120
ARENA_FLAG_EXPIRY_ROUNDS=5
EOF

cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -u

printf 'docker %s\n' "$*" >> "${SMOKE_TEST_DOCKER_LOG:?}"

case "${1:-}" in
    info)
        exit 0
        ;;
    inspect)
        echo "running"
        exit 0
        ;;
    exec)
        shift
        if [[ "${1:-}" == "-d" ]]; then
            exit 0
        fi
        container="${1:-}"
        shift
        if [[ "${container}" == "sandcastle-firewall" && "${1:-}" == "sh" ]]; then
            count="$(cat "${SMOKE_TEST_COUNTER:?}")"
            echo "${count}"
            echo $((count + 1)) > "${SMOKE_TEST_COUNTER}"
            exit 0
        fi
        if [[ "${container}" == "sandcastle-firewall" && "${1:-}" == "test" ]]; then
            exit 0
        fi
        if [[ "${container}" == "sandcastle-firewall" && "${1:-}" == "python" ]]; then
            echo '{"srcIp":"10.10.1.3","dstIp":"10.10.2.3","maskedSrcIp":"10.10.0.1"}'
            exit 0
        fi
        if [[ "${container}" == "team2-vuln" ]]; then
            exit 0
        fi
        if [[ "${container}" == "team1-vuln" ]]; then
            if [[ "${SMOKE_TEST_SCENARIO:-healthy}" == "mask-fail" ]]; then
                echo "10.10.1.3"
            else
                echo "10.10.0.1"
            fi
            exit 0
        fi
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
echo 7 > "${COUNTER_FILE}"

output="$(
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${FIXTURE}" \
        SMOKE_TEST_COUNTER="${COUNTER_FILE}" \
        SMOKE_TEST_DOCKER_LOG="${DOCKER_LOG}" \
        "${ROOT}/scripts/smoke-network.sh"
)"

grep -Fq "network smoke: ok" <<< "${output}"
grep -Fq "destination observed: 10.10.0.1" <<< "${output}"
grep -Fq "redirect packets: 7 -> 8" <<< "${output}"
grep -Fq "team1-vuln curl" "${DOCKER_LOG}"

echo 7 > "${COUNTER_FILE}"
set +e
mask_failure="$(
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${FIXTURE}" \
        SMOKE_TEST_COUNTER="${COUNTER_FILE}" \
        SMOKE_TEST_DOCKER_LOG="${DOCKER_LOG}" \
        SMOKE_TEST_SCENARIO=mask-fail \
        "${ROOT}/scripts/smoke-network.sh" 2>&1
)"
mask_rc=$?
set -e
((mask_rc != 0)) || {
    echo "Smoke test should reject an unmasked source" >&2
    exit 1
}
grep -Fq "instead of masked source" <<< "${mask_failure}"

echo "network smoke tests: ok"
