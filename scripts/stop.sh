#!/usr/bin/env bash
# Stop the infrastructure scaffold but keep team volumes.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

docker compose down
