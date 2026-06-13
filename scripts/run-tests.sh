#!/usr/bin/env bash
# Run every local (fixture-driven) test in dependency order.
#
# Usage:
#   ./scripts/run-tests.sh           # all checks including visualizer build
#   ./scripts/run-tests.sh --fast    # skip the visualizer build
#
# Each step is printed before it runs. On failure the step name is echoed and
# the script exits non-zero. No Docker is required for any step here; for the
# full arena smoke test see:
#   ./tests/integration_test.sh          (fixture mode, called here)
#   ./tests/integration_test.sh          (full Docker, requires a running host)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAST=0
for arg in "$@"; do
    case "${arg}" in
        --fast) FAST=1 ;;
        -h|--help)
            echo "Usage: $0 [--fast]"
            echo "  --fast  Skip the visualizer build step"
            exit 0
            ;;
        *)
            echo "run-tests.sh: unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

step() {
    echo
    echo "────────────────────────────────────────"
    echo "STEP: $*"
    echo "────────────────────────────────────────"
}

ok() { echo "[+] $*"; }

# ---------------------------------------------------------------------------
step "Shell syntax check: scripts and bot helpers"
bash -n \
    "${ROOT}"/scripts/*.sh \
    "${ROOT}"/bot/*.sh \
    "${ROOT}"/tests/*.sh
ok "bash -n: all shell files are syntactically valid"

# ---------------------------------------------------------------------------
step "Python syntax check: core modules"
python3 -B -m py_compile \
    "${ROOT}/scripts/gen_compose.py" \
    "${ROOT}"/bot/*.py \
    "${ROOT}"/bot/bot_lib/*.py \
    "${ROOT}/firewall/firewall.py" \
    "${ROOT}/gameserver/"*.py \
    "${ROOT}/gameserver/checkers/"*.py \
    "${ROOT}/tests/checker_test.py" \
    "${ROOT}/tests/gameserver_test.py" \
    "${ROOT}/tests/scoring_test.py" \
    "${ROOT}/tests/round_engine_test.py" \
    "${ROOT}/services/example-vuln/app/app.py" \
    "${ROOT}/services/example-vuln/checker.py" \
    "${ROOT}/services/example-vuln/exploits/"*.py
ok "py_compile: all Python files are syntactically valid"

# ---------------------------------------------------------------------------
step "Firewall unit tests"
python3 -B "${ROOT}/tests/firewall_test.py"

# ---------------------------------------------------------------------------
step "Gameserver unit tests"
python3 -B "${ROOT}/tests/gameserver_test.py"

# ---------------------------------------------------------------------------
step "Deterministic scoring tests"
python3 -B "${ROOT}/tests/scoring_test.py"

# ---------------------------------------------------------------------------
step "Checker contract and TurtleNotes tests"
python3 -B "${ROOT}/tests/checker_test.py"

# ---------------------------------------------------------------------------
step "Round scheduling and flag lifecycle tests"
python3 -B "${ROOT}/tests/round_engine_test.py"

# ---------------------------------------------------------------------------
step "Firewall host preflight tests"
"${ROOT}/tests/firewall_preflight_test.sh"

# ---------------------------------------------------------------------------
step "Network smoke fixture tests"
"${ROOT}/tests/network_smoke_test.sh"

# ---------------------------------------------------------------------------
step "Doctor tests"
"${ROOT}/tests/doctor_test.sh"

# ---------------------------------------------------------------------------
step "Setup/generation tests"
"${ROOT}/tests/setup_test.sh"

# ---------------------------------------------------------------------------
step "Arena lifecycle tests"
"${ROOT}/tests/arena_test.sh"

# ---------------------------------------------------------------------------
step "Integration test (local fixture mode)"
"${ROOT}/tests/integration_test.sh" --local

# ---------------------------------------------------------------------------
step "Docker Compose config validation"
docker compose -f "${ROOT}/docker-compose.yml" config --quiet
ok "compose config: valid"

# ---------------------------------------------------------------------------
if [[ "${FAST}" == "1" ]]; then
    echo
    echo "Skipping visualizer build (--fast)"
else
    step "Visualizer build"
    (
        cd "${ROOT}/visualizer"
        npm ci --prefer-offline --silent
        npm run build
    )
    ok "visualizer: built successfully"
fi

# ---------------------------------------------------------------------------
echo
echo "════════════════════════════════════════"
echo "  All local checks passed."
echo "════════════════════════════════════════"
echo
echo "To run the full Docker integration test on a native Linux host:"
echo "  sudo ./scripts/firewall-preflight.sh --apply"
echo "  ./tests/integration_test.sh"
