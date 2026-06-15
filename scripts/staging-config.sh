#!/usr/bin/env bash
# Apply staging-only arena configuration from environment variables.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG_FILE="${SANDCASTLE_ARENA_CONFIG:-${ROOT}/config/arena.env}"
TEAMS="${SANDCASTLE_STAGING_TEAMS:-2}"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

usage() {
    cat <<'EOF'
Usage: ./scripts/staging-config.sh [--teams N]

Apply staging-only values to config/arena.env. Values are read from:
  STAGING_OPERATOR_TOKEN
  STAGING_CHECKER_SECRET
  STAGING_TEAM_TOKEN_PATTERN

Options:
  --teams N      Team count for staging, default SANDCASTLE_STAGING_TEAMS or 2
  -h, --help     Show this help text
EOF
}

die() {
    echo "staging-config.sh: $*" >&2
    exit 1
}

require_env() {
    local name="$1"
    [[ -n "${!name:-}" ]] || die "${name} is required"
}

while (($#)); do
    case "$1" in
        --teams)
            [[ $# -ge 2 ]] || die "--teams requires a value"
            TEAMS="$2"
            shift 2
            ;;
        --teams=*)
            TEAMS="${1#*=}"
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

[[ -f "${CONFIG_FILE}" ]] || die "missing arena config: ${CONFIG_FILE}"
arena_config_require_int TEAMS 1 250 || die "invalid --teams value"

require_env STAGING_OPERATOR_TOKEN
require_env STAGING_CHECKER_SECRET
require_env STAGING_TEAM_TOKEN_PATTERN

arena_config_validate_simple_value STAGING_OPERATOR_TOKEN "${STAGING_OPERATOR_TOKEN}" ||
    die "invalid STAGING_OPERATOR_TOKEN"
arena_config_validate_simple_value STAGING_CHECKER_SECRET "${STAGING_CHECKER_SECRET}" ||
    die "invalid STAGING_CHECKER_SECRET"
arena_config_validate_simple_value STAGING_TEAM_TOKEN_PATTERN "${STAGING_TEAM_TOKEN_PATTERN}" ||
    die "invalid STAGING_TEAM_TOKEN_PATTERN"

arena_config_set_key "${CONFIG_FILE}" ARENA_ISOLATION_MODE dind
arena_config_set_key "${CONFIG_FILE}" ARENA_TEAM_COUNT "${TEAMS}"
arena_config_set_key "${CONFIG_FILE}" ARENA_OPERATOR_TOKEN "${STAGING_OPERATOR_TOKEN}"
arena_config_set_key "${CONFIG_FILE}" ARENA_CHECKER_SECRET "${STAGING_CHECKER_SECRET}"
arena_config_set_key "${CONFIG_FILE}" ARENA_TEAM_TOKEN_PATTERN "${STAGING_TEAM_TOKEN_PATTERN}"

ARENA_CONFIG_SILENT=1 arena_config_load "${ROOT}" ||
    die "staging arena config failed validation"

echo "[+] Staging arena configuration applied."
