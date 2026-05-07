#!/usr/bin/env bash
# Setup helper - generates the top-level docker-compose.yml for the requested
# number of teams. Defaults to 3 when no argument is provided.

set -euo pipefail

NUM_TEAMS="${1:-3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 "${ROOT}/scripts/gen_compose.py" "${NUM_TEAMS}"
echo
echo "Next:"
echo "  VULN_IMAGE=<image> ./scripts/start.sh"
echo "  ./scripts/stop.sh         # tears it down"
echo "  VULN_IMAGE=<image> ./scripts/reset.sh"
