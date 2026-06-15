#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
FIXTURE="${TMP_ROOT}/fixture"
LOG_FILE="${TMP_ROOT}/staging-dind-smoke.log"
PHASE_FILE="${TMP_ROOT}/staging-smoke-phase"
DIND_ISOLATION_LOG_FILE="${TMP_ROOT}/dind-isolation.log"

cleanup() {
    rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

mkdir -p "${FIXTURE}/scripts" "${FIXTURE}/tests"

cp "${ROOT}/scripts/staging-dind-smoke.sh" "${FIXTURE}/scripts/staging-dind-smoke.sh"
chmod +x "${FIXTURE}/scripts/staging-dind-smoke.sh"

cat > "${FIXTURE}/scripts/setup.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'setup %s\n' "$*" >> "${STAGING_DIND_SMOKE_TEST_LOG:?}"
EOF

cat > "${FIXTURE}/scripts/firewall-preflight.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'preflight %s\n' "$*" >> "${STAGING_DIND_SMOKE_TEST_LOG:?}"
EOF

cat > "${FIXTURE}/scripts/doctor.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'doctor\n' >> "${STAGING_DIND_SMOKE_TEST_LOG:?}"
EOF

cat > "${FIXTURE}/scripts/arena.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'arena %s\n' "$*" >> "${STAGING_DIND_SMOKE_TEST_LOG:?}"
EOF

cat > "${FIXTURE}/tests/dind_isolation_test.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'dind-isolation\n' >> "${STAGING_DIND_SMOKE_TEST_LOG:?}"
printf 'dind-isolation-output\n'
EOF

cat > "${FIXTURE}/tests/integration_test.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'integration\n' >> "${STAGING_DIND_SMOKE_TEST_LOG:?}"
EOF

chmod +x \
    "${FIXTURE}/scripts/setup.sh" \
    "${FIXTURE}/scripts/firewall-preflight.sh" \
    "${FIXTURE}/scripts/doctor.sh" \
    "${FIXTURE}/scripts/arena.sh" \
    "${FIXTURE}/tests/dind_isolation_test.sh" \
    "${FIXTURE}/tests/integration_test.sh"

smoke_output="$(
    SANDCASTLE_ROOT="${FIXTURE}" \
        SANDCASTLE_STAGING_PHASE_FILE="${PHASE_FILE}" \
        SANDCASTLE_DIND_ISOLATION_LOG_FILE="${DIND_ISOLATION_LOG_FILE}" \
        STAGING_DIND_SMOKE_TEST_LOG="${LOG_FILE}" \
        "${FIXTURE}/scripts/staging-dind-smoke.sh" --teams 4 --timeout 321
)"

expected="$(
    cat <<'EOF'
setup --teams 4 --dind --remove-orphan-containers
preflight --check
doctor
arena reset --timeout 321
dind-isolation
integration
EOF
)"
actual="$(cat "${LOG_FILE}")"
phase="$(cat "${PHASE_FILE}")"
dind_log="$(cat "${DIND_ISOLATION_LOG_FILE}")"

if [[ "${actual}" != "${expected}" ]]; then
    echo "Unexpected staging DinD smoke order" >&2
    echo "--- expected ---" >&2
    echo "${expected}" >&2
    echo "--- actual ---" >&2
    echo "${actual}" >&2
    exit 1
fi
if [[ "${phase}" != "running full integration test" ]]; then
    echo "Unexpected final smoke phase: ${phase}" >&2
    exit 1
fi
if [[ "${dind_log}" != "dind-isolation-output" ]]; then
    echo "Unexpected DinD isolation log: ${dind_log}" >&2
    exit 1
fi

for marker in \
    "[*] [staging-smoke] Generating DinD topology..." \
    "[*] [staging-smoke] Checking firewall preflight..." \
    "[*] [staging-smoke] Running doctor before startup..." \
    "[*] [staging-smoke] Starting disposable DinD arena..." \
    "[*] [staging-smoke] Running DinD isolation test..." \
    "[*] [staging-smoke] Running full integration test..."
do
    if ! grep -Fq "${marker}" <<< "${smoke_output}"; then
        echo "Missing smoke marker: ${marker}" >&2
        echo "--- output ---" >&2
        echo "${smoke_output}" >&2
        exit 1
    fi
done
