#!/usr/bin/env bash
# Deploy the current checkout to the disposable Oracle VPS staging arena.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODE="local"
STAGING_DEPLOY_TMP_DIR=""

STAGING_SSH_PORT="${STAGING_SSH_PORT:-22}"
STAGING_DEPLOY_PATH="${STAGING_DEPLOY_PATH:-/opt/sandcastle-staging}"
SANDCASTLE_STAGING_TEAMS="${SANDCASTLE_STAGING_TEAMS:-2}"
SANDCASTLE_STAGING_TIMEOUT="${SANDCASTLE_STAGING_TIMEOUT:-240}"

usage() {
    cat <<'EOF'
Usage: ./scripts/staging-deploy.sh

Deploy the current checkout to the staging VPS over SSH, then run a fresh DinD
smoke deployment remotely. Intended for GitHub Actions.

Required environment:
  STAGING_SSH_HOST
  STAGING_SSH_USER
  STAGING_SSH_PRIVATE_KEY
  STAGING_SSH_KNOWN_HOSTS
  STAGING_OPERATOR_TOKEN
  STAGING_CHECKER_SECRET
  STAGING_TEAM_TOKEN_PATTERN

Optional environment:
  STAGING_SSH_PORT            Default: 22
  STAGING_DEPLOY_PATH         Default: /opt/sandcastle-staging
  SANDCASTLE_STAGING_TEAMS    Default: 2
  SANDCASTLE_STAGING_TIMEOUT  Default: 240
EOF
}

die() {
    echo "staging-deploy.sh: $*" >&2
    exit 1
}

