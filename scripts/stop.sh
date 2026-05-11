#!/usr/bin/env bash
# Stop the infrastructure scaffold but keep team volumes.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

app_containers="$(docker ps -aq --filter "label=sandcastle.role=vuln-app")"
if [[ -n "${app_containers}" ]]; then
    docker rm -f ${app_containers}
fi

docker compose down
