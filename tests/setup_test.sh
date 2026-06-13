#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
MOCK_BIN="${TMP_ROOT}/bin"
DOCKER_LOG="${TMP_ROOT}/docker.log"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p "${MOCK_BIN}"

cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -u

case "${1:-}" in
    info)
        exit 0
        ;;
    ps)
        if [[ "${SETUP_TEST_SCENARIO:-empty}" == "orphan" ]]; then
            printf '%s\n' team3-ssh team3-vuln
        fi
        exit 0
        ;;
    rm)
        printf '%s\n' "$*" >> "${SETUP_TEST_DOCKER_LOG:?}"
        exit 0
        ;;
esac

exit 0
EOF
chmod +x "${MOCK_BIN}/docker"

make_fixture() {
    local fixture="$1"
    local teams="${2:-2}"

    mkdir -p \
        "${fixture}/config" \
        "${fixture}/scripts/lib" \
        "${fixture}/gameserver/checkers" \
        "${fixture}/services/example-vuln/app"

    cp "${ROOT}/scripts/setup.sh" "${fixture}/scripts/setup.sh"
    cp "${ROOT}/scripts/lib/arena_config.sh" "${fixture}/scripts/lib/arena_config.sh"
    cp "${ROOT}/gameserver/checker_credentials.py" "${fixture}/gameserver/checker_credentials.py"
    cp "${ROOT}/gameserver/checkers/__init__.py" "${fixture}/gameserver/checkers/__init__.py"
    cp "${ROOT}/gameserver/checkers/contract.py" "${fixture}/gameserver/checkers/contract.py"
    cp "${ROOT}/gameserver/checkers/credentials.py" "${fixture}/gameserver/checkers/credentials.py"
    chmod +x "${fixture}/scripts/setup.sh"

    cat > "${fixture}/config/arena.env" <<EOF
ARENA_TEAM_COUNT=${teams}
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

    printf 'FROM scratch\n' > "${fixture}/services/example-vuln/Dockerfile"
    printf 'CHECKER = object()\n' > "${fixture}/services/example-vuln/checker.py"
    printf 'print("template")\n' > "${fixture}/services/example-vuln/app/app.py"
    printf 'Flask==3.0.3\n' > "${fixture}/services/example-vuln/app/requirements.txt"
}

run_setup() {
    local fixture="$1"
    shift
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${fixture}" \
        SETUP_TEST_DOCKER_LOG="${DOCKER_LOG}" \
        "${fixture}/scripts/setup.sh" "$@"
}

fixture_hashes() {
    local fixture="$1"
    find \
        "${fixture}/config" \
        "${fixture}/docker-compose.yml" \
        "${fixture}/teams/generated" \
        -type f -print0 |
        sort -z |
        xargs -0 sha256sum
}

assert_contains() {
    local haystack="$1"
    local needle="$2"
    if ! grep -Fq "${needle}" <<< "${haystack}"; then
        echo "Expected output to contain: ${needle}" >&2
        echo "--- output ---" >&2
        echo "${haystack}" >&2
        exit 1
    fi
}

deterministic_fixture="${TMP_ROOT}/deterministic"
make_fixture "${deterministic_fixture}" 2
run_setup "${deterministic_fixture}" >/dev/null
before="$(fixture_hashes "${deterministic_fixture}")"
run_setup "${deterministic_fixture}" >/dev/null
after="$(fixture_hashes "${deterministic_fixture}")"
[[ "${before}" == "${after}" ]] || {
    echo "Unchanged generation was not deterministic" >&2
    exit 1
}
grep -Fq 'team_count: 2' "${deterministic_fixture}/docker-compose.yml"
grep -Fq 'checker_max_concurrency: 8' "${deterministic_fixture}/docker-compose.yml"
grep -Fq 'submission_rate_limit: 60' "${deterministic_fixture}/docker-compose.yml"
grep -Fq '2202:22' "${deterministic_fixture}/docker-compose.yml"
team1_service_compose="${deterministic_fixture}/teams/generated/team1/example-vuln/docker-compose.yml"
team2_service_compose="${deterministic_fixture}/teams/generated/team2/example-vuln/docker-compose.yml"
grep -Fq 'CHECKER_USERNAME: "checker_t1_example_vuln"' "${team1_service_compose}"
grep -Fq 'CHECKER_USERNAME: "checker_t2_example_vuln"' "${team2_service_compose}"
team1_plant_token="$(grep 'PLANT_TOKEN:' "${team1_service_compose}")"
team2_plant_token="$(grep 'PLANT_TOKEN:' "${team2_service_compose}")"
[[ "${team1_plant_token}" != "${team2_plant_token}" ]] || {
    echo "Checker plant tokens were not team scoped" >&2
    exit 1
}
if run_setup "${deterministic_fixture}" | grep -Fq 'team1pass'; then
    echo "Default setup output exposed development credentials" >&2
    exit 1
