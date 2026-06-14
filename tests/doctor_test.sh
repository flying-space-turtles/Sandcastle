#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCTOR="${ROOT}/scripts/doctor.sh"
TMP_ROOT="$(mktemp -d)"
MOCK_BIN="${TMP_ROOT}/bin"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p "${MOCK_BIN}"

cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -u

scenario="${DOCTOR_TEST_SCENARIO:-empty}"

case "${1:-}" in
    --version)
        echo "Docker version 99.0.0, build test"
        exit 0
        ;;
    info)
        if [[ "${2:-}" == "--format" ]]; then
            echo "Mock Linux"
        fi
        exit 0
        ;;
    compose)
        if [[ "${2:-}" == "version" ]]; then
            echo "Docker Compose version v99.0.0"
        fi
        exit 0
        ;;
    ps)
        case "${scenario}" in
            empty)
                ;;
            orphan)
                printf '%s\n' \
                    $'team1-ssh\trunning' \
                    $'team1-vuln\trunning' \
                    $'team1-vuln-app\trunning' \
                    $'team2-ssh\trunning' \
                    $'sandcastle-firewall\trunning'
                ;;
            firewall-zero)
                printf '%s\n' \
                    $'team1-ssh\trunning' \
                    $'team1-vuln\trunning' \
                    $'team1-vuln-app\trunning' \
                    $'sandcastle-firewall\trunning'
                ;;
        esac
        exit 0
        ;;
    network)
        case "${2:-}" in
            ls) exit 0 ;;
            inspect) exit 0 ;;
        esac
        ;;
    inspect)
        echo "bind|true"
        exit 0
        ;;
    exec)
        container="${2:-}"
        shift 2
        if [[ "${container}" == "sandcastle-firewall" && "$*" == *"bridge-nf-call-iptables"* ]]; then
            echo 1
            exit 0
        fi
        if [[ "${container}" == "sandcastle-firewall" && "$*" == *"-S PREROUTING"* ]]; then
            echo "-A PREROUTING -s 10.10.0.0/16 -d 10.10.0.0/16 -p tcp -m comment --comment sandcastle-firewall-transparent-proxy -j REDIRECT --to-ports 15000"
            exit 0
        fi
        if [[ "${container}" == "sandcastle-firewall" && "$*" == *"-L PREROUTING"* ]]; then
            packets=7
            [[ "${scenario}" == "firewall-zero" ]] && packets=0
            printf '1 %s 100 REDIRECT tcp -- * * 10.10.0.0/16 10.10.0.0/16 /* sandcastle-firewall-transparent-proxy */ redir ports 15000\n' "${packets}"
            exit 0
        fi
        if [[ "$*" == *"/health"* ]]; then
            echo '{"status":"ok"}'
        fi
        exit 0
        ;;
esac

exit 0
EOF

cat > "${MOCK_BIN}/ss" <<'EOF'
#!/usr/bin/env bash
case "${DOCTOR_TEST_SCENARIO:-empty}" in
    orphan|firewall-zero)
        printf '%s\n' \
            'LISTEN 0 128 0.0.0.0:2201 0.0.0.0:*' \
            'LISTEN 0 128 0.0.0.0:6789 0.0.0.0:*' \
            'LISTEN 0 128 0.0.0.0:15000 0.0.0.0:*'
        ;;
esac
EOF

cat > "${MOCK_BIN}/sysctl" <<'EOF'
#!/usr/bin/env bash
echo 1
EOF

cat > "${MOCK_BIN}/ip" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat > "${MOCK_BIN}/stat" <<'EOF'
#!/usr/bin/env bash
target="${*: -1}"
if [[ "${target}" == */docker.sock ]]; then
    echo "socket"
    exit 0
fi
exec /usr/bin/stat "$@"
EOF

chmod +x \
    "${MOCK_BIN}/docker" \
    "${MOCK_BIN}/ss" \
    "${MOCK_BIN}/sysctl" \
    "${MOCK_BIN}/ip" \
    "${MOCK_BIN}/stat"

