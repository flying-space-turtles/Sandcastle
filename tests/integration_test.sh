#!/usr/bin/env bash
# SC-005 – Full competition-lifecycle integration test.
#
# Two modes:
#   Full mode (default): requires Docker on a native-Linux host.
#     ./tests/integration_test.sh
#
#   Local fixture mode: mocks Docker and all I/O; safe to run in CI without
#     Docker.  Controlled by --local or SANDCASTLE_LOCAL_TEST=1.
#     ./tests/integration_test.sh --local
#
# On any failure the script exits non-zero and prints a log directory path
# where Compose/app/firewall logs were collected.
#
# Environment overrides:
#   SANDCASTLE_LOCAL_TEST=1   Force local mode (same as --local)
#   SC005_TIMEOUT=180         Bounded timeout for arena startup in seconds
#   SC005_LOG_DIR=<path>      Where to dump failure logs (default: ./sc005-logs/<ts>)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARENA="${ROOT}/scripts/arena.sh"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

LOCAL_MODE="${SANDCASTLE_LOCAL_TEST:-0}"
SC005_TIMEOUT="${SC005_TIMEOUT:-180}"

for arg in "$@"; do
    case "${arg}" in
        --local)
            LOCAL_MODE=1
            ;;
        --help|-h)
            echo "Usage: $0 [--local]"
            echo "  --local   Run fixture-driven tests only (no Docker required)"
            exit 0
            ;;
        *)
            echo "integration_test.sh: unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

TS="$(date +%Y%m%d-%H%M%S)"
SC005_LOG_DIR="${SC005_LOG_DIR:-${ROOT}/sc005-logs/${TS}}"

log_info()  { printf '[*] %s\n' "$*"; }
log_ok()    { printf '[+] %s\n' "$*"; }
log_fail()  { printf '[!] FAIL: %s\n' "$*" >&2; }

die() {
    log_fail "$*"
    exit 1
}

collect_failure_logs() {
    if [[ "${LOCAL_MODE}" == "1" ]]; then
        return 0
    fi
    log_info "Collecting failure logs -> ${SC005_LOG_DIR}"
    mkdir -p "${SC005_LOG_DIR}"
    docker compose -f "${ROOT}/docker-compose.yml" logs \
        >"${SC005_LOG_DIR}/compose.log" 2>&1 || true
    for team in 1 2; do
        docker logs "team${team}-vuln-app" \
            >"${SC005_LOG_DIR}/team${team}-vuln-app.log" 2>&1 || true
    done
    docker logs sandcastle-firewall \
        >"${SC005_LOG_DIR}/firewall.log" 2>&1 || true
    echo "Logs written to: ${SC005_LOG_DIR}"
}

trap 'rc=$?; if ((rc != 0)); then collect_failure_logs; fi' EXIT

checker_plant_token() {
    local team="$1"
    local service_name plant_token

    service_name="$(basename "${ARENA_SERVICE_TEMPLATE_PATH%/}")"
    IFS=$'\t' read -r _ _ plant_token < <(
        python3 "${ROOT}/gameserver/checker_credentials.py" \
            --secret "${ARENA_CHECKER_SECRET}" \
            --team "${team}" \
            --service "${service_name}"
    )
    [[ -n "${plant_token}" ]] ||
        die "failed to derive checker plant token for team${team}/${service_name}"
    printf '%s\n' "${plant_token}"
}

# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
#  LOCAL FIXTURE MODE
# ═══════════════════════════════════════════════════════════════════════════
# ---------------------------------------------------------------------------

