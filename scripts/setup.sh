#!/usr/bin/env bash
# Setup helper — generates the top-level docker-compose.yml for the requested
# number of teams. Defaults to 3 when no argument is provided.

set -euo pipefail

NUM_TEAMS="${1:-3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 "${ROOT}/scripts/gen_compose.py" "${NUM_TEAMS}"
echo
echo "Next:"
echo "  ./scripts/start.sh        # builds and brings everything up"
echo "  ./scripts/stop.sh         # tears it down"
echo "  ./scripts/reset.sh        # wipes scoreboard data and restarts"
