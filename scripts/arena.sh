#!/usr/bin/env bash
# Unified Sandcastle arena lifecycle command.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
NETWORK_SMOKE="${SANDCASTLE_NETWORK_SMOKE:-${ROOT}/scripts/smoke-network.sh}"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

COMMAND="${1:-}"
if [[ -n "${COMMAND}" ]]; then
    shift
fi

REQUESTED_TEAMS=""
TIMEOUT_OVERRIDE=""
STATUS_FORMAT="text"
HEALTH_POLL_SECONDS="${SANDCASTLE_HEALTH_POLL_SECONDS:-1}"

usage() {
    cat <<'EOF'
Usage:
  ./scripts/arena.sh up [--teams N] [--timeout SEC]
  ./scripts/arena.sh status [--format text|tsv]
  ./scripts/arena.sh restart [--teams N] [--timeout SEC]
  ./scripts/arena.sh down
  ./scripts/arena.sh reset [--teams N] [--timeout SEC]

Commands:
  up        Validate and generate the topology, start infrastructure, recreate
            all vulnerable apps, and wait for health.
  status    Report gateways, vulnerable machines, apps, app health, and firewall.
  restart   Stop all containers while preserving source and data, then run up.
  down      Stop and remove containers while preserving source and app data.
  reset     Run down, delete vulnerable-app data volumes, then run up. Source
            patches under teams/generated are preserved.

Options:
  --teams N         Persist and start N teams.
  --timeout SEC     Override ARENA_STARTUP_TIMEOUT_SECONDS for this command.
  --format FORMAT   status output: text or tsv.
  -h, --help        Show this help text.
EOF
}

die() {
    echo "arena.sh: $*" >&2
    exit 1
}

parse_args() {
    while (($#)); do
        case "$1" in
            --teams|-t)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                REQUESTED_TEAMS="$2"
                shift 2
                ;;
            --teams=*)
                REQUESTED_TEAMS="${1#*=}"
                shift
                ;;
            --timeout)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                TIMEOUT_OVERRIDE="$2"
                shift 2
                ;;
            --timeout=*)
                TIMEOUT_OVERRIDE="${1#*=}"
                shift
                ;;
            --format)
                [[ $# -ge 2 ]] || die "$1 requires text or tsv"
                STATUS_FORMAT="$2"
                shift 2
                ;;
            --format=*)
                STATUS_FORMAT="${1#*=}"
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
}

validate_options() {
    if [[ -n "${REQUESTED_TEAMS}" ]]; then
        arena_config_require_int REQUESTED_TEAMS 1 250 ||
            die "invalid --teams value"
    fi
    if [[ -n "${TIMEOUT_OVERRIDE}" ]]; then
        arena_config_require_int TIMEOUT_OVERRIDE 1 86400 ||
            die "invalid --timeout value"
    fi
    arena_config_require_int HEALTH_POLL_SECONDS 1 3600 ||
        die "SANDCASTLE_HEALTH_POLL_SECONDS must be between 1 and 3600"
    [[ "${STATUS_FORMAT}" == "text" || "${STATUS_FORMAT}" == "tsv" ]] ||
        die "--format must be text or tsv"

    case "${COMMAND}" in
        status)
            [[ -z "${REQUESTED_TEAMS}" ]] ||
                die "--teams is not valid for status"
            [[ -z "${TIMEOUT_OVERRIDE}" ]] ||
                die "--timeout is not valid for status"
            ;;
        down)
            [[ -z "${REQUESTED_TEAMS}" ]] ||
                die "--teams is not valid for down"
            [[ -z "${TIMEOUT_OVERRIDE}" ]] ||
                die "--timeout is not valid for down"
            [[ "${STATUS_FORMAT}" == "text" ]] ||
                die "--format is only valid for status"
            ;;
        up|restart|reset)
            [[ "${STATUS_FORMAT}" == "text" ]] ||
                die "--format is only valid for status"
            ;;
    esac
}