run_local_tests() {
    local TMP_ROOT
    TMP_ROOT="$(mktemp -d)"
    local FIXTURE="${TMP_ROOT}/arena"
    local MOCK_BIN="${TMP_ROOT}/bin"
    local LOG_FILE="${TMP_ROOT}/lifecycle.log"

    cleanup_local() { rm -rf "${TMP_ROOT:-}"; }
    # Override the outer EXIT trap for the duration of this function;
    # restore it on return so the outer trap (collect_failure_logs) takes over.
    trap cleanup_local EXIT

    # -----------------------------------------------------------------------
    # Fixture tree
    # -----------------------------------------------------------------------
    mkdir -p \
        "${MOCK_BIN}" \
        "${FIXTURE}/config" \
        "${FIXTURE}/scripts/lib" \
        "${FIXTURE}/services/example-vuln/exploits" \
        "${FIXTURE}/teams/generated/team1/example-vuln/app" \
        "${FIXTURE}/teams/generated/team2/example-vuln/app"

    cp "${ROOT}/scripts/lib/arena_config.sh" "${FIXTURE}/scripts/lib/arena_config.sh"

    cat >"${FIXTURE}/config/arena.env" <<'EOF'
ARENA_TEAM_COUNT=2
ARENA_CTF_SUBNET=10.10.0.0/16
ARENA_CTF_GATEWAY=10.10.0.1
ARENA_SSH_BASE_PORT=2200
ARENA_SERVICE_PORT=8080
ARENA_TEAM_USERNAME_PATTERN=team{team}
ARENA_TEAM_PASSWORD_PATTERN=team{team}pass
ARENA_TEAM_TOKEN_PATTERN=sandcastle-team{team}-submission-token-change-me
ARENA_SERVICE_TEMPLATE=services/example-vuln
ARENA_FIREWALL_WS_PORT=6789
ARENA_FIREWALL_PROXY_PORT=15000
ARENA_FIREWALL_PROBE_PORT=18080
ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS=15
ARENA_FIREWALL_EVENT_QUEUE_SIZE=2048
ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES=4194304
ARENA_FIREWALL_RECENT_ICMP_LIMIT=4096
ARENA_BOT_API_HOST=127.0.0.1
ARENA_BOT_API_PORT=7878
ARENA_BOT_LOOP_SECONDS=60
ARENA_STARTUP_TIMEOUT_SECONDS=5
ARENA_ROUND_DURATION_SECONDS=120
ARENA_FLAG_EXPIRY_ROUNDS=5
EOF

    # Per-team compose stubs
    printf 'services: {}\n' >"${FIXTURE}/docker-compose.yml"
    for team in 1 2; do
        printf 'services: {}\n' \
            >"${FIXTURE}/teams/generated/team${team}/example-vuln/docker-compose.yml"
        printf 'print("team%s patch")\n' "${team}" \
            >"${FIXTURE}/teams/generated/team${team}/example-vuln/app/app.py"
    done

    # -----------------------------------------------------------------------
    # Reference exploit stub: prints a plausible flag
    # -----------------------------------------------------------------------
    cat >"${FIXTURE}/services/example-vuln/exploits/path_traversal_export.py" <<'PYEOF'
#!/usr/bin/env python3
import sys
print("[+] flag: FLAG{deadbeefdeadbeefdeadbeefdeadbeef}")
sys.exit(0)
PYEOF
    chmod +x "${FIXTURE}/services/example-vuln/exploits/path_traversal_export.py"

    # -----------------------------------------------------------------------
    # Mock scripts that log invocations
    # -----------------------------------------------------------------------
    cat >"${FIXTURE}/scripts/setup.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'setup %s\n' "$*" >> "${SC005_LOG:?}"
EOF
    chmod +x "${FIXTURE}/scripts/setup.sh"

    cat >"${FIXTURE}/scripts/smoke-network.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'smoke-network\n' >> "${SC005_LOG:?}"
[[ "${SC005_SMOKE_FAIL:-0}" != "1" ]]
EOF
    chmod +x "${FIXTURE}/scripts/smoke-network.sh"

    # -----------------------------------------------------------------------
    # Mock docker binary
    # -----------------------------------------------------------------------
    cat >"${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >> "${SC005_LOG:?}"
scenario="${SC005_SCENARIO:-healthy}"

case "${1:-}" in
    info)    exit 0 ;;
    compose) exit 0 ;;
    inspect)
        if [[ "${2:-}" == "--format" ]]; then
            name="${4:-}"
            if [[ "${scenario}" == "infra-fail" && "${name}" == "team2-vuln" ]]; then
                echo "exited"
            elif [[ "${scenario}" == "missing-app" && "${name}" == "team2-vuln-app" ]]; then
                exit 1
            else
                echo "running"
            fi
        fi
        exit 0
        ;;
    exec)
        container="${2:-}"
        shift 2 || true
        # SSH reachability probe: nc -z host port
        if [[ "${1:-}" == "nc" ]]; then
            printf 'nc-ok\n' >> "${SC005_LOG:?}"
            exit 0
        fi
        # plant flag step
        if [[ "${container}" == "team1-vuln" && "${1:-}" == "python3" ]]; then
            printf 'python3-exploit\n' >> "${SC005_LOG:?}"
            if [[ "${scenario}" == "exploit-fail" ]]; then
                exit 1
            fi
            echo "[+] flag: FLAG{deadbeefdeadbeefdeadbeefdeadbeef}"
            exit 0
        fi
        # curl plant step
        if [[ "${container}" == "team1-vuln" && "${1:-}" == "curl" ]]; then
            printf 'curl-plant\n' >> "${SC005_LOG:?}"
            echo '{"status":"planted"}'
            exit 0
        fi
        # health check
        if [[ "${scenario}" == "health-fail" && "${container}" == "team2-vuln" ]]; then
            exit 1
        fi
        echo '{"status":"ok"}'
        exit 0
        ;;
    ps)
        printf '%s\n' team1-vuln-app team2-vuln-app
        exit 0
        ;;
    rm)      exit 0 ;;
    volume)
        case "${2:-}" in
            ls) printf '%s\n' sandcastle_team1-data sandcastle_team2-data ;;
            rm) ;;
        esac
        exit 0
        ;;
    logs)    exit 0 ;;
    network)
        case "${2:-}" in
            ls) printf '\n' ;;
        esac
        exit 0
        ;;
