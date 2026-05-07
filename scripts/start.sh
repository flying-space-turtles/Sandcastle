#!/usr/bin/env bash
# Build images and start the infrastructure scaffold.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

# Make sure a .env exists so docker compose and this script see the same
# values. We seed it from .env.example on first run; users edit .env after
# that.
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "[*] Created .env from .env.example"
fi

# Export every key in .env into this shell so the diagnostic checks below
# (and any later docker invocations that don't read .env, e.g. `docker
# build`) can see them.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [[ ! -f docker-compose.yml ]]; then
    echo "[*] No docker-compose.yml - running setup with default 3 teams"
    ./scripts/setup.sh 3
fi

if [[ -z "${VULN_IMAGE:-}" ]]; then
    cat <<'EOF'
[!] VULN_IMAGE is required.

Set it in .env (recommended) or in your shell, then re-run this script:

  echo 'VULN_IMAGE=<image-name-or-tag>' >> .env
  ./scripts/start.sh

Each generated team<N>-vuln service will use that image at 10.10.<N>.3.
EOF
    exit 1
fi

# When the bundled template service is selected, build it for the user. This
# keeps `./scripts/start.sh` a one-shot for fresh checkouts.
if [[ -d services/example-vuln && "${VULN_IMAGE%%:*}" == "sandcastle/example-vuln" ]]; then
    echo "[*] Building bundled template vulnerable app image (${VULN_IMAGE})..."
    docker build -t "${VULN_IMAGE}" ./services/example-vuln
fi

echo "[*] Building team SSH gateway images..."
docker compose build

echo "[*] Starting infrastructure..."
docker compose up -d

echo
echo "[+] Up. Useful access points:"
echo "    Per-team SSH:          ssh -p <2200+N> ctfuser@localhost  (password team<N>pass)"
echo "    Vulnerable app slots:  team<N>-vuln at 10.10.<N>.3 using ${VULN_IMAGE}"
echo "    Inside team SSH box:   curl http://team<N>-vuln:8080/health"
echo "                           cd ~/service && ls   # source for patching"
echo
echo "[*] Inspect containers with:  docker compose ps"