require_docker() {
    command -v docker >/dev/null 2>&1 ||
        die "Docker CLI is not installed"
    docker info >/dev/null 2>&1 ||
        die "Docker daemon is not reachable"
    docker compose version >/dev/null 2>&1 ||
        die "Docker Compose plugin is not available"
}

verify_firewall_runtime() {
    docker exec sandcastle-firewall sh -ec '
        test "$(cat /proc/sys/net/bridge/bridge-nf-call-iptables)" = "1"
        iptables -t nat -C PREROUTING \
            -s "$CTF_NETWORK" \
            -d "$CTF_NETWORK" \
            -p tcp \
            -m comment \
            --comment sandcastle-firewall-transparent-proxy \
            -j REDIRECT \
            --to-ports "$PROXY_PORT"
        iptables -t filter -C INPUT \
            -s "$CTF_NETWORK" \
            -p tcp \
            --dport "$PROXY_PORT" \
            -m conntrack \
            --ctstate DNAT \
            -m comment \
            --comment sandcastle-firewall-proxy-input \
            -j ACCEPT
        ss -lnt | grep -Eq "[:.]${PROXY_PORT}[[:space:]]"
        ss -lnt | grep -Eq "[:.]${WS_PORT}[[:space:]]"
    ' >/dev/null ||
        die "firewall enforcement rule or listeners are inactive"
}

verify_network_path() {
    if [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" ]]; then
        echo "[*] Skipping legacy firewall network smoke in DinD mode."
        return 0
    fi
    [[ -x "${NETWORK_SMOKE}" ]] ||
        die "missing executable network smoke test: ${NETWORK_SMOKE}"
    "${NETWORK_SMOKE}" ||
        die "firewall network smoke test failed"
}

top_compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

team_compose_file() {
    local team_id="$1"
    printf '%s/teams/generated/team%s/example-vuln/docker-compose.yml' \
        "${ROOT}" "${team_id}"
}

container_state() {
    local name="$1"
    local state=""

    if state="$(docker inspect --format '{{.State.Status}}' "${name}" 2>/dev/null)" &&
       [[ -n "${state}" ]]; then
        printf '%s\n' "${state}"
    else
        printf 'absent\n'
    fi
}

container_exists() {
    docker inspect "$1" >/dev/null 2>&1
}

team_username() {
    arena_config_render_team_value "${ARENA_TEAM_USERNAME_PATTERN}" "$1"
}

dind_daemon_ready() {
    local team_id="$1"
    local machine="team${team_id}-vuln"

    [[ "$(container_state "team${team_id}-dind")" == "running" ]] || return 1
    [[ "$(container_state "${machine}")" == "running" ]] || return 1
    docker exec "${machine}" sh -lc \
        'command -v docker >/dev/null && docker info >/dev/null' \
        >/dev/null 2>&1
}

dind_app_diagnostics() {
    local team_id="$1"
    local machine="team${team_id}-vuln"
    local username service_dir

    [[ "${ARENA_ISOLATION_MODE}" == "dind" ]] || return 0
    [[ "$(container_state "${machine}")" == "running" ]] || return 0

    username="$(team_username "${team_id}")"
    service_dir="/home/${username}/example-vuln"
    {
        echo "--- team${team_id} nested DinD compose diagnostics ---"
        docker exec "${machine}" sh -lc \
            "cd '${service_dir}' && docker compose ps || true"
        docker exec "${machine}" sh -lc \
            "cd '${service_dir}' && docker compose logs --no-color --tail=120 || true"
    } >&2 || true
}

app_container_state() {
    local team_id="$1"
    local app="team${team_id}-vuln-app"
    local state=""

    if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
        if state="$(
            docker exec "team${team_id}-vuln" \
                docker inspect --format '{{.State.Status}}' "${app}" 2>/dev/null
        )" && [[ -n "${state}" ]]; then
            printf '%s\n' "${state}"
        else
            printf 'absent\n'
        fi
        return
    fi

    container_state "${app}"
}

