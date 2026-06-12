#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
MOCK_BIN="${TMP_ROOT}/bin"
SYSCTL_FILE="${TMP_ROOT}/bridge-nf-call-iptables"

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
esac
exit 0
EOF
chmod +x "${MOCK_BIN}/docker"

run_preflight() {
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_HOST_OS="${PREFLIGHT_HOST_OS:-Linux}" \
        SANDCASTLE_BRIDGE_SYSCTL_PATH="${SYSCTL_FILE}" \
        "${ROOT}/scripts/firewall-preflight.sh" --check
}

echo 1 > "${SYSCTL_FILE}"
run_preflight | grep -Fq "firewall preflight"

echo 0 > "${SYSCTL_FILE}"
set +e
disabled_output="$(run_preflight 2>&1)"
disabled_rc=$?
set -e
((disabled_rc != 0))
grep -Fq "bridge-nf-call-iptables is 0" <<< "${disabled_output}"

echo 1 > "${SYSCTL_FILE}"
set +e
desktop_output="$(PREFLIGHT_DOCKER_OS="Docker Desktop" run_preflight 2>&1)"
desktop_rc=$?
set -e
((desktop_rc != 0))
grep -Fq "Docker Desktop" <<< "${desktop_output}"

set +e
mac_output="$(PREFLIGHT_HOST_OS="Darwin" run_preflight 2>&1)"
mac_rc=$?
set -e
((mac_rc != 0))
grep -Fq "native Linux" <<< "${mac_output}"

echo "firewall preflight tests: ok"