require_env() {
    local name="$1"
    [[ -n "${!name:-}" ]] || die "${name} is required"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

require_int() {
    local name="$1"
    local min="$2"
    local max="$3"
    local value="${!name:-}"

    [[ "${value}" =~ ^[0-9]+$ ]] || die "${name} must be an integer"
    value="$((10#${value}))"
    ((value >= min && value <= max)) ||
        die "${name} must be between ${min} and ${max}"
    printf -v "${name}" '%s' "${value}"
}

shell_quote() {
    local value="$1"
    printf "'%s'" "${value//\'/\'\\\'\'}"
}

validate_common() {
    require_int STAGING_SSH_PORT 1 65535
    require_int SANDCASTLE_STAGING_TEAMS 1 250
    require_int SANDCASTLE_STAGING_TIMEOUT 1 86400
    [[ "${STAGING_DEPLOY_PATH}" == /* ]] ||
        die "STAGING_DEPLOY_PATH must be absolute"
}

failure_report() {
    local rc=$?
    ((rc == 0)) && return

    echo "::group::staging failure diagnostics"
    "${ROOT}/scripts/arena.sh" status --format tsv || true
    docker ps -a \
        --filter "label=sandcastle.role" \
        --filter "label=com.docker.compose.project=sandcastle" || true
    nested_dind_report
    docker compose -f "${ROOT}/docker-compose.yml" logs --no-color --tail=80 || true
    echo "::endgroup::"
}

nested_dind_report() {
    local teams="${SANDCASTLE_STAGING_TEAMS:-2}"
    local id machine username service_dir service_dir_q

    if [[ -f "${ROOT}/scripts/lib/arena_config.sh" && -f "${ROOT}/config/arena.env" ]]; then
        # shellcheck source=scripts/lib/arena_config.sh
        source "${ROOT}/scripts/lib/arena_config.sh"
        if arena_config_load "${ROOT}" >/dev/null 2>&1; then
            teams="${ARENA_TEAM_COUNT}"
        fi
    fi

    echo "--- nested DinD app diagnostics ---"
    for ((id = 1; id <= teams; id++)); do
        machine="team${id}-vuln"
        if [[ "$(docker inspect --format '{{.State.Status}}' "${machine}" 2>/dev/null || true)" != "running" ]]; then
            echo "team${id}: ${machine} is not running; nested diagnostics skipped"
            continue
        fi

        if command -v arena_config_render_team_value >/dev/null 2>&1; then
            username="$(arena_config_render_team_value "${ARENA_TEAM_USERNAME_PATTERN:-team{team}}" "${id}")"
        else
            username="team${id}"
        fi
        service_dir="/home/${username}/example-vuln"
        service_dir_q="$(shell_quote "${service_dir}")"

        echo "--- team${id}: nested docker ps ---"
        docker exec "${machine}" docker ps -a || true
        echo "--- team${id}: nested compose ps ---"
        docker exec "${machine}" sh -lc "cd ${service_dir_q} && docker compose ps || true" || true
        echo "--- team${id}: nested app logs ---"
        docker exec "${machine}" sh -lc "docker logs --tail=160 team${id}-vuln-app || true" || true
        echo "--- team${id}: nested compose logs ---"
        docker exec "${machine}" sh -lc "cd ${service_dir_q} && docker compose logs --no-color --tail=160 || true" || true
    done
}

remote_run() {
    validate_common
    if [[ "${STAGING_DEPLOY_READ_STDIN:-0}" == "1" ]]; then
        local env_file
        env_file="$(mktemp "${TMPDIR:-/tmp}/sandcastle-staging-env.XXXXXX")"
        chmod 0600 "${env_file}"
        cat > "${env_file}"
        set -a
        # shellcheck disable=SC1090
        source "${env_file}"
        set +a
        rm -f "${env_file}"
    fi

    require_env STAGING_OPERATOR_TOKEN
    require_env STAGING_CHECKER_SECRET
    require_env STAGING_TEAM_TOKEN_PATTERN
    require_command docker

    trap failure_report EXIT

    cd "${ROOT}"
    echo "[*] Removing previous Sandcastle staging deployment..."
    ./scripts/cleanup.sh --remove-generated

    echo "[*] Applying host firewall preflight..."
    sudo -n ./scripts/firewall-preflight.sh --apply

    echo "[*] Applying staging arena config..."
    ./scripts/staging-config.sh --teams "${SANDCASTLE_STAGING_TEAMS}"

    echo "[*] Running DinD staging smoke..."
    ./scripts/staging-dind-smoke.sh \
        --teams "${SANDCASTLE_STAGING_TEAMS}" \
        --timeout "${SANDCASTLE_STAGING_TIMEOUT}"

    echo "[*] Final staging status..."
    ./scripts/arena.sh status --format tsv
}

local_deploy() {
    local key_file known_hosts_file remote deploy_path_q ssh_remote_cmd
    local ssh_cmd

    validate_common
    require_env STAGING_SSH_HOST
    require_env STAGING_SSH_USER
    require_env STAGING_SSH_PRIVATE_KEY
    require_env STAGING_SSH_KNOWN_HOSTS
    require_env STAGING_OPERATOR_TOKEN
    require_env STAGING_CHECKER_SECRET
    require_env STAGING_TEAM_TOKEN_PATTERN
    require_command ssh
    require_command rsync

    STAGING_DEPLOY_TMP_DIR="$(mktemp -d)"
    key_file="${STAGING_DEPLOY_TMP_DIR}/staging_key"
    known_hosts_file="${STAGING_DEPLOY_TMP_DIR}/known_hosts"
    cleanup_local() {
        rm -rf "${STAGING_DEPLOY_TMP_DIR}"
    }
    trap cleanup_local EXIT

    printf '%s\n' "${STAGING_SSH_PRIVATE_KEY}" > "${key_file}"
    chmod 0600 "${key_file}"
    printf '%s\n' "${STAGING_SSH_KNOWN_HOSTS}" > "${known_hosts_file}"

    remote="${STAGING_SSH_USER}@${STAGING_SSH_HOST}"
    ssh_cmd="ssh -i ${key_file} -p ${STAGING_SSH_PORT} -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${known_hosts_file}"
    deploy_path_q="$(shell_quote "${STAGING_DEPLOY_PATH}")"

    echo "[*] Preparing remote staging path ${STAGING_DEPLOY_PATH}..."
    ssh \
        -i "${key_file}" \
        -p "${STAGING_SSH_PORT}" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="${known_hosts_file}" \
        "${remote}" \
        "mkdir -p ${deploy_path_q}"

    echo "[*] Syncing checkout to staging VPS..."
    rsync -az --delete --delete-excluded \
        --exclude '.git/' \
        --exclude '.env' \
        --exclude '.env.*' \
        --exclude '*.log' \
        --exclude 'logs/' \
        --exclude 'tmp/' \
        --exclude 'teams/generated/' \
        --exclude 'teams/team*/' \
        --exclude 'visualizer/node_modules/' \
        --exclude 'visualizer/dist/' \
        --exclude '__pycache__/' \
        --exclude '.pytest_cache/' \
        --exclude '.ruff_cache/' \
        -e "${ssh_cmd}" \
        "${ROOT}/" \
        "${remote}:${STAGING_DEPLOY_PATH}/"

    ssh_remote_cmd="cd ${deploy_path_q} && STAGING_DEPLOY_READ_STDIN=1 ./scripts/staging-deploy.sh --remote-run"
    echo "[*] Starting remote staging deployment..."
    {
        printf 'STAGING_OPERATOR_TOKEN=%s\n' "$(shell_quote "${STAGING_OPERATOR_TOKEN}")"
        printf 'STAGING_CHECKER_SECRET=%s\n' "$(shell_quote "${STAGING_CHECKER_SECRET}")"
        printf 'STAGING_TEAM_TOKEN_PATTERN=%s\n' "$(shell_quote "${STAGING_TEAM_TOKEN_PATTERN}")"
        printf 'STAGING_DEPLOY_PATH=%s\n' "$(shell_quote "${STAGING_DEPLOY_PATH}")"
        printf 'SANDCASTLE_STAGING_TEAMS=%s\n' "$(shell_quote "${SANDCASTLE_STAGING_TEAMS}")"
        printf 'SANDCASTLE_STAGING_TIMEOUT=%s\n' "$(shell_quote "${SANDCASTLE_STAGING_TIMEOUT}")"
    } | ssh \
        -i "${key_file}" \
        -p "${STAGING_SSH_PORT}" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="${known_hosts_file}" \
        "${remote}" \
        "${ssh_remote_cmd}"
}

while (($#)); do
    case "$1" in
        --remote-run)
            MODE="remote"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

case "${MODE}" in
    local) local_deploy ;;
    remote) remote_run ;;
esac
