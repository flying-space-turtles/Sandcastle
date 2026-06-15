#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
MOCK_BIN="${TMP_ROOT}/bin"
BRIDGE_NF_PATH="${TMP_ROOT}/bridge-nf-call-iptables"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p "${MOCK_BIN}"

cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
    info)
        if [[ "${2:-}" == "--format" ]]; then
            echo "${PREFLIGHT_DOCKER_OS:-Mock Linux}"
        fi
        exit 0
        ;;
    compose)
        [[ "${2:-}" == "version" ]] && echo "Docker Compose version v99.0.0"
        exit 0
        ;;
esac
exit 0
EOF
cat > "${MOCK_BIN}/modprobe" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "br_netfilter" ]] || exit 1
printf '0\n' > "${PREFLIGHT_BRIDGE_NF_PATH:?}"
EOF
cat > "${MOCK_BIN}/sysctl" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-w" && "${2:-}" == "net.bridge.bridge-nf-call-iptables=1" ]] || exit 1
printf '1\n' > "${PREFLIGHT_BRIDGE_NF_PATH:?}"
EOF
chmod +x "${MOCK_BIN}/docker" "${MOCK_BIN}/modprobe" "${MOCK_BIN}/sysctl"

run_preflight() {
    local mode="${1:---check}"
    PATH="${MOCK_BIN}:${PATH}" \
        FIREWALL_PREFLIGHT_BRIDGE_NF_PATH="${BRIDGE_NF_PATH}" \
        PREFLIGHT_BRIDGE_NF_PATH="${BRIDGE_NF_PATH}" \
        SANDCASTLE_HOST_OS="${PREFLIGHT_HOST_OS:-Linux}" \
        "${ROOT}/scripts/firewall-preflight.sh" "${mode}"
}

printf '1\n' > "${BRIDGE_NF_PATH}"
enabled_output="$(run_preflight)"
grep -Fq "Docker orchestration prerequisites are available" <<< "${enabled_output}"
grep -Fq "bridge netfilter is enabled" <<< "${enabled_output}"

rm -f "${BRIDGE_NF_PATH}"
set +e
missing_output="$(run_preflight 2>&1)"
missing_rc=$?
set -e
((missing_rc != 0)) || {
    echo "Missing bridge netfilter should fail --check" >&2
    exit 1
}
grep -Fq "bridge netfilter control is unavailable" <<< "${missing_output}"

apply_output="$(run_preflight --apply 2>&1)"
grep -Fq "bridge netfilter is enabled" <<< "${apply_output}"
grep -Fq "Docker orchestration prerequisites are available" <<< "${apply_output}"
grep -Fxq "1" "${BRIDGE_NF_PATH}"

desktop_output="$(PREFLIGHT_DOCKER_OS="Docker Desktop" run_preflight 2>&1)"
grep -Fq "Docker Desktop" <<< "${desktop_output}"
grep -Fq "Docker orchestration prerequisites are available" <<< "${desktop_output}"

mac_output="$(PREFLIGHT_HOST_OS="Darwin" run_preflight 2>&1)"
grep -Fq "Docker orchestration prerequisites are available" <<< "${mac_output}"

echo "firewall preflight tests: ok"
