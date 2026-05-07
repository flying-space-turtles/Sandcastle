#!/usr/bin/env bash
# Build images and start the simulation.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if [[ ! -f docker-compose.yml ]]; then
    echo "[*] No docker-compose.yml — running setup with default 3 teams"
    ./scripts/setup.sh 3
fi

echo "[*] Building images…"
docker compose build

echo "[*] Starting simulation…"
docker compose up -d

echo
echo "[+] Up. Useful endpoints:"
echo "    Gameserver REST API:  http://localhost:8080/api/state"
echo "    Per-team SSH:         ssh -p 220<N> ctfuser@localhost  (password team<N>pass)"
echo
echo "[*] Tail gameserver logs with:  docker compose logs -f gameserver"
