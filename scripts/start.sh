#!/usr/bin/env bash
# Build images and start the infrastructure scaffold.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

if [[ ! -f docker-compose.yml ]]; then
    echo "[*] No docker-compose.yml - running setup with default 3 teams"
    ./scripts/setup.sh --teams 3
fi

echo "[*] Building team images..."
docker compose build

echo "[*] Starting infrastructure..."
docker compose up -d

echo
echo "[+] Up. Useful access points:"
echo "    Per-team SSH:          ssh -p <2200+N> team<N>@localhost  (password team<N>pass)"
echo "    Vulnerable app slots:  team<N>-vuln at 10.10.<N>.3"
echo "    Inside team SSH box:   curl http://team<N>-vuln:8080/health"
echo "                           cd ~/service && ls   # source for patching"
echo
echo "[*] Inspect containers with:  docker compose ps"
