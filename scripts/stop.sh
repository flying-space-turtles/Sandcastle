#!/usr/bin/env bash
# Stop the simulation but keep volumes (so the gameserver DB persists).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

docker compose down
