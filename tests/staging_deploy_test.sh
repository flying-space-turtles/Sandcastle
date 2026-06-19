#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
MOCK_BIN="${TMP_ROOT}/bin"
LOG_FILE="${TMP_ROOT}/commands.log"
PAYLOAD_FILE="${TMP_ROOT}/payload.env"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p "${MOCK_BIN}"

cat > "${MOCK_BIN}/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'ssh %s\n' "$*" >> "${STAGING_TEST_LOG:?}"
if [[ "$*" == *"STAGING_DEPLOY_READ_STDIN=1"* ]]; then
    cat > "${STAGING_TEST_PAYLOAD:?}"
fi
EOF

cat > "${MOCK_BIN}/rsync" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'rsync %s\n' "$*" >> "${STAGING_TEST_LOG:?}"
EOF

cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'docker %s\n' "$*" >> "${STAGING_TEST_LOG:?}"

case "${1:-} ${2:-}" in
    "ps -aq")
        if [[ "$*" == *"name=^/team[0-9]+-(vuln|ssh|vuln-app|dind)$"* ]]; then
            printf '%s\n' team1-dind team1-vuln team1-vuln-app
        elif [[ "$*" == *"name=^/sandcastle-(monitor|firewall|gameserver|bot-controller|visualizer)$"* ]]; then
            printf '%s\n' sandcastle-gameserver sandcastle-bot-controller sandcastle-visualizer
        fi
        ;;
    "volume ls")
        if [[ "$*" == *"name=^sandcastle_team[0-9]+-dind-(data|run)$"* ]]; then
            printf '%s\n' sandcastle_team1-dind-data sandcastle_team1-dind-run
        elif [[ "$*" == *"name=^sandcastle_team[0-9]+-data$"* ]]; then
            printf '%s\n' sandcastle_team1-data
        fi
        ;;
    "network ls")
        if [[ "$*" == *"name=^sandcastle_ctf-network$"* ]]; then
            printf '%s\n' sandcastle_ctf-network
        fi
        ;;
    "image ls")
        printf '%s\n' image-a image-b
        ;;
esac
EOF

chmod +x "${MOCK_BIN}/ssh" "${MOCK_BIN}/rsync" "${MOCK_BIN}/docker"

assert_file_contains() {
    local file="$1"
    local needle="$2"
    if ! grep -Fq -- "${needle}" "${file}"; then
        echo "Expected ${file} to contain: ${needle}" >&2
        echo "--- ${file} ---" >&2
        cat "${file}" >&2
        exit 1
    fi
}

assert_file_not_contains() {
    local file="$1"
    local needle="$2"
    if grep -Fq -- "${needle}" "${file}"; then
        echo "Did not expect ${file} to contain: ${needle}" >&2
        echo "--- ${file} ---" >&2
        cat "${file}" >&2
        exit 1
    fi
}

workflow="${ROOT}/.github/workflows/ci.yml"
assert_file_contains "${workflow}" "types: [opened, labeled, synchronize, reopened, ready_for_review]"
assert_file_contains "${workflow}" "contains(github.event.pull_request.labels.*.name, 'deploy:staging')"
assert_file_contains "${workflow}" "github.event.pull_request.head.repo.full_name == github.repository"
assert_file_contains "${workflow}" "ref: \${{ github.event.pull_request.head.sha }}"
assert_file_contains "${workflow}" "OPENAI_API_KEY: \${{ secrets.OPENAI_API_KEY }}"
assert_file_contains "${workflow}" "GEMINI_API_KEY: \${{ secrets.GEMINI_API_KEY }}"

: > "${LOG_FILE}"
PATH="${MOCK_BIN}:${PATH}" \
    STAGING_TEST_LOG="${LOG_FILE}" \
    STAGING_TEST_PAYLOAD="${PAYLOAD_FILE}" \
    STAGING_SSH_HOST="staging.example.test" \
    STAGING_SSH_USER="deploy" \
    STAGING_SSH_PRIVATE_KEY="fake-private-key" \
    STAGING_SSH_KNOWN_HOSTS="staging.example.test ssh-ed25519 AAAATEST" \
    STAGING_OPERATOR_TOKEN="operator-token-1234567890abcdef" \
    STAGING_CHECKER_SECRET="checker-secret-1234567890" \
    STAGING_TEAM_TOKEN_PATTERN="team-{team}-token-1234567890abcdef" \
    OPENAI_API_KEY="sk-test-openai-key-1234567890" \
    GEMINI_API_KEY="gemini-test-key-1234567890" \
    STAGING_DEPLOY_PATH="/srv/sandcastle-staging" \
    SANDCASTLE_STAGING_TEAMS="3" \
    SANDCASTLE_STAGING_TIMEOUT="321" \
    "${ROOT}/scripts/staging-deploy.sh"

assert_file_contains "${LOG_FILE}" "ssh -i"
assert_file_contains "${LOG_FILE}" "mkdir -p '/srv/sandcastle-staging'"
assert_file_contains "${LOG_FILE}" "rsync -az --delete"
assert_file_not_contains "${LOG_FILE}" "--delete-excluded"
assert_file_contains "${LOG_FILE}" "--exclude .sandcastle/"
assert_file_contains "${LOG_FILE}" "--exclude challenges/"
assert_file_contains "${LOG_FILE}" "--exclude teams/generated/"
assert_file_contains "${LOG_FILE}" "--exclude visualizer/node_modules/"
assert_file_contains "${LOG_FILE}" "deploy@staging.example.test:/srv/sandcastle-staging/"
assert_file_contains "${LOG_FILE}" "STAGING_DEPLOY_READ_STDIN=1 ./scripts/staging-deploy.sh --remote-run"
assert_file_contains "${PAYLOAD_FILE}" "SANDCASTLE_STAGING_TEAMS='3'"
assert_file_contains "${PAYLOAD_FILE}" "SANDCASTLE_STAGING_TIMEOUT='321'"
assert_file_contains "${PAYLOAD_FILE}" "OPENAI_API_KEY='sk-test-openai-key-1234567890'"
assert_file_contains "${PAYLOAD_FILE}" "GEMINI_API_KEY='gemini-test-key-1234567890'"
assert_file_not_contains "${LOG_FILE}" "operator-token-1234567890abcdef"
assert_file_not_contains "${LOG_FILE}" "sk-test-openai-key-1234567890"
assert_file_not_contains "${LOG_FILE}" "gemini-test-key-1234567890"

