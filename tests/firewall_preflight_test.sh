#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
MOCK_BIN="${TMP_ROOT}/bin"

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
chmod +x "${MOCK_BIN}/docker"

run_preflight() {
    PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_HOST_OS="${PREFLIGHT_HOST_OS:-Linux}" \
        "${ROOT}/scripts/firewall-preflight.sh" --check
}

run_preflight | grep -Fq "Docker orchestration prerequisites are available"

desktop_output="$(PREFLIGHT_DOCKER_OS="Docker Desktop" run_preflight 2>&1)"
grep -Fq "Docker Desktop" <<< "${desktop_output}"
grep -Fq "Docker orchestration prerequisites are available" <<< "${desktop_output}"

mac_output="$(PREFLIGHT_HOST_OS="Darwin" run_preflight 2>&1)"
grep -Fq "Docker orchestration prerequisites are available" <<< "${mac_output}"

echo "firewall preflight tests: ok"
