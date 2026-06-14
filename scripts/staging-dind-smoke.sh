#!/usr/bin/env bash
# Production-like DinD smoke test for a native Linux Docker host.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TEAMS="${SANDCASTLE_STAGING_TEAMS:-2}"
TIMEOUT="${SANDCASTLE_STAGING_TIMEOUT:-240}"
PHASE="initializing"
PHASE_FILE="${SANDCASTLE_STAGING_PHASE_FILE:-}"

set_phase() {
    PHASE="$1"
    if [[ -n "${PHASE_FILE}" ]]; then
        printf '%s\n' "${PHASE}" > "${PHASE_FILE}"
    fi
}

report_failure_phase() {
    local rc=$?
    ((rc == 0)) && return
    echo "[!] [staging-smoke] FAILED during: ${PHASE}" >&2
}
trap report_failure_phase EXIT

usage() {
    cat <<'EOF'
Usage: ./scripts/staging-dind-smoke.sh [--teams N] [--timeout SEC]

Runs a production-oriented Docker-in-Docker smoke test on the current Docker
host. Intended for a disposable cloud VM or self-hosted CI runner.

Environment:
  SANDCASTLE_STAGING_TEAMS      Team count, default 2.
  SANDCASTLE_STAGING_TIMEOUT    Arena startup timeout, default 240.
EOF
}

while (($#)); do
    case "$1" in
        --teams)
            [[ $# -ge 2 ]] || {
                echo "staging-dind-smoke.sh: --teams requires a value" >&2
                exit 2
            }
            TEAMS="$2"
            shift 2
            ;;
        --teams=*)
            TEAMS="${1#*=}"
            shift
            ;;
        --timeout)
            [[ $# -ge 2 ]] || {
                echo "staging-dind-smoke.sh: --timeout requires a value" >&2
                exit 2
            }
            TIMEOUT="$2"
            shift 2
            ;;
        --timeout=*)
            TIMEOUT="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "staging-dind-smoke.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

set_phase "generating DinD topology"
echo "[*] [staging-smoke] Generating DinD topology..."
"${ROOT}/scripts/setup.sh" --teams "${TEAMS}" --dind --remove-orphan-containers
set_phase "checking firewall preflight"
echo "[*] [staging-smoke] Checking firewall preflight..."
"${ROOT}/scripts/firewall-preflight.sh" --check
set_phase "running doctor before startup"
echo "[*] [staging-smoke] Running doctor before startup..."
"${ROOT}/scripts/doctor.sh"
set_phase "starting disposable DinD arena"
echo "[*] [staging-smoke] Starting disposable DinD arena..."
"${ROOT}/scripts/arena.sh" reset --timeout "${TIMEOUT}"
set_phase "running DinD isolation test"
echo "[*] [staging-smoke] Running DinD isolation test..."
"${ROOT}/tests/dind_isolation_test.sh"
set_phase "running full integration test"
echo "[*] [staging-smoke] Running full integration test..."
"${ROOT}/tests/integration_test.sh"
