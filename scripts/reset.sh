#!/usr/bin/env bash
# Tear everything down, drop volumes, rebuild, restart.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

docker compose down -v
./scripts/start.sh
