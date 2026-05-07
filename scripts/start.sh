#!/usr/bin/env bash
# Build images and start the infrastructure scaffold.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if [[ ! -f docker-compose.yml ]]; then
    echo "[*] No docker-compose.yml - running setup with default 3 teams"
    ./scripts/setup.sh 3
fi

if [[ -z "${VULN_IMAGE:-}" ]]; then
    cat <<'EOF'
[!] VULN_IMAGE is required.

This scaffold does not bundle a vulnerable application. Build or pull one,
then start the infrastructure with:

  VULN_IMAGE=<image-name-or-tag> ./scripts/start.sh

Each generated team<N>-vuln service will use that image at 10.10.<N>.3.
EOF
    exit 1
fi

echo "[*] Building team SSH gateway images..."
docker compose build

echo "[*] Starting infrastructure..."
docker compose up -d

echo
echo "[+] Up. Useful access points:"
echo "    Per-team SSH:          ssh -p <2200+N> ctfuser@localhost  (password team<N>pass)"
echo "    Vulnerable app slots:  team<N>-vuln at 10.10.<N>.3 using ${VULN_IMAGE}"
echo
echo "[*] Inspect containers with:  docker compose ps"
