#!/usr/bin/env bash
# Verify or configure the Linux host requirements for transparent interception.

set -euo pipefail

MODE="check"
BRIDGE_SYSCTL="net.bridge.bridge-nf-call-iptables"
BRIDGE_SYSCTL_PATH="${SANDCASTLE_BRIDGE_SYSCTL_PATH:-/proc/sys/net/bridge/bridge-nf-call-iptables}"

usage() {
    cat <<'EOF'
Usage: ./scripts/firewall-preflight.sh [--check|--apply]

Verify the native Linux networking requirements used by the Sandcastle
transparent firewall. --apply loads br_netfilter and enables bridge iptables
processing; it normally requires root.
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

host_os="${SANDCASTLE_HOST_OS:-$(uname -s 2>/dev/null || true)}"
[[ "${host_os}" == "Linux" ]] ||
    die "transparent firewall enforcement requires a native Linux host"

command -v docker >/dev/null 2>&1 ||
    die "Docker CLI is not installed"
docker info >/dev/null 2>&1 ||
    die "Docker daemon is not reachable"

docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
case "${docker_os}" in
    *Docker\ Desktop*|*docker\ desktop*)
        die "Docker Desktop does not expose the required native Linux bridge path"
        ;;
esac

if [[ "${MODE}" == "apply" ]]; then
    ((EUID == 0)) ||
        die "--apply must run as root, for example: sudo ./scripts/firewall-preflight.sh --apply"
    command -v modprobe >/dev/null 2>&1 ||
        die "modprobe is required to load br_netfilter"
    command -v sysctl >/dev/null 2>&1 ||
        die "sysctl is required to configure ${BRIDGE_SYSCTL}"

    modprobe br_netfilter
    sysctl -w "${BRIDGE_SYSCTL}=1" >/dev/null
fi

[[ -r "${BRIDGE_SYSCTL_PATH}" ]] ||
    die "${BRIDGE_SYSCTL_PATH} is unavailable; load the br_netfilter module"

bridge_value="$(<"${BRIDGE_SYSCTL_PATH}")"
[[ "${bridge_value}" == "1" ]] ||
    die "${BRIDGE_SYSCTL} is ${bridge_value}; run sudo ./scripts/firewall-preflight.sh --apply"

echo "firewall preflight: native Linux bridge interception is enabled"
