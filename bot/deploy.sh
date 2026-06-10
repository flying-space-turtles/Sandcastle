#!/usr/bin/env bash
# =============================================================================
# deploy.sh — HOST-SIDE script to deploy the bot into team SSH containers
#
# Copies bot.py, bot_lib/, and bot.sh into the specified team(s) SSH containers and
# optionally starts the bot as a background process inside the container.
#
# Prerequisites on the host: docker CLI (to run `docker cp` and `docker exec`)
#
# Usage:
#   ./deploy.sh 2              # copy bot into team2-ssh and start loop
#   ./deploy.sh 2 3 4          # deploy to multiple teams at once
#   ./deploy.sh --copy-only 2  # copy files but don't start the bot
#   ./deploy.sh --stop 2       # kill the running bot in team2-ssh
#   ./deploy.sh --status       # show bot process status in all containers
#
# Config / knobs:
#   --config <file.json>          copy that JSON config file as /tmp/bot_config.json
#   --service-port <N>            target HTTP port (overrides config file)
#   --flag-re <REGEX>             flag regex (overrides config file)
#   --ip-pattern <PATTERN>        IP format with {team} placeholder
#   --actions <a,b,c>             comma-separated action list
#   --planner <ID>                scripted, recon_first, or module:object
#   --target-policy <POLICY>      all_opponents or selected
#   --target-teams <a,b,c>        target team ids for selected policy
#   --exploits <a,b,c>            legacy comma-separated exploit list
#   --no-stop-on-first            run all exploits even after a hit
#
# Environment overrides:
#   LOOP_INTERVAL=30 WATCHDOG=false ./deploy.sh 2 3
# =============================================================================
set -euo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${BOT_DIR}/.." && pwd)"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"
arena_config_load "${ROOT}"

NUM_TEAMS="${ARENA_TEAM_COUNT}"
SERVICE_PORT="${ARENA_SERVICE_PORT}"
IP_PATTERN="${ARENA_SERVICE_IP_PATTERN}"
LOOP_INTERVAL=${LOOP_INTERVAL:-${ARENA_BOT_LOOP_SECONDS}}
WATCHDOG=${WATCHDOG:-false}          # also monitor own service (true/false)

# Extra bot.py CLI args assembled from --service-port / --flag-re / etc.
EXTRA_BOT_ARGS=""
CONFIG_FILE_PATH=""

RED='\033[0;31m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log_ok()   { echo -e "${GREEN}[+]${NC} $*"; }
log_info() { echo -e "${CYAN}[*]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[!]${NC} $*"; }
log_err()  { echo -e "${RED}[-]${NC} $*" >&2; }

# ─────────────────────────────── helpers ───────────────────────────────────

container_name() { echo "team${1}-ssh"; }

container_running() {
    local state
    state=$(docker inspect --format='{{.State.Running}}' "$(container_name "$1")" 2>/dev/null || echo "false")
    [[ "$state" == "true" ]]
}

copy_bot() {
    local team_id=$1
    local cname; cname=$(container_name "$team_id")
    log_info "Copying bot files into ${cname} …"
    docker cp "${BOT_DIR}/bot.py" "${cname}:/tmp/bot.py"
    docker exec "$cname" bash -c "rm -rf /tmp/bot_lib"
    docker cp "${BOT_DIR}/bot_lib" "${cname}:/tmp/bot_lib"
    docker cp "${BOT_DIR}/bot.sh" "${cname}:/tmp/bot.sh"
    docker cp "${ARENA_CONFIG_FILE}" "${cname}:/tmp/arena.env"
    docker exec "$cname" chmod +x /tmp/bot.sh
    # Copy config file if one was specified (web UI writes it to a temp path)
    if [[ -n "$CONFIG_FILE_PATH" ]]; then
        docker cp "${CONFIG_FILE_PATH}" "${cname}:/tmp/bot_config.json"
        log_ok "Config copied to ${cname}:/tmp/bot_config.json"
    fi
    log_ok "Files copied to ${cname}:/tmp/"
}

start_bot() {
    local team_id=$1
    local cname; cname=$(container_name "$team_id")

    local bot_args="--teams ${NUM_TEAMS} --loop ${LOOP_INTERVAL} --service-port ${SERVICE_PORT} --ip-pattern ${IP_PATTERN}"
    [[ "$WATCHDOG" == "true" ]] && bot_args="${bot_args} --watchdog"
    # Append any extra args built from --service-port / --flag-re / etc.
    [[ -n "$EXTRA_BOT_ARGS" ]] && bot_args="${bot_args} ${EXTRA_BOT_ARGS}"

    log_info "Starting bot in ${cname} (interval=${LOOP_INTERVAL}s, watchdog=${WATCHDOG}) …"
    # Kill any existing bot first (ignore errors — nothing running is fine)
    docker exec "$cname" bash -c "pkill -f '/tmp/bot.py' 2>/dev/null; exit 0" || true
    # Start bot in background; setsid detaches it from the exec session so it
    # keeps running after `docker exec` returns
    # shellcheck disable=SC2086
    docker exec "$cname" bash -c \
        "setsid python3 -u /tmp/bot.py ${bot_args} > /tmp/bot.log 2>&1 &" || true
    # Poll up to 3 s for the process to appear
    local pid=""
    for _ in 1 2 3; do
        pid=$(docker exec "$cname" pgrep -f '/tmp/bot.py' 2>/dev/null) && break || true
        docker exec "$cname" bash -c 'sleep 1' || true
    done
    if [[ -n "$pid" ]]; then
        log_ok "Bot started in ${cname} (pid ${pid}) — logs: docker exec ${cname} tail -f /tmp/bot.log"
    else
        log_warn "Bot may have failed — check: docker exec ${cname} cat /tmp/bot.log"
    fi
}