config_fixture="${TMP_ROOT}/config-fixture"
mkdir -p "${config_fixture}/config" "${config_fixture}/scripts/lib" "${config_fixture}/scripts" "${config_fixture}/services/example-vuln"
cp "${ROOT}/scripts/lib/arena_config.sh" "${config_fixture}/scripts/lib/arena_config.sh"
cp "${ROOT}/scripts/staging-config.sh" "${config_fixture}/scripts/staging-config.sh"
chmod +x "${config_fixture}/scripts/staging-config.sh"
cat > "${config_fixture}/config/arena.env" <<'EOF'
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
ARENA_STARTUP_TIMEOUT_SECONDS=120
ARENA_ROUND_DURATION_SECONDS=120
ARENA_FLAG_EXPIRY_ROUNDS=5
ARENA_CHECKER_MAX_CONCURRENCY=8
ARENA_GAMESERVER_PORT=8000
ARENA_OPERATOR_TOKEN=sandcastle-local-operator-token-change-me
ARENA_SUBMISSION_RATE_LIMIT=60
ARENA_SUBMISSION_RATE_WINDOW_SECONDS=60
ARENA_SCORE_ATTACK_POINTS=10
ARENA_SCORE_DEFENSE_POINTS=2
ARENA_SCORE_SLA_POINTS=1
ARENA_CHECKER_SECRET=sandcastle-local-checker-secret-change-me
ARENA_ISOLATION_MODE=trusted
EOF
touch "${config_fixture}/services/example-vuln/.keep"

SANDCASTLE_ROOT="${config_fixture}" \
    STAGING_OPERATOR_TOKEN="operator-token-abcdef1234567890" \
    STAGING_CHECKER_SECRET="checker-secret-abcdef123456" \
    STAGING_TEAM_TOKEN_PATTERN="staging-team-{team}-token-abcdef123456" \
    "${config_fixture}/scripts/staging-config.sh" --teams 4 >/dev/null

assert_file_contains "${config_fixture}/config/arena.env" "ARENA_TEAM_COUNT=4"
assert_file_contains "${config_fixture}/config/arena.env" "ARENA_ISOLATION_MODE=dind"
assert_file_contains "${config_fixture}/config/arena.env" "ARENA_OPERATOR_TOKEN=operator-token-abcdef1234567890"
assert_file_contains "${config_fixture}/config/arena.env" "ARENA_CHECKER_SECRET=checker-secret-abcdef123456"
assert_file_contains "${config_fixture}/config/arena.env" "ARENA_TEAM_TOKEN_PATTERN=staging-team-{team}-token-abcdef123456"

set +e
invalid_output="$(
    SANDCASTLE_ROOT="${config_fixture}" \
        STAGING_OPERATOR_TOKEN="bad;token" \
        STAGING_CHECKER_SECRET="checker-secret-abcdef123456" \
        STAGING_TEAM_TOKEN_PATTERN="staging-team-{team}-token-abcdef123456" \
        "${config_fixture}/scripts/staging-config.sh" --teams 4 2>&1
)"
invalid_rc=$?
set -e
((invalid_rc != 0)) || {
    echo "staging-config should reject shell metacharacters" >&2
    exit 1
}
grep -Fq "unsupported characters" <<< "${invalid_output}" || {
    echo "staging-config did not report unsupported characters" >&2
    echo "${invalid_output}" >&2
    exit 1
}

cleanup_fixture="${TMP_ROOT}/cleanup-fixture"
mkdir -p "${cleanup_fixture}/scripts" "${cleanup_fixture}/teams/generated/team1/example-vuln"
cp "${ROOT}/scripts/cleanup.sh" "${cleanup_fixture}/scripts/cleanup.sh"
chmod +x "${cleanup_fixture}/scripts/cleanup.sh"
printf 'generated\n' > "${cleanup_fixture}/teams/generated/team1/example-vuln/file.txt"

: > "${LOG_FILE}"
PATH="${MOCK_BIN}:${PATH}" \
    STAGING_TEST_LOG="${LOG_FILE}" \
    "${cleanup_fixture}/scripts/cleanup.sh" --remove-generated >/dev/null

assert_file_contains "${LOG_FILE}" "docker rm -f"
assert_file_contains "${LOG_FILE}" "team1-dind"
assert_file_contains "${LOG_FILE}" "sandcastle-gameserver"
assert_file_contains "${LOG_FILE}" "sandcastle-visualizer"
assert_file_contains "${LOG_FILE}" "docker volume rm -f"
assert_file_contains "${LOG_FILE}" "sandcastle_team1-dind-data"
assert_file_contains "${LOG_FILE}" "sandcastle_team1-dind-run"
assert_file_contains "${LOG_FILE}" "docker image rm -f"
test ! -e "${cleanup_fixture}/teams/generated" || {
    echo "cleanup --remove-generated did not remove generated workspaces" >&2
    exit 1
}
