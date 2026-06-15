#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${SANDCASTLE_ALLOW_REAL_MODEL:-0}" != "1" ]]; then
    echo "Refusing a real provider call." >&2
    echo "Set SANDCASTLE_ALLOW_REAL_MODEL=1 and a cost limit of at most 0.02." >&2
    exit 2
fi

max_cost="${ARENA_AGENT_MAX_COST_USD_PER_CALL:-0.02}"
awk -v cost="${max_cost}" 'BEGIN { exit !(cost <= 0.02) }' || {
    echo "ARENA_AGENT_MAX_COST_USD_PER_CALL must be at most 0.02" >&2
    exit 2
}

if [[ -f "${ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT}/.env"
    set +a
fi

echo "Running one OpenAI planning call with maximum reserved cost: \$${max_cost}"
PYTHONPATH="${ROOT}/bot" \
    ARENA_AGENT_MAX_COST_USD_PER_CALL="${max_cost}" \
    python3 -B "${ROOT}/bot/openai_smoke.py"