stop_bot() {
    local team_id=$1
    local cname; cname=$(container_name "$team_id")
    log_info "Stopping bot in ${cname} …"
    docker exec "$cname" bash -c "pkill -f '/tmp/bot.py' 2>/dev/null && echo 'killed' || echo 'not running'"
}

bot_status() {
    local team_id=$1
    local cname; cname=$(container_name "$team_id")
    if ! container_running "$team_id"; then
        echo -e "  ${cname}  ${RED}container not running${NC}"
        return
    fi
    local pid
    pid=$(docker exec "$cname" pgrep -f '/tmp/bot.py' 2>/dev/null || true)
    if [[ -n "$pid" ]]; then
        echo -e "  ${cname}  ${GREEN}bot running (pid ${pid})${NC}"
    else
        echo -e "  ${cname}  ${YELLOW}bot not running${NC}"
    fi
}

show_logs() {
    local team_id=$1
    local cname; cname=$(container_name "$team_id")
    docker exec "$cname" tail -n 40 /tmp/bot.log 2>/dev/null \
        || log_warn "No log file yet in ${cname}"
}

# ─────────────────────────────── entry point ───────────────────────────────

usage() {
    echo "Usage:"
    echo "  $0 <team_id> [team_id ...]              # deploy (copy + start) bot"
    echo "  $0 --copy-only <team_id> [...]          # copy files only"
    echo "  $0 --stop <team_id> [...]               # stop running bot"
    echo "  $0 --logs <team_id>                     # tail bot logs"
    echo "  $0 --status                             # show bot status in all containers"
    echo ""
    echo "Config flags (can be combined with any mode):"
    echo "  --config <file.json>                    # path to bot_config.json on the host"
    echo "  --bot-name <NAME>                       # display name written into bot config"
    echo "  --planner <ID>                          # scripted, recon_first, or module:object"
    echo "  --target-policy <POLICY>                # all_opponents or selected"
    echo "  --target-teams <a,b,c>                  # targets for selected policy"
    echo "  --actions <a,b,c>                       # comma-separated action list"
    echo "  --service-port <N>                      # override target service port"
    echo "  --flag-re <REGEX>                       # override flag regex"
    echo "  --ip-pattern <PATTERN>                  # override IP pattern (use {team})"
    echo "  --exploits <a,b,c>                      # legacy comma-separated exploit list"
    echo "  --no-stop-on-first                      # run all exploits even after a hit"
    echo ""
    echo "Arena defaults come from: ${ARENA_CONFIG_FILE}"
    echo "Environment overrides:"
    echo "  LOOP_INTERVAL=30 WATCHDOG=false $0 2 3"
}

main() {
    if [[ $# -eq 0 ]]; then
        usage
        exit 1
    fi

    local mode="deploy"
    local teams=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --copy-only) mode="copy"; shift ;;
            --stop)      mode="stop"; shift ;;
            --logs)      mode="logs"; shift ;;
            --status)
                log_info "Bot status across all ${NUM_TEAMS} containers:"
                for i in $(seq 1 "$NUM_TEAMS"); do
                    bot_status "$i"
                done
                exit 0
                ;;
            # ── new config flags ───────────────────────────────────────
            --config)
                CONFIG_FILE_PATH="$2"; shift 2 ;;
            --bot-name)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --bot-name $2"; shift 2 ;;
            --planner)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --planner $2"; shift 2 ;;
            --target-policy)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --target-policy $2"; shift 2 ;;
            --target-teams)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --target-teams $2"; shift 2 ;;
            --actions)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --actions $2"; shift 2 ;;
            --service-port)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --service-port $2"; shift 2 ;;
            --flag-re)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --flag-re $2"; shift 2 ;;
            --ip-pattern)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --ip-pattern $2"; shift 2 ;;
            --exploits)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --exploits $2"; shift 2 ;;
            --no-stop-on-first)
                EXTRA_BOT_ARGS="${EXTRA_BOT_ARGS} --no-stop-on-first"; shift ;;
            # ──────────────────────────────────────────────────────────
            --help|-h) usage; exit 0 ;;
            [0-9]*) teams+=("$1"); shift ;;
            *)
                log_err "Unknown argument: $1"
                usage
                exit 1
                ;;
        esac
    done

    if [[ ${#teams[@]} -eq 0 ]]; then
        log_err "No team IDs specified"
        usage
        exit 1
    fi

    for team_id in "${teams[@]}"; do
        echo ""
        if ! container_running "$team_id"; then
            log_err "team${team_id}-ssh container is not running — start the platform first"
            continue
        fi

        case "$mode" in
            deploy)
                copy_bot "$team_id"
                start_bot "$team_id"
                ;;
            copy)
                copy_bot "$team_id"
                ;;
            stop)
                stop_bot "$team_id"
                ;;
            logs)
                show_logs "$team_id"
                ;;
        esac
    done
}

main "$@"