make_fixture() {
    local fixture="$1"
    local workspace_mode="$2"

    mkdir -p \
        "${fixture}/bot" \
        "${fixture}/config" \
        "${fixture}/scripts/lib" \
        "${fixture}/services/example-vuln" \
        "${fixture}/teams/generated/team1/example-vuln/app"

    cp "${ROOT}/scripts/lib/arena_config.sh" "${fixture}/scripts/lib/arena_config.sh"
    cat > "${fixture}/config/arena.env" <<'EOF'
ARENA_TEAM_COUNT=1
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
ARENA_STARTUP_TIMEOUT_SECONDS=120
ARENA_ROUND_DURATION_SECONDS=120
ARENA_FLAG_EXPIRY_ROUNDS=5
EOF

    cat > "${fixture}/docker-compose.yml" <<'EOF'
name: sandcastle

networks:
  ctf-network:
    ipam:
      config:
        - subnet: 10.10.0.0/16

services:
  team1-vuln:
    image: test
  team1-ssh:
    image: test
    ports:
      - "127.0.0.1:2201:22"
  firewall:
    image: test
EOF

    touch "${fixture}/teams/generated/team1/.sandcastle-generated"
    printf '"""test bot api"""\n' > "${fixture}/bot/bot_api.py"
    printf '#!/usr/bin/env bash\nexit 0\n' > "${fixture}/bot/deploy.sh"
    chmod +x "${fixture}/bot/deploy.sh"
    if [[ "${workspace_mode}" == "complete" ]]; then
        printf 'FROM scratch\n' > "${fixture}/teams/generated/team1/example-vuln/Dockerfile"
        printf 'services: {}\n' > "${fixture}/teams/generated/team1/example-vuln/docker-compose.yml"
        printf 'print("test")\n' > "${fixture}/teams/generated/team1/example-vuln/app/app.py"
        printf 'Flask==3.0.3\n' > "${fixture}/teams/generated/team1/example-vuln/app/requirements.txt"
    fi
}

run_doctor() {
    local fixture="$1"
    local scenario="$2"
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${fixture}" \
        DOCTOR_DOCKER_SOCKET="${fixture}/docker.sock" \
        DOCTOR_TEST_SCENARIO="${scenario}" \
        "${DOCTOR}" --format tsv 2>&1 || true
}

assert_status() {
    local output="$1"
    local status="$2"
    local check_id="$3"
    if ! grep -Fq "${status}"$'\t'"${check_id}"$'\t' <<< "${output}"; then
        echo "Expected ${status} for ${check_id}" >&2
        echo "--- doctor output ---" >&2
        echo "${output}" >&2
        exit 1
    fi
}

empty_fixture="${TMP_ROOT}/empty-workspace"
make_fixture "${empty_fixture}" empty
empty_output="$(run_doctor "${empty_fixture}" empty)"
assert_status "${empty_output}" FAIL workspace.completeness

orphan_fixture="${TMP_ROOT}/orphan-team"
make_fixture "${orphan_fixture}" complete
orphan_output="$(run_doctor "${orphan_fixture}" orphan)"
assert_status "${orphan_output}" FAIL runtime.orphans

firewall_fixture="${TMP_ROOT}/firewall-zero"
make_fixture "${firewall_fixture}" complete
firewall_output="$(run_doctor "${firewall_fixture}" firewall-zero)"
assert_status "${firewall_output}" FAIL firewall.traffic

invalid_fixture="${TMP_ROOT}/invalid-config"
make_fixture "${invalid_fixture}" complete
awk '!/^ARENA_SERVICE_PORT=/' "${invalid_fixture}/config/arena.env" \
    > "${invalid_fixture}/config/arena.env.tmp"
mv "${invalid_fixture}/config/arena.env.tmp" "${invalid_fixture}/config/arena.env"
invalid_output="$(run_doctor "${invalid_fixture}" empty)"
assert_status "${invalid_output}" FAIL arena.config
assert_status "${invalid_output}" WARN bot.api

ready_fixture="${TMP_ROOT}/ready"
make_fixture "${ready_fixture}" complete
touch "${ready_fixture}/docker.sock"
before_files="$(
    find "${ready_fixture}" -type f -print0 |
        sort -z |
        xargs -0 sha256sum
)"

set +e
ready_output="$(
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${ready_fixture}" \
        DOCTOR_DOCKER_SOCKET="${ready_fixture}/docker.sock" \
        DOCTOR_TEST_SCENARIO=empty \
        "${DOCTOR}" --format tsv 2>&1
)"
ready_rc=$?
set -e
after_files="$(
    find "${ready_fixture}" -type f -print0 |
        sort -z |
        xargs -0 sha256sum
)"

if ((ready_rc != 0)); then
    echo "Expected a warning-only fixture to exit 0, got ${ready_rc}" >&2
    echo "${ready_output}" >&2
    exit 1
fi
if grep -q $'^FAIL\t' <<< "${ready_output}"; then
    echo "Warning-only fixture unexpectedly reported a failure" >&2
    echo "${ready_output}" >&2
    exit 1
fi
if ! awk -F '\t' 'NF != 4 { exit 1 }' <<< "${ready_output}"; then
    echo "TSV output did not contain exactly four columns per line" >&2
    echo "${ready_output}" >&2
    exit 1
fi
if [[ "${before_files}" != "${after_files}" ]]; then
    echo "Doctor modified files in a read-only fixture" >&2
    diff <(printf '%s\n' "${before_files}") <(printf '%s\n' "${after_files}") >&2 || true
    exit 1
fi

echo "doctor tests: ok"