app_is_healthy() {
    local team_id="$1"
    local machine="team${team_id}-vuln"
    local app="team${team_id}-vuln-app"

    [[ "$(container_state "${machine}")" == "running" ]] || return 1
    [[ "$(app_container_state "${team_id}")" == "running" ]] || return 1

    if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
        # In DinD mode the app runs inside the nested Docker daemon.
        # network_mode: container:teamN-vuln refers to the *inner* teamN-vuln
        # container, so the app is reachable on 127.0.0.1 only from within
        # that inner network namespace — reached via a nested docker exec.
        docker exec "${machine}" \
            docker exec "${app}" \
            curl -fsS --max-time 2 "http://127.0.0.1:${ARENA_SERVICE_PORT}/health" \
            > /dev/null 2>&1
    else
        docker exec "${machine}" \
            curl -fsS --max-time 1 "http://127.0.0.1:${ARENA_SERVICE_PORT}/health" \
            > /dev/null 2>&1
    fi
}

run_setup() {
    local -a args=(--remove-orphan-containers)

    if [[ -n "${REQUESTED_TEAMS}" ]]; then
        args+=(--teams "${REQUESTED_TEAMS}")
    fi

    echo "[*] Validating configuration and reconciling generated topology..."
    "${ROOT}/scripts/setup.sh" "${args[@]}"
    arena_config_load "${ROOT}"
}

