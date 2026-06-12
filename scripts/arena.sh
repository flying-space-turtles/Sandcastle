#!/usr/bin/env bash
# Unified Sandcastle arena lifecycle command.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
FIREWALL_PREFLIGHT="${SANDCASTLE_FIREWALL_PREFLIGHT:-${ROOT}/scripts/firewall-preflight.sh}"
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

verify_firewall_host() {
    [[ -x "${FIREWALL_PREFLIGHT}" ]] ||
        die "missing executable firewall preflight: ${FIREWALL_PREFLIGHT}"
    "${FIREWALL_PREFLIGHT}" --check ||
        die "firewall host preflight failed"
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
        ss -lnt | grep -Eq "[:.]${PROXY_PORT}[[:space:]]"
        ss -lnt | grep -Eq "[:.]${WS_PORT}[[:space:]]"
    ' >/dev/null ||
        die "firewall enforcement rule or listeners are inactive"
}

verify_network_path() {
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

app_is_healthy() {
    local team_id="$1"
    local machine="team${team_id}-vuln"
    local app="team${team_id}-vuln-app"

    [[ "$(container_state "${machine}")" == "running" ]] || return 1
    [[ "$(container_state "${app}")" == "running" ]] || return 1
    docker exec "${machine}" \
        curl -fsS --max-time 1 "http://127.0.0.1:${ARENA_SERVICE_PORT}/health" \
        >/dev/null 2>&1
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
        done
        if [[ "$(container_state sandcastle-firewall)" != "running" ]]; then
            pending+=("sandcastle-firewall")
        fi

        ((${#pending[@]} == 0)) && return 0
        ((attempt < attempts)) && sleep "${HEALTH_POLL_SECONDS}"
    done

    echo "arena.sh: infrastructure failed to become ready: ${pending[*]}" >&2
    return 1
}

recreate_apps() {
    local id app compose_file

    echo "[*] Recreating vulnerable apps against the current parent containers..."
    for ((id = 1; id <= ARENA_TEAM_COUNT; id++)); do
        app="team${id}-vuln-app"
        compose_file="$(team_compose_file "${id}")"
        [[ -s "${compose_file}" ]] ||
            die "missing generated app Compose file: ${compose_file#${ROOT}/}"

        # network_mode: container:<parent> stores the parent container ID.
        # Removing the old app before Compose up prevents stale namespace reuse.
        if container_exists "${app}"; then
            docker rm -f "${app}" >/dev/null
        fi
        docker compose -f "${compose_file}" \
            up -d --build --force-recreate --remove-orphans
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
    local firewall_state
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
        app_state="$(container_state "${app}")"
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
            printf 'team%-4s %-10s %-12s %-10s\n' "${id}" "app" "${app_state}" "${health}"
        else
            printf 'team%s\tgateway\t%s\t-\n' "${id}" "${gateway_state}"
            printf 'team%s\tmachine\t%s\t-\n' "${id}" "${machine_state}"
            printf 'team%s\tapp\t%s\t%s\n' "${id}" "${app_state}" "${health}"
        fi

        component_ready "${gateway_state}" || ready=1
        component_ready "${machine_state}" || ready=1
        component_ready "${app_state}" "${health}" || ready=1
    done

    firewall_state="$(container_state sandcastle-firewall)"
    if [[ "${STATUS_FORMAT}" == "text" ]]; then
        printf '%-8s %-10s %-12s %-10s\n' "-" "firewall" "${firewall_state}" "-"
    else
        printf -- '-\tfirewall\t%s\t-\n' "${firewall_state}"
    fi
    component_ready "${firewall_state}" || ready=1

    return "${ready}"
}

collect_app_containers() {
    local -a containers=()
    local -a matches=()

    mapfile -t matches < <(
        docker ps -aq --filter "label=sandcastle.role=vuln-app" 2>/dev/null
    )
    containers+=("${matches[@]}")
    mapfile -t matches < <(
        docker ps -aq --filter "name=^/team[0-9]+-vuln-app$" 2>/dev/null
    )
    containers+=("${matches[@]}")

    ((${#containers[@]} > 0)) || return 0
    printf '%s\n' "${containers[@]}" | awk 'NF' | sort -u
}

down_arena() {
    local -a app_containers=()

    echo "[*] Stopping vulnerable apps while preserving data volumes..."
    mapfile -t app_containers < <(collect_app_containers)
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

    mapfile -t matches < <(
        docker volume ls -q --filter "label=sandcastle.role=vuln-data" 2>/dev/null
    )
    volumes+=("${matches[@]}")
    mapfile -t matches < <(
        docker volume ls -q --filter "name=^sandcastle_team[0-9]+-data$" 2>/dev/null
    )
    volumes+=("${matches[@]}")

    ((${#volumes[@]} > 0)) || {
        echo "[*] No vulnerable-app data volumes found."
        return 0
    }

    mapfile -t volumes < <(printf '%s\n' "${volumes[@]}" | awk 'NF' | sort -u)
    echo "[!] RESET: deleting vulnerable-app data volumes: ${volumes[*]}"
    docker volume rm -f "${volumes[@]}" >/dev/null
}

up_arena() {
    local setup_complete="${1:-0}"
    local timeout

    if ((setup_complete == 0)); then
        run_setup
    fi
    timeout="${TIMEOUT_OVERRIDE:-${ARENA_STARTUP_TIMEOUT_SECONDS}}"

    echo "[*] Building and starting infrastructure..."
    top_compose up -d --build --remove-orphans
    wait_for_infrastructure "${timeout}" ||
        die "infrastructure startup failed; run ./scripts/arena.sh status"
    verify_firewall_runtime

    recreate_apps
    wait_for_apps "${timeout}" || {
        STATUS_FORMAT="text"
        print_status || true
        die "one or more vulnerable apps failed health checks"
    }
    verify_network_path

    echo
    echo "[+] Complete arena is healthy."
    STATUS_FORMAT="text"
    print_status
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
        up|restart|reset)
            verify_firewall_host
            ;;
    esac

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
