#!/usr/bin/env bash
# Run every local check and fixture-driven test in dependency order.
#
# Usage:
#   ./scripts/run-tests.sh           # all checks including visualizer build
#   ./scripts/run-tests.sh --fast    # skip the visualizer build
#
# Each step is printed before it runs. On failure the step name is echoed and
# the script exits non-zero. The Docker CLI is used for Compose parsing, but no
# running Docker daemon is required. For the full arena smoke test see:
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
    "${ROOT}"/scripts/lib/*.sh \
    "${ROOT}"/bot/*.sh \
    "${ROOT}"/tests/*.sh
ok "bash -n: all shell files are syntactically valid"

# ---------------------------------------------------------------------------
step "ShellCheck: all tracked shell scripts"
if ! command -v shellcheck >/dev/null 2>&1; then
    echo "run-tests.sh: shellcheck is required" >&2
    exit 1
fi
mapfile -t shell_files < <(git -C "${ROOT}" ls-files '*.sh')
shellcheck -x -P "${ROOT}/scripts/lib" "${shell_files[@]/#/${ROOT}/}"
ok "shellcheck: all tracked shell scripts passed"

# ---------------------------------------------------------------------------
step "Python syntax check: all tracked modules"
mapfile -t python_files < <(git -C "${ROOT}" ls-files '*.py')
python3 -B -m py_compile "${python_files[@]/#/${ROOT}/}"
ok "py_compile: all tracked Python files are syntactically valid"

# ---------------------------------------------------------------------------
step "Python formatting and lint"
if ! command -v ruff >/dev/null 2>&1; then
    echo "run-tests.sh: ruff is required; install requirements-dev.txt" >&2
    exit 1
fi
ruff format --check "${ROOT}"
ruff check "${ROOT}"
ok "ruff: all Python source passed formatting and lint checks"

# ---------------------------------------------------------------------------
step "Bot configuration tests"
python3 -B "${ROOT}/tests/bot_config_test.py"

# ---------------------------------------------------------------------------
step "AI agent contract and configuration tests"
python3 -B "${ROOT}/tests/agent_contracts_test.py"

# ---------------------------------------------------------------------------
step "Bot planner tests"
python3 -B "${ROOT}/tests/planners_test.py"

# ---------------------------------------------------------------------------
step "Bot action tests"
python3 -B "${ROOT}/tests/actions_test.py"

# ---------------------------------------------------------------------------
step "Bot API validation tests"
python3 -B "${ROOT}/tests/bot_api_test.py"

# ---------------------------------------------------------------------------
step "Bot runtime, submission, and telemetry tests"
python3 -B "${ROOT}/tests/bot_test.py"

# ---------------------------------------------------------------------------
step "Model-backed planner tests"
python3 -B "${ROOT}/tests/model_planner_test.py"

# ---------------------------------------------------------------------------
step "Provider-neutral model gateway tests"
python3 -B "${ROOT}/tests/model_gateway_test.py"

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
step "Telemetry storage and redaction tests"
python3 -B "${ROOT}/tests/telemetry_test.py"

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
step "Docker Compose config validation: committed and generated variants"
"${ROOT}/scripts/validate-compose.sh"

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
