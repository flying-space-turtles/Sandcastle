#!/usr/bin/env bash
# Remove resources created by the Sandcastle scaffold.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

REMOVE_IMAGES=1
REMOVE_GENERATED=0

usage() {
    cat <<'EOF'
Usage: ./scripts/cleanup.sh [--keep-images] [--remove-generated]

Remove Sandcastle Docker containers, volumes, networks, and images.

Options:
  --keep-images       Keep sandcastle/* Docker images
  --remove-generated  Remove generated team workspaces after Docker cleanup
  -h, --help          Show this help text
EOF
}

while (($# > 0)); do
    case "$1" in
        --keep-images)
            REMOVE_IMAGES=0
            ;;
        --remove-generated)
            REMOVE_GENERATED=1
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

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "label=sandcastle.role")
    ((${#matches[@]} == 0)) || containers+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "label=com.docker.compose.project=sandcastle")
    ((${#matches[@]} == 0)) || containers+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "label=com.docker.compose.project" --filter "name=^/team[0-9]+-vuln-app$")
    ((${#matches[@]} == 0)) || containers+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "name=^/team[0-9]+-(vuln|ssh|vuln-app|dind)$")
    ((${#matches[@]} == 0)) || containers+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "name=^/sandcastle-(monitor|firewall|gameserver|bot-controller|visualizer)$")
    ((${#matches[@]} == 0)) || containers+=("${matches[@]}")

    if ((${#containers[@]} > 0)); then
        printf '%s\n' "${containers[@]}" | sort -u | xargs docker rm -f
    fi
}

remove_volumes() {
    local -a volumes=()
    local -a matches=()

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker volume ls -q --filter "label=sandcastle.role")
    ((${#matches[@]} == 0)) || volumes+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker volume ls -q --filter "label=com.docker.compose.project=sandcastle")
    ((${#matches[@]} == 0)) || volumes+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker volume ls -q --filter "name=^sandcastle_team[0-9]+-data$")
    ((${#matches[@]} == 0)) || volumes+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker volume ls -q --filter "name=^sandcastle_team[0-9]+-dind-(data|run)$")
    ((${#matches[@]} == 0)) || volumes+=("${matches[@]}")

    if ((${#volumes[@]} > 0)); then
        printf '%s\n' "${volumes[@]}" | sort -u | xargs docker volume rm -f
    fi
}

remove_networks() {
    local -a networks=()
    local -a matches=()

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker network ls -q --filter "label=com.docker.compose.project=sandcastle")
    ((${#matches[@]} == 0)) || networks+=("${matches[@]}")

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker network ls -q --filter "name=^sandcastle_ctf-network$")
    ((${#matches[@]} == 0)) || networks+=("${matches[@]}")

    if ((${#networks[@]} > 0)); then
        printf '%s\n' "${networks[@]}" | sort -u | xargs docker network rm
    fi
}

remove_images() {
    local -a images=()

    while IFS= read -r image; do
        images+=("${image}")
    done < <(docker image ls -q --filter "reference=sandcastle/*")

    if ((${#images[@]} > 0)); then
        printf '%s\n' "${images[@]}" | sort -u | xargs docker image rm -f
    fi
}

remove_generated_workspaces() {
    local generated_dir="${ROOT}/teams/generated"

    if [[ -d "${generated_dir}" ]]; then
        echo "[*] Removing generated team workspaces..."
        if rm -rf "${generated_dir}" 2>/dev/null; then
            return
        fi
        if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
            sudo -n rm -rf "${generated_dir}"
            return
        fi
        echo "cleanup.sh: failed to remove ${generated_dir}; rerun with sudo or fix file ownership" >&2
        return 1
    else
        echo "[*] No generated team workspaces found."
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

if ((REMOVE_GENERATED)); then
    remove_generated_workspaces
fi

echo "[*] Cleanup complete."
