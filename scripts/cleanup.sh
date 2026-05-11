#!/usr/bin/env bash
# Remove all Docker resources created by the Sandcastle scaffold.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

REMOVE_IMAGES=1

usage() {
    cat <<'EOF'
Usage: ./scripts/cleanup.sh [--keep-images]

Remove Sandcastle Docker containers, volumes, networks, and images.

Options:
  --keep-images    Keep sandcastle/* Docker images
  -h, --help       Show this help text
EOF
}

while (($# > 0)); do
    case "$1" in
        --keep-images)
            REMOVE_IMAGES=0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

remove_containers() {
    local -a containers=()
    local -a matches=()

    mapfile -t matches < <(docker ps -aq --filter "label=sandcastle.role")
    containers+=("${matches[@]}")

    mapfile -t matches < <(docker ps -aq --filter "label=com.docker.compose.project=sandcastle")
    containers+=("${matches[@]}")

    mapfile -t matches < <(docker ps -aq --filter "label=com.docker.compose.project" --filter "name=^/team[0-9]+-vuln-app$")
    containers+=("${matches[@]}")

    mapfile -t matches < <(docker ps -aq --filter "name=^/team[0-9]+-(vuln|ssh|vuln-app)$")
    containers+=("${matches[@]}")

    mapfile -t matches < <(docker ps -aq --filter "name=^/sandcastle-(monitor|firewall)$")
    containers+=("${matches[@]}")

    if ((${#containers[@]} > 0)); then
        printf '%s\n' "${containers[@]}" | sort -u | xargs docker rm -f
    fi
}

remove_volumes() {
    local -a volumes=()
    local -a matches=()

    mapfile -t matches < <(docker volume ls -q --filter "label=sandcastle.role")
    volumes+=("${matches[@]}")

    mapfile -t matches < <(docker volume ls -q --filter "label=com.docker.compose.project=sandcastle")
    volumes+=("${matches[@]}")

    mapfile -t matches < <(docker volume ls -q --filter "name=^sandcastle_team[0-9]+-data$")
    volumes+=("${matches[@]}")

    if ((${#volumes[@]} > 0)); then
        printf '%s\n' "${volumes[@]}" | sort -u | xargs docker volume rm -f
    fi
}

remove_networks() {
    local -a networks=()
    local -a matches=()

    mapfile -t matches < <(docker network ls -q --filter "label=com.docker.compose.project=sandcastle")
    networks+=("${matches[@]}")

    mapfile -t matches < <(docker network ls -q --filter "name=^sandcastle_ctf-network$")
    networks+=("${matches[@]}")

    if ((${#networks[@]} > 0)); then
        printf '%s\n' "${networks[@]}" | sort -u | xargs docker network rm
    fi
}

remove_images() {
    local -a images=()

    mapfile -t images < <(docker image ls -q --filter "reference=sandcastle/*")

    if ((${#images[@]} > 0)); then
        printf '%s\n' "${images[@]}" | sort -u | xargs docker image rm -f
    fi
}

echo "[*] Removing Sandcastle containers..."
remove_containers

echo "[*] Removing Sandcastle volumes..."
remove_volumes

echo "[*] Removing Sandcastle networks..."
remove_networks

if ((REMOVE_IMAGES)); then
    echo "[*] Removing Sandcastle images..."
    remove_images
else
    echo "[*] Keeping Sandcastle images."
fi

echo "[*] Cleanup complete."
