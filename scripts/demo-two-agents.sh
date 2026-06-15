#!/usr/bin/env bash
# scripts/demo-two-agents.sh
#
# AI-018: Sandcastle two-agent demonstration script.
#
# Usage:
#   ./scripts/demo-two-agents.sh --check     # prerequisite check only
#   ./scripts/demo-two-agents.sh --fake      # full demo, fake provider (no cost)
#   ./scripts/demo-two-agents.sh --real      # opt-in: requires OPENAI_API_KEY or GEMINI_API_KEY
#
# Default is --fake (no API cost).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[1;34m'
BOLD='\033[1m'; NC='\033[0m'

mode="--fake"
for arg in "$@"; do
  case $arg in
    --check|--fake|--real) mode="$arg" ;;
  esac
done

header() { echo -e "\n${BOLD}${BLUE}══════════════════════════════════════════════${NC}"; echo -e "${BOLD}  $1${NC}"; echo -e "${BOLD}${BLUE}══════════════════════════════════════════════${NC}\n"; }
ok()     { echo -e "  ${GREEN}✓${NC}  $1"; }
warn()   { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail()   { echo -e "  ${RED}✗${NC}  $1"; }
info()   { echo -e "  ${BLUE}→${NC}  $1"; }

header "Sandcastle Two-Agent Demo"
echo "  This script demonstrates the two required AI agents:"
echo "  • ChallengeGeneratorAgent — generates and publishes CTF challenges"
echo "  • AttackDefenseAgent       — attacks opponents, defends own service"
echo ""
echo "  Mode: ${BOLD}$mode${NC}"
echo ""

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
header "Prerequisites"

if command -v python3 &>/dev/null; then
  PYVER=$(python3 --version 2>&1)
  ok "Python: $PYVER"
else
  fail "python3 not found" && exit 1
fi

cd "$ROOT"

# Check bot_lib importable
if python3 -B -c "import sys; sys.path.insert(0,'bot'); import bot_lib" 2>/dev/null; then
  ok "bot_lib importable"
else
  fail "bot_lib not importable — run from repository root"
  exit 1
fi

if [ "$mode" == "--real" ]; then
  if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${GEMINI_API_KEY:-}" ]; then
    fail "No API key found: set OPENAI_API_KEY or GEMINI_API_KEY"
    exit 1
  fi
  COST_LIMIT="Max cost: \$0.10 total (enforced by BudgetPolicy)"
  warn "Real-model mode — provider calls will be made."
  warn "$COST_LIMIT"
  echo ""
  read -rp "  Type 'yes' to continue with real model calls: " confirm
  [[ "$confirm" == "yes" ]] || { info "Aborted."; exit 0; }
else
  ok "Fake provider mode — no API cost, no network calls"
fi

[ "$mode" == "--check" ] && { echo ""; ok "All checks passed."; exit 0; }

# ---------------------------------------------------------------------------
# Step 1: Challenge Generator Agent
# ---------------------------------------------------------------------------
header "Step 1 — ChallengeGeneratorAgent"

info "Running challenge generator fixture test..."
python3 -B "$ROOT/tests/challenge_generator_agent_test.py" 2>&1 | \
  grep -E "^(Ran|OK|FAIL|ERROR|\.)" | head -5
ok "ChallengeGeneratorAgent: fake provider loop passed"

info "Rendering a sample challenge deterministically..."
python3 -B - <<'PYEOF'
import sys, json
sys.path.insert(0, 'bot')
from bot_lib.agent_contracts import ChallengeSpec
from bot_lib.challenge_renderer import render
import tempfile, pathlib

spec = ChallengeSpec(seed=42, vulnerability="path_traversal", difficulty="medium")
with tempfile.TemporaryDirectory() as d:
    candidate = render(spec, staging_root=pathlib.Path(d))
    print(f"  Render ID : {candidate.render_id}")
    print(f"  Files     : {list(candidate.file_digests.keys())[:4]} …")
    print(f"  Digest[0] : {list(candidate.file_digests.values())[0][:16]}…")
PYEOF
ok "Deterministic render complete (same spec = same bytes every time)"

# ---------------------------------------------------------------------------
# Step 2: Attack Defense Agent
# ---------------------------------------------------------------------------
header "Step 2 — AttackDefenseAgent"

info "Running attack/defense fixture tests..."
python3 -B "$ROOT/tests/attack_defense_agent_test.py" 2>&1 | \
  grep -E "^(Ran|OK|FAIL|ERROR|\.)" | head -5
ok "AttackDefenseAgent: full attack + defense sequences passed"

# ---------------------------------------------------------------------------
# Step 3: Two-agent e2e proof
# ---------------------------------------------------------------------------
header "Step 3 — Two-Agent End-to-End (AI-016)"

info "Running two-agent deterministic e2e test..."
python3 -B "$ROOT/tests/two_agent_e2e_test.py" -v 2>&1 | \
  grep -E "(test_|OK|FAIL|ERROR|Ran)"
ok "Distinct agent identities proved via telemetry"
ok "ChallengeGenerator published — AttackDefenseAgent captured flag + committed patch"

# ---------------------------------------------------------------------------
# Step 4: Full test suite
# ---------------------------------------------------------------------------
header "Step 4 — Full Test Suite"
info "Running all tests..."
bash "$ROOT/scripts/run-tests.sh" --fast 2>&1 | tail -8
ok "All tests passed"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
header "Summary"
echo "  Two agents demonstrated:"
echo ""
echo "  ${BOLD}ChallengeGeneratorAgent${NC}"
echo "    • Deterministically generates Flask challenge services"
echo "    • Validates vulnerable state + patched state in isolation"
echo "    • Publishes immutable challenge artifacts with provenance"
echo ""
echo "  ${BOLD}AttackDefenseAgent${NC}"
echo "    • Selects and executes typed attack tools (recon/exploit/submit)"
echo "    • Defends own service via transactional patch workflow"
echo "    • Self-attack and cross-team defense structurally prevented"
echo ""
echo "  ${BOLD}Both agents:${NC}"
echo "    • Use the same provider-neutral model gateway"
echo "    • Persist structured telemetry with distinct identities"
echo "    • Respect hard per-call/per-run/per-day cost limits"
echo "    • Default mode incurred \$0.00 (fake provider)"
echo ""
ok "Demo complete ✓"
