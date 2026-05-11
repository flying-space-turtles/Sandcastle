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

infer_team_count() {
    local count=0
    local line

    while IFS= read -r line; do
        if [[ "${line}" =~ ^[[:space:]]{2}team([0-9]+)-vuln: ]]; then
            if ((BASH_REMATCH[1] > count)); then
                count="${BASH_REMATCH[1]}"
            fi
        fi
    done < docker-compose.yml

    if ((count == 0)); then
        count=3
    fi

    printf '%s\n' "${count}"
}

generated_contexts_missing() {
    local teams="$1"
    local i

    for ((i = 1; i <= teams; i++)); do
        if [[ ! -d "teams/generated/team${i}/example-vuln" ]]; then
            return 0
        fi
    done

    return 1
}

if [[ ! -f docker-compose.yml ]]; then
    echo "[*] No docker-compose.yml - running setup with default 3 teams"
    ./scripts/setup.sh --teams 3
else
    team_count="$(infer_team_count)"
    if generated_contexts_missing "${team_count}"; then
        echo "[*] Missing generated vulnerable app workspaces - running setup for ${team_count} teams"
        ./scripts/setup.sh --teams "${team_count}"
    fi
fi

echo "[*] Building team images..."
docker compose build

echo "[*] Starting infrastructure..."
docker compose up -d

echo
echo "[+] Up. Useful access points:"
echo "    Per-team SSH gateway:      ssh -p <2200+N> team<N>@localhost  (password team<N>pass)"
echo "    Vulnerable machine:        ssh team<N>@team<N>-vuln from inside the gateway"
echo "    App source on vuln box:    cd ~/example-vuln"
echo "    Start vulnerable app:      docker compose up -d --build"
echo "    App health after startup:  curl http://team<N>-vuln:8080/health"
echo
echo "[*] Inspect containers with:  docker compose ps"