wait_for_infrastructure() {
    local timeout="$1"
    local attempts=$((timeout / HEALTH_POLL_SECONDS + 1))
    local attempt id name
    local -a pending=()

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        pending=()
        for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
            for name in "team${id}-ssh" "team${id}-vuln"; do
                if [[ "$(container_state "${name}")" != "running" ]]; then
                    pending+=("${name}")
                fi
            done
            if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
                if [[ "$(container_state "team${id}-dind")" != "running" ]]; then
                    pending+=("team${id}-dind")
                elif ! dind_daemon_ready "${id}"; then
                    pending+=("team${id}-dind-daemon")
                fi
            fi
        done
        if [[ "$(container_state sandcastle-firewall)" != "running" ]]; then
            pending+=("sandcastle-firewall")
        fi
        if [[ "$(container_state sandcastle-gameserver)" != "running" ]]; then
            pending+=("sandcastle-gameserver")
        fi
        if [[ "$(container_state sandcastle-bot-controller)" != "running" ]]; then
            pending+=("sandcastle-bot-controller")
        fi
        if [[ "$(container_state sandcastle-visualizer)" != "running" ]]; then
            pending+=("sandcastle-visualizer")
        fi

        ((${#pending[@]} == 0)) && return 0
        ((attempt < attempts)) && sleep "${HEALTH_POLL_SECONDS}"
    done

    echo "arena.sh: infrastructure failed to become ready: ${pending[*]}" >&2
    return 1
}

recreate_apps() {
    local id app compose_file username service_dir

    echo "[*] Recreating vulnerable apps against the current parent containers..."
    for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
        app="team${id}-vuln-app"
        compose_file="$(team_compose_file "${id}")"
        [[ -s "${compose_file}" ]] ||
            die "missing generated app Compose file: ${compose_file#"${ROOT}"/}"

        if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
            username="$(team_username "${id}")"
            service_dir="/home/${username}/example-vuln"
            docker exec "team${id}-vuln" sh -lc \
                "cd '${service_dir}' && docker rm -f '${app}' >/dev/null 2>&1 || true"
            if ! docker exec "team${id}-vuln" sh -lc \
                "cd '${service_dir}' && docker compose up -d --build --force-recreate --remove-orphans"; then
                echo "arena.sh: nested DinD compose failed for team${id}" >&2
                dind_app_diagnostics "${id}"
                return 1
            fi
        else
            # network_mode: container:<parent> stores the parent container ID.
            # Removing the old app before Compose up prevents stale namespace reuse.
            if container_exists "${app}"; then
                docker rm -f "${app}" >/dev/null
            fi
            docker compose -f "${compose_file}" \
                up -d --build --force-recreate --remove-orphans
        fi
    done
}

wait_for_apps() {
    local timeout="$1"
    local attempts=$((timeout / HEALTH_POLL_SECONDS + 1))
    local attempt id index
    local -a pending=()
    local -a team_ids=()
    local -a health_pids=()

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        pending=()
        team_ids=()
        health_pids=()
        for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
            app_is_healthy "${id}" &
            team_ids+=("${id}")
            health_pids+=("$!")
        done
        for index in "${!health_pids[@]}"; do
            if ! wait "${health_pids[${index}]}"; then
                pending+=("team${team_ids[${index}]}-vuln-app")
            fi
        done

        ((${#pending[@]} == 0)) && return 0
        ((attempt < attempts)) && sleep "${HEALTH_POLL_SECONDS}"
    done

    echo "arena.sh: app health timeout after ${timeout}s: ${pending[*]}" >&2
    return 1
}

component_ready() {
    local state="$1"
    local health="${2:--}"
    [[ "${state}" == "running" ]] &&
        [[ "${health}" == "-" || "${health}" == "healthy" ]]
}

print_status() {
    local id gateway machine app gateway_state machine_state app_state health
    local firewall_state bot_controller_state bot_controller_health
    local visualizer_state visualizer_health
    local ready=0

    if [[ "${STATUS_FORMAT}" == "text" ]]; then
        printf '%-8s %-10s %-12s %-10s\n' "TEAM" "COMPONENT" "STATE" "HEALTH"
    else
        printf 'TEAM\tCOMPONENT\tSTATE\tHEALTH\n'
    fi

    for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
        gateway="team${id}-ssh"
        machine="team${id}-vuln"
        app="team${id}-vuln-app"
        gateway_state="$(container_state "${gateway}")"
        machine_state="$(container_state "${machine}")"
        app_state="$(app_container_state "${id}")"
        health="not-running"
        if [[ "${app_state}" == "running" ]]; then
            if app_is_healthy "${id}"; then
                health="healthy"
            else
                health="unhealthy"
            fi
        fi

        if [[ "${STATUS_FORMAT}" == "text" ]]; then
            printf 'team%-4s %-10s %-12s %-10s\n' "${id}" "gateway" "${gateway_state}" "-"
            printf 'team%-4s %-10s %-12s %-10s\n' "${id}" "machine" "${machine_state}" "-"
            if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
                printf 'team%-4s %-10s %-12s %-10s\n' "${id}" "dind" "$(container_state "team${id}-dind")" "-"
            fi
            printf 'team%-4s %-10s %-12s %-10s\n' "${id}" "app" "${app_state}" "${health}"
        else
            printf 'team%s\tgateway\t%s\t-\n' "${id}" "${gateway_state}"
            printf 'team%s\tmachine\t%s\t-\n' "${id}" "${machine_state}"
            if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
                printf 'team%s\tdind\t%s\t-\n' "${id}" "$(container_state "team${id}-dind")"
            fi
            printf 'team%s\tapp\t%s\t%s\n' "${id}" "${app_state}" "${health}"
        fi

        component_ready "${gateway_state}" || ready=1
        component_ready "${machine_state}" || ready=1
        if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
            component_ready "$(container_state "team${id}-dind")" || ready=1
        fi
        component_ready "${app_state}" "${health}" || ready=1
    done

    firewall_state="$(container_state sandcastle-firewall)"
    gameserver_state="$(container_state sandcastle-gameserver)"
    gameserver_health="-"
    if [[ "${gameserver_state}" == "running" ]]; then
        if docker exec sandcastle-gameserver python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" >/dev/null 2>&1; then
            gameserver_health="healthy"
        else
            gameserver_health="unhealthy"
        fi
    fi
    bot_controller_state="$(container_state sandcastle-bot-controller)"
    bot_controller_health="-"
    if [[ "${bot_controller_state}" == "running" ]]; then
        if docker exec sandcastle-bot-controller python3 -c \
            "import urllib.request; urllib.request.urlopen('http://localhost:${ARENA_BOT_API_PORT}/health')" \
            >/dev/null 2>&1; then
            bot_controller_health="healthy"
        else
            bot_controller_health="unhealthy"
        fi
    fi
    visualizer_state="$(container_state sandcastle-visualizer)"
    visualizer_health="-"
    if [[ "${visualizer_state}" == "running" ]]; then
        if docker exec sandcastle-visualizer wget -q -O - http://127.0.0.1/ >/dev/null 2>&1; then
            visualizer_health="healthy"
        else
            visualizer_health="unhealthy"
        fi
    fi

    if [[ "${STATUS_FORMAT}" == "text" ]]; then
        printf '%-8s %-10s %-12s %-10s\n' "-" "firewall" "${firewall_state}" "-"
        printf '%-8s %-10s %-12s %-10s\n' "-" "gameserver" "${gameserver_state}" "${gameserver_health}"
        printf '%-8s %-10s %-12s %-10s\n' "-" "bot-api" "${bot_controller_state}" "${bot_controller_health}"
        printf '%-8s %-10s %-12s %-10s\n' "-" "visualizer" "${visualizer_state}" "${visualizer_health}"
    else
        printf -- '-\tfirewall\t%s\t-\n' "${firewall_state}"
        printf -- '-\tgameserver\t%s\t%s\n' "${gameserver_state}" "${gameserver_health}"
        printf -- '-\tbot-api\t%s\t%s\n' "${bot_controller_state}" "${bot_controller_health}"
        printf -- '-\tvisualizer\t%s\t%s\n' "${visualizer_state}" "${visualizer_health}"
    fi
    component_ready "${firewall_state}" || ready=1
    component_ready "${gameserver_state}" "${gameserver_health}" || ready=1
    component_ready "${bot_controller_state}" "${bot_controller_health}" || ready=1
    component_ready "${visualizer_state}" "${visualizer_health}" || ready=1

    return "${ready}"
}

collect_app_containers() {
    local -a containers=()
    local -a matches=()

    [[ "${ARENA_ISOLATION_MODE:-trusted}" != "dind" ]] || return 0

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "label=sandcastle.role=vuln-app" 2>/dev/null)
    containers+=("${matches[@]}")
    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker ps -aq --filter "name=^/team[0-9]+-vuln-app$" 2>/dev/null)
    containers+=("${matches[@]}")

    ((${#containers[@]} > 0)) || return 0
    printf '%s\n' "${containers[@]}" | awk 'NF' | sort -u
}

down_arena() {
    local -a app_containers=()

    echo "[*] Stopping vulnerable apps while preserving data volumes..."
    while IFS= read -r app_container; do
        app_containers+=("${app_container}")
    done < <(collect_app_containers)
    if ((${#app_containers[@]} > 0)); then
        docker rm -f "${app_containers[@]}" >/dev/null
    fi

    echo "[*] Stopping infrastructure..."
    if [[ -f "${COMPOSE_FILE}" ]]; then
        top_compose down --remove-orphans
    fi
    echo "[+] Arena stopped. Source patches and vulnerable-app data volumes were preserved."
}

remove_app_data() {
    local -a volumes=()
    local -a matches=()
    local id

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker volume ls -q --filter "label=sandcastle.role=vuln-data" 2>/dev/null)
    volumes+=("${matches[@]}")
    if [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" ]]; then
        for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
            volumes+=("sandcastle_team${id}-dind-data" "sandcastle_team${id}-dind-run")
        done
    fi
    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(docker volume ls -q --filter "name=^sandcastle_team[0-9]+-data$" 2>/dev/null)
    volumes+=("${matches[@]}")

    ((${#volumes[@]} > 0)) || {
        echo "[*] No vulnerable-app data volumes found."
        return 0
    }

    matches=()
    while IFS= read -r match; do
        matches+=("${match}")
    done < <(printf '%s\n' "${volumes[@]}" | awk 'NF' | sort -u)
    volumes=("${matches[@]}")
    echo "[!] RESET: deleting vulnerable-app data volumes: ${volumes[*]}"
    docker volume rm -f "${volumes[@]}" >/dev/null
}

print_trusted_mode_banner() {
    if [[ "${SANDCASTLE_SKIP_TRUSTED_BANNER:-0}" == "1" ]]; then
        return
    fi
    printf '\n'
    printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n'
    printf '!!  SANDCASTLE TRUSTED-LOCAL MODE                                !!\n'
    printf '!!                                                                !!\n'
    printf '!!  Each vulnerable machine has FULL HOST DOCKER DAEMON ACCESS.  !!\n'
    printf '!!  A participant with SSH access can escape to the host.         !!\n'
    printf '!!                                                                !!\n'
    printf '!!  Only run this mode with trusted participants on a private     !!\n'
    printf '!!  network.  DO NOT use it for public events or with external    !!\n'
    printf '!!  teams.                                                        !!\n'
    printf '!!                                                                !!\n'
    printf '!!  Read docs/THREAT_MODEL.md before sharing this arena.         !!\n'
    printf '!!  Run ./scripts/doctor.sh to review active warnings.           !!\n'
    printf '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n'
    printf '\n'
}

up_arena() {
    local setup_complete="${1:-0}"
    local timeout

    if [[ "${ARENA_ISOLATION_MODE}" == "isolated" ]]; then
        mkdir -p /run/sandcastle
        chmod 755 /run/sandcastle
    elif [[ "${ARENA_ISOLATION_MODE}" == "trusted" ]]; then
        print_trusted_mode_banner
    fi

    if ((setup_complete == 0)); then
        run_setup
    fi
    timeout="${TIMEOUT_OVERRIDE:-${ARENA_STARTUP_TIMEOUT_SECONDS}}"

    echo "[*] Building and starting infrastructure..."
    top_compose up -d --build --remove-orphans
    wait_for_infrastructure "${timeout}" ||
        die "infrastructure startup failed; run ./scripts/arena.sh status"
    verify_firewall_runtime

    recreate_apps ||
        die "vulnerable app recreation failed"
    wait_for_apps "${timeout}" || {
        STATUS_FORMAT="text"
        print_status || true
        if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
            local id
            for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
                dind_app_diagnostics "${id}"
            done
        fi
        die "one or more vulnerable apps failed health checks"
    }
    verify_network_path

    echo
    echo "[+] Complete arena is healthy."
    STATUS_FORMAT="text"
    print_status || true
}

main() {
    case "${COMMAND}" in
        up|status|restart|down|reset) ;;
        -h|--help|"")
            usage
            [[ -n "${COMMAND}" ]] || exit 2
            exit 0
            ;;
        *)
            usage >&2
            die "unknown command: ${COMMAND}"
            ;;
    esac

    parse_args "$@"
    if [[ "${COMMAND}" != "down" ]]; then
        arena_config_load "${ROOT}" || exit 1
    fi
    validate_options
    require_docker
    case "${COMMAND}" in
        up)
            up_arena
            ;;
        status)
            print_status
            ;;
        restart)
            run_setup
            down_arena
            up_arena 1
            ;;
        down)
            down_arena
            ;;
        reset)
            run_setup
            down_arena
            remove_app_data
            up_arena 1
            ;;
    esac
}

main "$@"
