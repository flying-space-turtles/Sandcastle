#!/usr/bin/env bash
# Verify Docker orchestration and bridge-netfilter prerequisites.

set -euo pipefail

MODE="check"
BRIDGE_NF_PATH="${FIREWALL_PREFLIGHT_BRIDGE_NF_PATH:-/proc/sys/net/bridge/bridge-nf-call-iptables}"

usage() {
    cat <<'EOF'
Usage: ./scripts/firewall-preflight.sh [--check|--apply]

Verify host-side Docker orchestration prerequisites and Linux bridge netfilter
support for the Sandcastle firewall.

--apply loads br_netfilter and enables net.bridge.bridge-nf-call-iptables when
needed. Run it with sudo on native Linux hosts.
EOF
}

die() {
    echo "firewall-preflight.sh: $*" >&2
    exit 1
}

is_native_linux_runtime() {
    local host_os="$1"
    local docker_os="$2"

    [[ "${host_os}" == "Linux" ]] || return 1
    [[ "${docker_os}" != *"Docker Desktop"* ]] || return 1
    return 0
}

bridge_value() {
    [[ -f "${BRIDGE_NF_PATH}" ]] || return 1
    cat "${BRIDGE_NF_PATH}"
}

set_bridge_value() {
    if [[ "${BRIDGE_NF_PATH}" == "/proc/sys/net/bridge/bridge-nf-call-iptables" ]] &&
       command -v sysctl >/dev/null 2>&1; then
        sysctl -w net.bridge.bridge-nf-call-iptables=1 >/dev/null
        return
    fi
    printf '1\n' > "${BRIDGE_NF_PATH}"
}

check_bridge_netfilter() {
    local value

    if ! value="$(bridge_value 2>/dev/null)"; then
        die "bridge netfilter control is unavailable at ${BRIDGE_NF_PATH}; run sudo ./scripts/firewall-preflight.sh --apply to load br_netfilter"
    fi
    value="${value//$'\n'/}"
    [[ "${value}" == "1" ]] ||
        die "net.bridge.bridge-nf-call-iptables must be 1, got ${value}; run sudo ./scripts/firewall-preflight.sh --apply"
    echo "firewall preflight: bridge netfilter is enabled"
}

apply_bridge_netfilter() {
    local value

    if [[ ! -f "${BRIDGE_NF_PATH}" ]]; then
        command -v modprobe >/dev/null 2>&1 ||
            die "modprobe is required to load br_netfilter"
        modprobe br_netfilter ||
            die "failed to load br_netfilter; rerun with sudo or inspect kernel module support"
    fi
    [[ -f "${BRIDGE_NF_PATH}" ]] ||
        die "bridge netfilter control is still unavailable after loading br_netfilter"

    value="$(bridge_value)"
    value="${value//$'\n'/}"
    if [[ "${value}" != "1" ]]; then
        set_bridge_value ||
            die "failed to enable net.bridge.bridge-nf-call-iptables"
    fi
    check_bridge_netfilter
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

host_os="${SANDCASTLE_HOST_OS:-$(uname -s)}"
docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
if [[ -n "${docker_os}" ]]; then
    echo "firewall preflight: Docker runtime OS: ${docker_os}"
fi

if is_native_linux_runtime "${host_os}" "${docker_os}"; then
    if [[ "${MODE}" == "apply" ]]; then
        apply_bridge_netfilter
    else
        check_bridge_netfilter
    fi
else
    echo "firewall preflight: bridge netfilter is checked after firewall container startup"
fi

echo "firewall preflight: Docker orchestration prerequisites are available"
