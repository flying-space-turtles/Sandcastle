#!/usr/bin/env bash
# Tear everything down, drop volumes, rebuild, restart.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

app_containers="$(docker ps -aq --filter "label=sandcastle.role=vuln-app")"
if [[ -n "${app_containers}" ]]; then
    docker rm -f ${app_containers}
fi

docker compose down -v

app_volumes="$(docker volume ls -q --filter "label=sandcastle.role=vuln-data")"
if [[ -n "${app_volumes}" ]]; then
    docker volume rm ${app_volumes}
fi

./scripts/start.sh