esac
exit 0
EOF
    chmod +x "${MOCK_BIN}/docker"

    cat >"${MOCK_BIN}/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "${MOCK_BIN}/sleep"

    # nc stub for SSH port check
    cat >"${MOCK_BIN}/nc" <<'EOF'
#!/usr/bin/env bash
printf 'nc %s\n' "$*" >> "${SC005_LOG:?}"
exit 0
EOF
    chmod +x "${MOCK_BIN}/nc"

    # python3 stub for exploit (when called directly, not via docker exec)
    cat >"${MOCK_BIN}/python3" <<'EOF'
#!/usr/bin/env bash
printf 'python3 %s\n' "$*" >> "${SC005_LOG:?}"
echo "[+] flag: FLAG{deadbeefdeadbeefdeadbeefdeadbeef}"
exit 0
EOF
    chmod +x "${MOCK_BIN}/python3"

    # -----------------------------------------------------------------------
    # Helper to run arena.sh with the fixture environment
    # -----------------------------------------------------------------------
    run_arena() {
        local scenario="$1"; shift
        SC005_LOG="${LOG_FILE}" \
        SC005_SCENARIO="${scenario}" \
            PATH="${MOCK_BIN}:${PATH}" \
            SANDCASTLE_ROOT="${FIXTURE}" \
            SANDCASTLE_FIREWALL_PREFLIGHT="${FIXTURE}/scripts/firewall-preflight.sh" \
            SANDCASTLE_NETWORK_SMOKE="${FIXTURE}/scripts/smoke-network.sh" \
            SANDCASTLE_HEALTH_POLL_SECONDS=1 \
            "${ARENA}" "$@"
    }

    assert_log() {
        local pattern="$1"
        if ! grep -Fq "${pattern}" "${LOG_FILE}"; then
            printf 'Expected log to contain: %s\n' "${pattern}" >&2
            cat "${LOG_FILE}" >&2
            die "local fixture assertion failed"
        fi
    }

    refute_log() {
        local pattern="$1"
        if grep -Fq "${pattern}" "${LOG_FILE}"; then
            printf 'Expected log NOT to contain: %s\n' "${pattern}" >&2
            cat "${LOG_FILE}" >&2
            die "local fixture assertion failed (pattern should be absent)"
        fi
    }

    # -----------------------------------------------------------------------
    # LOCAL TEST 1: happy-path up → exploit → restart → down
    # -----------------------------------------------------------------------
    log_info "[local] 1/5  happy-path lifecycle"
    : >"${LOG_FILE}"
    up_output="$(run_arena healthy up --teams 2 --timeout 2)"

    grep -Fq "Complete arena is healthy" <<<"${up_output}" ||
        die "up did not report healthy arena"
    assert_log "setup --remove-orphan-containers --teams 2"
    assert_log "smoke-network"
    assert_log "docker compose -f ${FIXTURE}/docker-compose.yml up -d --build --remove-orphans"
    assert_log "docker rm -f team1-vuln-app"
    assert_log "docker compose -f ${FIXTURE}/teams/generated/team1/example-vuln/docker-compose.yml up"
    log_ok "[local] 1/5  up: ok"

    # -----------------------------------------------------------------------
    # LOCAL TEST 2: flag plant (simulate curl to /internal/plant)
    # -----------------------------------------------------------------------
    log_info "[local] 2/5  flag plant via /internal/plant"
    : >"${LOG_FILE}"
    plant_output="$(
        SC005_LOG="${LOG_FILE}" \
        SC005_SCENARIO="healthy" \
            PATH="${MOCK_BIN}:${PATH}" \
            SANDCASTLE_ROOT="${FIXTURE}" \
            docker exec team1-vuln curl -fsS \
                -X POST \
                -H "X-Plant-Token: test-token" \
                -H "Content-Type: application/json" \
                -d '{"flag":"FLAG{deadbeefdeadbeefdeadbeefdeadbeef}"}' \
                "http://10.10.2.3:8080/internal/plant"
    )"
    grep -Fq "planted" <<<"${plant_output}" ||
        die "plant command did not return 'planted'"
    log_ok "[local] 2/5  plant: ok"

    # -----------------------------------------------------------------------
    # LOCAL TEST 3: cross-team exploit captures a flag
    # -----------------------------------------------------------------------
    log_info "[local] 3/5  cross-team reference exploit"
    : >"${LOG_FILE}"
    exploit_output="$(
        SC005_LOG="${LOG_FILE}" \
        SC005_SCENARIO="healthy" \
            PATH="${MOCK_BIN}:${PATH}" \
            SANDCASTLE_ROOT="${FIXTURE}" \
            docker exec team1-vuln python3 \
                "${FIXTURE}/services/example-vuln/exploits/path_traversal_export.py" \
                "http://10.10.2.3:8080"
    )"
    flag_pattern="FLAG{[a-f0-9]{32}}"
    if ! echo "${exploit_output}" | grep -Eq "${flag_pattern}"; then
        die "exploit output did not contain a flag: ${exploit_output}"
    fi
    log_ok "[local] 3/5  exploit: ok"

    # -----------------------------------------------------------------------
    # LOCAL TEST 4: stale-namespace regression – app container removed before
    # nested Compose up
    # -----------------------------------------------------------------------
    log_info "[local] 4/5  stale-namespace regression"
    : >"${LOG_FILE}"
    run_arena healthy restart --timeout 2 >/dev/null

    remove_line="$(grep -nF 'docker rm -f team1-vuln-app' "${LOG_FILE}" | head -n 1 | cut -d: -f1)"
    up_line="$(
        grep -nF "docker compose -f ${FIXTURE}/teams/generated/team1/example-vuln/docker-compose.yml up" \
            "${LOG_FILE}" | head -n 1 | cut -d: -f1
    )"
    ((remove_line < up_line)) ||
        die "stale-namespace fix: app was not removed before nested Compose recreate"
    log_ok "[local] 4/5  stale-namespace: ok"

    # -----------------------------------------------------------------------
    # LOCAL TEST 5: cleanup leaves no arena containers or networks
    # -----------------------------------------------------------------------
    log_info "[local] 5/5  cleanup verification"
    : >"${LOG_FILE}"
    run_arena healthy down >/dev/null

    assert_log "docker rm -f team1-vuln-app team2-vuln-app"
    assert_log "docker compose -f ${FIXTURE}/docker-compose.yml down --remove-orphans"
    # Data volumes must be preserved on plain 'down'
    refute_log "docker volume rm"
    log_ok "[local] 5/5  cleanup: ok"

    # -----------------------------------------------------------------------
    # LOCAL TEST BONUS: failure paths
    # -----------------------------------------------------------------------
    log_info "[local] +    failure path: exploit fails → test exits non-zero"
    : >"${LOG_FILE}"
    set +e
    SC005_LOG="${LOG_FILE}" \
    SC005_SCENARIO="exploit-fail" \
        PATH="${MOCK_BIN}:${PATH}" \
        SANDCASTLE_ROOT="${FIXTURE}" \
        docker exec team1-vuln python3 \
            "${FIXTURE}/services/example-vuln/exploits/path_traversal_export.py" \
            "http://10.10.2.3:8080" >/dev/null 2>&1
    exploit_fail_rc=$?
    set -e
    ((exploit_fail_rc != 0)) ||
        die "exploit in 'exploit-fail' scenario should have returned non-zero"
    log_ok "[local] +    exploit-fail scenario: ok"

    log_ok "[local] All local fixture tests passed."

    # Restore the outer trap and clean up now.
    rm -rf "${TMP_ROOT}"
    trap 'rc=$?; if ((rc != 0)); then collect_failure_logs; fi' EXIT
}

# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
#  FULL DOCKER MODE
# ═══════════════════════════════════════════════════════════════════════════
# ---------------------------------------------------------------------------

run_docker_tests() {
    log_info "Starting full Docker integration test (timeout=${SC005_TIMEOUT}s)"

    # Load arena config so we have ARENA_NETWORK_PREFIX, ARENA_SERVICE_PORT, etc.
    arena_config_load "${ROOT}" ||
        die "could not load arena config"

    # -----------------------------------------------------------------------
    # Pre-flight: Docker must be available
    # -----------------------------------------------------------------------
    command -v docker >/dev/null 2>&1 || die "Docker CLI not found"
    docker info >/dev/null 2>&1 || die "Docker daemon not reachable"
    docker compose version >/dev/null 2>&1 || die "Docker Compose plugin missing"

    # -----------------------------------------------------------------------
    # STEP 1: Generate topology and start the complete arena
    # -----------------------------------------------------------------------
    log_info "[docker] 1/7  arena up --teams 2"
    "${ARENA}" up --teams 2 --timeout "${SC005_TIMEOUT}" ||
        die "arena.sh up failed"
    log_ok "[docker] 1/7  arena up: ok"

    # -----------------------------------------------------------------------
    # STEP 2: Status check – all components healthy
    # -----------------------------------------------------------------------
    log_info "[docker] 2/7  status check"
    status_output="$("${ARENA}" status --format tsv)"
    for team in 1 2; do
        grep -Fq "team${team}"$'\t'"gateway"$'\t'"running" <<<"${status_output}" ||
            die "team${team} gateway not running"
        grep -Fq "team${team}"$'\t'"app"$'\t'"running"$'\t'"healthy" <<<"${status_output}" ||
            die "team${team} app not healthy"
    done
    grep -Fq $'-\tfirewall\trunning' <<<"${status_output}" ||
        die "firewall not running"
    log_ok "[docker] 2/7  status: ok"

    # -----------------------------------------------------------------------
    # STEP 3: SSH reachability (TCP port check, no key needed)
    # -----------------------------------------------------------------------
    log_info "[docker] 3/7  SSH gateway reachability"
    for team in 1 2; do
        ssh_port="$((ARENA_SSH_BASE_PORT + team))"
        timeout 10 bash -c "
            until nc -z -w2 127.0.0.1 ${ssh_port}; do sleep 1; done
        " || die "team${team} SSH gateway port ${ssh_port} not reachable"
    done
    log_ok "[docker] 3/7  SSH reachability: ok"

    # -----------------------------------------------------------------------
    # STEP 4: Plant a known flag via /internal/plant
    # -----------------------------------------------------------------------
    log_info "[docker] 4/7  flag plant via /internal/plant"
    TEST_FLAG="FLAG{$(python3 -c 'import secrets; print(secrets.token_hex(16))')}"
    TEAM2_PLANT_TOKEN="$(checker_plant_token 2)"
    # team2 app is at 10.10.2.3:ARENA_SERVICE_PORT inside the ctf network;
    # we use docker exec on team1-vuln so we're inside the correct network.
    plant_response="$(
        docker exec team1-vuln \
            curl -fsS \
                --max-time 10 \
                -X POST \
                -H "X-Plant-Token: ${TEAM2_PLANT_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "{\"flag\":\"${TEST_FLAG}\"}" \
                "http://${ARENA_NETWORK_PREFIX}.2.3:${ARENA_SERVICE_PORT}/internal/plant"
    )" || die "curl to /internal/plant failed"
    grep -Fq "planted" <<<"${plant_response}" ||
        die "plant API did not return 'planted': ${plant_response}"
    log_ok "[docker] 4/7  plant: ok (flag=${TEST_FLAG})"

    # -----------------------------------------------------------------------
    # STEP 5: Cross-team reference exploit – path traversal from team1 → team2
    # -----------------------------------------------------------------------
    log_info "[docker] 5/7  cross-team path-traversal exploit"
    exploit_output="$(
        docker exec team1-vuln \
            python3 /srv/example-vuln/exploits/path_traversal_export.py \
            "http://${ARENA_NETWORK_PREFIX}.2.3:${ARENA_SERVICE_PORT}"
    )" || die "exploit script exited non-zero"

    if ! echo "${exploit_output}" | grep -qF "${TEST_FLAG}"; then
        die "exploit captured a flag but it does not match the planted flag.
  Expected: ${TEST_FLAG}
  Got:      ${exploit_output}"
    fi
    log_ok "[docker] 5/7  exploit: captured ${TEST_FLAG}"

    # -----------------------------------------------------------------------
    # STEP 6: Stale-namespace regression – stop team1-vuln, restart it, prove
    # the app recovers without manual intervention
    # -----------------------------------------------------------------------
    log_info "[docker] 6/7  stale-namespace regression"
    log_info "  Stopping team1-vuln..."
    docker compose -f "${ROOT}/docker-compose.yml" stop team1-vuln >/dev/null
    log_info "  Restarting team1-vuln..."
    docker compose -f "${ROOT}/docker-compose.yml" start team1-vuln >/dev/null

    # arena.sh restart removes the old app container and re-creates it,
    # which is exactly what must happen to fix the stale namespace.
    log_info "  Running arena.sh restart to prove app recovery..."
    "${ARENA}" restart --timeout "${SC005_TIMEOUT}" ||
        die "arena.sh restart failed after team1-vuln stop/start"

    # Confirm team1 app is back and healthy
    docker exec team1-vuln \
        curl -fsS --max-time 5 \
        "http://127.0.0.1:${ARENA_SERVICE_PORT}/health" >/dev/null ||
        die "team1 app /health failed after restart"
    log_ok "[docker] 6/7  stale-namespace regression: ok"

    # -----------------------------------------------------------------------
    # STEP 7: Cleanup – no sandcastle containers or ctf networks remain
    # -----------------------------------------------------------------------
    log_info "[docker] 7/7  cleanup"
    "${ARENA}" down ||
        die "arena.sh down failed"

    # Verify no running sandcastle containers
    remaining_containers="$(
        docker ps -a --filter "name=team" --filter "name=sandcastle" \
            --format '{{.Names}}' 2>/dev/null | grep -E '^(team|sandcastle)' || true
    )"
    if [[ -n "${remaining_containers}" ]]; then
        die "Cleanup left containers: ${remaining_containers}"
    fi

    # Verify the ctf network is removed
    remaining_networks="$(
        docker network ls --filter "name=sandcastle" --format '{{.Name}}' 2>/dev/null || true
    )"
    if [[ -n "${remaining_networks}" ]]; then
        die "Cleanup left networks: ${remaining_networks}"
    fi

    log_ok "[docker] 7/7  cleanup: ok"
    log_ok "[docker] All full-Docker integration tests passed."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

main() {
    if [[ "${LOCAL_MODE}" == "1" ]]; then
        log_info "Running in local fixture mode (no Docker required)"
        run_local_tests
    else
        run_docker_tests
    fi
    echo
    echo "integration_test.sh: all tests passed"
}

main
