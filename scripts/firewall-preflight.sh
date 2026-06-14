#!/usr/bin/env bash
# Verify Docker orchestration prerequisites for the container-owned firewall.

set -euo pipefail

MODE="check"

usage() {
    cat <<'EOF'
Usage: ./scripts/firewall-preflight.sh [--check|--apply]

Verify host-side Docker orchestration prerequisites for the Sandcastle firewall.
Firewall capability checks and mutations are owned by the sandcastle-firewall
container and are proven after startup by arena.sh and smoke-network.sh.

--apply is retained for compatibility and does not mutate host firewall state.
EOF
}

die() {
    echo "firewall-preflight.sh: $*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        --apply)
            MODE="apply"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

command -v docker >/dev/null 2>&1 ||
    die "Docker CLI is not installed"
docker info >/dev/null 2>&1 ||
    die "Docker daemon is not reachable"
docker compose version >/dev/null 2>&1 ||
    die "Docker Compose plugin is not available"

if [[ "${MODE}" == "apply" ]]; then
    echo "firewall preflight: --apply is deprecated; host firewall state is not modified"
fi

docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
if [[ -n "${docker_os}" ]]; then
    echo "firewall preflight: Docker runtime OS: ${docker_os}"
fi
echo "firewall preflight: Docker orchestration prerequisites are available"