fi
access_output="$(run_setup "${deterministic_fixture}" --show-access)"
assert_contains "${access_output}" "ssh -p 2201 team1@localhost"
assert_contains "${access_output}" "Password:     team1pass"
assert_contains "${access_output}" "API token:    sandcastle-team1-submission-token-change-me"
assert_contains "${access_output}" "ws://localhost:6789"

printf 'print("participant patch")\n' \
    > "${deterministic_fixture}/teams/generated/team1/example-vuln/app/app.py"
rm -f \
    "${deterministic_fixture}/teams/generated/team1/example-vuln/Dockerfile" \
    "${deterministic_fixture}/teams/generated/team1/example-vuln/app/requirements.txt"
repair_output="$(run_setup "${deterministic_fixture}" 2>&1)"
assert_contains "${repair_output}" "Repairing missing files"
grep -Fq 'participant patch' \
    "${deterministic_fixture}/teams/generated/team1/example-vuln/app/app.py"
test -s "${deterministic_fixture}/teams/generated/team1/example-vuln/Dockerfile"
test -s "${deterministic_fixture}/teams/generated/team1/example-vuln/app/requirements.txt"

unmarked_fixture="${TMP_ROOT}/unmarked"
make_fixture "${unmarked_fixture}" 1
mkdir -p "${unmarked_fixture}/teams/generated/team1/example-vuln/app"
printf 'print("participant patch")\n' \
    > "${unmarked_fixture}/teams/generated/team1/example-vuln/app/app.py"
set +e
unmarked_output="$(run_setup "${unmarked_fixture}" 2>&1)"
unmarked_rc=$?
set -e
((unmarked_rc != 0)) || {
    echo "Unmarked partial workspace should have been rejected" >&2
    exit 1
}
assert_contains "${unmarked_output}" "participant-owned"

overwrite_output="$(run_setup "${unmarked_fixture}" --overwrite-services 2>&1)"
assert_contains "${overwrite_output}" "DESTRUCTIVE MODE ENABLED"
grep -Fq 'print("template")' \
    "${unmarked_fixture}/teams/generated/team1/example-vuln/app/app.py"
test -f "${unmarked_fixture}/teams/generated/team1/.sandcastle-generated"

orphan_fixture="${TMP_ROOT}/orphan"
make_fixture "${orphan_fixture}" 3
run_setup "${orphan_fixture}" >/dev/null
set +e
orphan_output="$(
    SETUP_TEST_SCENARIO=orphan run_setup "${orphan_fixture}" --teams 2 2>&1
)"
orphan_rc=$?
set -e
((orphan_rc != 0)) || {
    echo "Reducing teams with stale containers should have failed" >&2
    exit 1
}
assert_contains "${orphan_output}" "stale team containers"
grep -Fq 'ARENA_TEAM_COUNT=3' "${orphan_fixture}/config/arena.env"

allow_output="$(
    SETUP_TEST_SCENARIO=orphan \
        run_setup "${orphan_fixture}" --teams 2 --allow-orphan-containers 2>&1
)"
assert_contains "${allow_output}" "explicitly allowed orphan containers"
grep -Fq 'ARENA_TEAM_COUNT=2' "${orphan_fixture}/config/arena.env"
test ! -e "${orphan_fixture}/teams/generated/team3"

remove_fixture="${TMP_ROOT}/remove-orphan"
make_fixture "${remove_fixture}" 3
run_setup "${remove_fixture}" >/dev/null
: > "${DOCKER_LOG}"
remove_output="$(
    SETUP_TEST_SCENARIO=orphan \
        run_setup "${remove_fixture}" --teams 2 --remove-orphan-containers 2>&1
)"
assert_contains "${remove_output}" "DESTRUCTIVE: removing stale containers"
grep -Fq 'rm -f team3-ssh team3-vuln' "${DOCKER_LOG}"

collision_fixture="${TMP_ROOT}/port-collision"
make_fixture "${collision_fixture}" 1
sed -i 's/^ARENA_SSH_BASE_PORT=.*/ARENA_SSH_BASE_PORT=7800/' \
    "${collision_fixture}/config/arena.env"
set +e
collision_output="$(run_setup "${collision_fixture}" --teams 100 2>&1)"
collision_rc=$?
set -e
((collision_rc != 0)) || {
    echo "Expanded SSH range collision should have been rejected" >&2
    exit 1
}
assert_contains "${collision_output}" "collides with the configured team SSH range"

echo "setup tests: ok"
