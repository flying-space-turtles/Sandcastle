#!/usr/bin/env bash
# Read-only Sandcastle host and arena readiness diagnostics.

set -uo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
DOCKER_SOCKET="${DOCTOR_DOCKER_SOCKET:-/var/run/docker.sock}"
RULE_COMMENT="sandcastle-firewall-transparent-proxy"
FORMAT="text"
USE_COLOR=0
COLOR_MODE="auto"

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

DOCKER_DAEMON=0
DOCKER_COMPOSE=0
RUNTIME_ACTIVE=0
CONFIG_LOADED=0
DESIRED_SUBNET=""
COMPOSE_SUBNET=""

declare -a CONFIGURED_IDS=()
declare -a SSH_IDS=()
declare -a VULN_IDS=()
declare -a CONTAINER_STATE_KEYS=()
declare -a CONTAINER_STATE_VALUES=()
declare -a REQUIRED_PORT_OWNER_KEYS=()
declare -a REQUIRED_PORT_OWNER_VALUES=()

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

usage() {
    cat <<'EOF'
Usage: ./scripts/doctor.sh [--format text|tsv] [--color|--no-color]

Run read-only checks for Sandcastle host compatibility and arena readiness.

Formats:
  text    Human-readable PASS/WARN/FAIL output (default)
  tsv     STATUS<TAB>CHECK_ID<TAB>MESSAGE<TAB>REMEDIATION

Exit codes:
  0       No blocking failures
  1       One or more blocking failures
  2       Invalid arguments
EOF
}

while (($# > 0)); do
    case "$1" in
        --format)
            if (($# < 2)); then
                echo "doctor.sh: --format requires text or tsv" >&2
                exit 2
            fi
            FORMAT="$2"
            shift 2
            ;;
        --format=*)
            FORMAT="${1#*=}"
            shift
            ;;
        --color)
            COLOR_MODE="always"
            shift
            ;;
        --no-color)
            COLOR_MODE="never"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "doctor.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "${FORMAT}" != "text" && "${FORMAT}" != "tsv" ]]; then
    echo "doctor.sh: unsupported format: ${FORMAT}" >&2
    exit 2
fi

if [[ "${FORMAT}" == "text" ]]; then
    case "${COLOR_MODE}" in
        always) USE_COLOR=1 ;;
        never) USE_COLOR=0 ;;
        auto)
            if [[ -t 1 ]]; then
                USE_COLOR=1
            fi
            ;;
    esac
fi

sanitize_field() {
    local value="$1"
    value="${value//$'\t'/ }"
    value="${value//$'\n'/ }"
    printf '%s' "${value}"
}

report() {
    local status="$1"
    local check_id="$2"
    local message="$3"
    local remediation="${4:-}"
    local label="${status}"

    case "${status}" in
        PASS) PASS_COUNT=$((PASS_COUNT + 1)) ;;
        WARN) WARN_COUNT=$((WARN_COUNT + 1)) ;;
        FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
        *)
            echo "doctor.sh: internal error: invalid status ${status}" >&2
            exit 2
            ;;
    esac

    if [[ "${FORMAT}" == "tsv" ]]; then
        printf '%s\t%s\t%s\t%s\n' \
            "${status}" \
            "$(sanitize_field "${check_id}")" \
            "$(sanitize_field "${message}")" \
            "$(sanitize_field "${remediation}")"
        return
    fi

    if ((USE_COLOR)); then
        case "${status}" in
            PASS) label=$'\033[32mPASS\033[0m' ;;
            WARN) label=$'\033[33mWARN\033[0m' ;;
            FAIL) label=$'\033[31mFAIL\033[0m' ;;
        esac
    fi

    printf '[%b] %-28s %s\n' "${label}" "${check_id}" "${message}"
    if [[ -n "${remediation}" && "${status}" != "PASS" ]]; then
        printf '       fix: %s\n' "${remediation}"
    fi
}

join_csv() {
    local IFS=", "
    printf '%s' "$*"
}

array_contains() {
    local needle="$1"
    shift
    local item
    for item in "$@"; do
        if [[ "${item}" == "${needle}" ]]; then
            return 0
        fi
    done
    return 1
}

container_state_set() {
    local key="$1"
    local value="$2"
    local index

    for index in "${!CONTAINER_STATE_KEYS[@]}"; do
        if [[ "${CONTAINER_STATE_KEYS[${index}]}" == "${key}" ]]; then
            CONTAINER_STATE_VALUES[${index}]="${value}"
            return
        fi
    done
    CONTAINER_STATE_KEYS+=("${key}")
    CONTAINER_STATE_VALUES+=("${value}")
}

container_state_get() {
    local key="$1"
    local default="${2:-}"
    local index

    for index in "${!CONTAINER_STATE_KEYS[@]}"; do
        if [[ "${CONTAINER_STATE_KEYS[${index}]}" == "${key}" ]]; then
            printf '%s\n' "${CONTAINER_STATE_VALUES[${index}]}"
            return
        fi
    done
    printf '%s\n' "${default}"
}

container_state_keys() {
    ((${#CONTAINER_STATE_KEYS[@]} == 0)) || printf '%s\n' "${CONTAINER_STATE_KEYS[@]}"
}

required_port_owner_set() {
    local key="$1"
    local value="$2"
    local index

    for index in "${!REQUIRED_PORT_OWNER_KEYS[@]}"; do
        if [[ "${REQUIRED_PORT_OWNER_KEYS[${index}]}" == "${key}" ]]; then
            REQUIRED_PORT_OWNER_VALUES[${index}]="${value}"
            return
        fi
    done
    REQUIRED_PORT_OWNER_KEYS+=("${key}")
    REQUIRED_PORT_OWNER_VALUES+=("${value}")
}

required_port_owner_get() {
    local key="$1"
    local default="${2:-}"
    local index

    for index in "${!REQUIRED_PORT_OWNER_KEYS[@]}"; do
        if [[ "${REQUIRED_PORT_OWNER_KEYS[${index}]}" == "${key}" ]]; then
            printf '%s\n' "${REQUIRED_PORT_OWNER_VALUES[${index}]}"
            return
        fi
    done
    printf '%s\n' "${default}"
}

required_port_owner_has_value() {
    local value="$1"
    local index

    for index in "${!REQUIRED_PORT_OWNER_VALUES[@]}"; do
        [[ "${REQUIRED_PORT_OWNER_VALUES[${index}]}" == "${value}" ]] && return 0
    done
    return 1
}

required_port_owner_keys() {
    ((${#REQUIRED_PORT_OWNER_KEYS[@]} == 0)) || printf '%s\n' "${REQUIRED_PORT_OWNER_KEYS[@]}"
}

cidr_overlaps() {
    local first="$1"
    local second="$2"
    python3 -c '
import ipaddress
import sys

try:
    left = ipaddress.ip_network(sys.argv[1], strict=False)
    right = ipaddress.ip_network(sys.argv[2], strict=False)
except ValueError:
    raise SystemExit(2)
raise SystemExit(0 if left.overlaps(right) else 1)
' "${first}" "${second}" >/dev/null 2>&1
}

check_arena_configuration() {
    ARENA_CONFIG_SILENT=1
    if arena_config_load "${ROOT}"; then
        CONFIG_LOADED=1
        DESIRED_SUBNET="${ARENA_CTF_SUBNET}"
        report PASS arena.config \
            "Loaded canonical configuration from ${ARENA_CONFIG_FILE#"${ROOT}"/}."
    else
        report FAIL arena.config \
            "Canonical arena configuration is invalid: ${ARENA_CONFIG_ERROR:-unknown error}." \
            "Fix config/arena.env, then rerun ./scripts/doctor.sh."
    fi
    unset ARENA_CONFIG_SILENT
}

load_compose_metadata() {
    local line current_service=""
    local -a ssh_raw=()
    local -a vuln_raw=()
    local -a configured_raw=()

    if [[ ! -f "${COMPOSE_FILE}" ]]; then
        report FAIL compose.file \
            "Missing ${COMPOSE_FILE#"${ROOT}"/}." \
            "Run ./scripts/setup.sh --teams <N>."
        return
    fi
    report PASS compose.file "Found ${COMPOSE_FILE#"${ROOT}"/}."

    while IFS= read -r line; do
        if [[ "${line}" =~ ^[[:space:]]{2}team([0-9]+)-ssh: ]]; then
            current_service="team${BASH_REMATCH[1]}-ssh"
            ssh_raw+=("${BASH_REMATCH[1]}")
            configured_raw+=("${BASH_REMATCH[1]}")
        elif [[ "${line}" =~ ^[[:space:]]{2}team([0-9]+)-vuln: ]]; then
            current_service="team${BASH_REMATCH[1]}-vuln"
            vuln_raw+=("${BASH_REMATCH[1]}")
            configured_raw+=("${BASH_REMATCH[1]}")
        elif [[ "${line}" =~ ^[[:space:]]{2}[a-zA-Z0-9_.-]+: ]]; then
            current_service=""
        fi

        if [[ -n "${current_service}" && "${line}" =~ ^[[:space:]]+-[[:space:]]*\"?([0-9]+):([0-9]+) ]]; then
            required_port_owner_set "${BASH_REMATCH[1]}" "${current_service}"
        fi

        if [[ -z "${COMPOSE_SUBNET}" && "${line}" =~ subnet:[[:space:]]*([^[:space:]#]+) ]]; then
            COMPOSE_SUBNET="${BASH_REMATCH[1]}"
        fi
    done < "${COMPOSE_FILE}"

    SSH_IDS=()
    while IFS= read -r id; do
        SSH_IDS+=("${id}")
    done < <(printf '%s\n' "${ssh_raw[@]}" | awk 'NF' | sort -nu)
    VULN_IDS=()
    while IFS= read -r id; do
        VULN_IDS+=("${id}")
    done < <(printf '%s\n' "${vuln_raw[@]}" | awk 'NF' | sort -nu)
    CONFIGURED_IDS=()
    while IFS= read -r id; do
        CONFIGURED_IDS+=("${id}")
    done < <(printf '%s\n' "${configured_raw[@]}" | awk 'NF' | sort -nu)

    if ((${#CONFIGURED_IDS[@]} == 0)); then
        report FAIL compose.teams \
            "No generated team services were found." \
            "Run ./scripts/setup.sh --teams <N> and inspect docker-compose.yml."
        return
    fi

    local ssh_csv vuln_csv configured_csv expected=1 contiguous=1
    ssh_csv="$(join_csv "${SSH_IDS[@]}")"
    vuln_csv="$(join_csv "${VULN_IDS[@]}")"
    configured_csv="$(join_csv "${CONFIGURED_IDS[@]}")"

    for id in "${CONFIGURED_IDS[@]}"; do
        if ((id != expected)); then
            contiguous=0
            break
        fi
        expected=$((expected + 1))
    done

    if [[ "${ssh_csv}" != "${vuln_csv}" ]]; then
        report FAIL compose.teams \
            "SSH teams [${ssh_csv}] do not match vulnerable-machine teams [${vuln_csv}]." \
            "Regenerate with ./scripts/setup.sh --teams <N>."
    elif ((contiguous == 0)); then
        report FAIL compose.teams \
            "Configured team IDs are not contiguous from 1: [${configured_csv}]." \
            "Regenerate with ./scripts/setup.sh --teams ${#CONFIGURED_IDS[@]}."
    else
        report PASS compose.teams \
            "Configured ${#CONFIGURED_IDS[@]} contiguous team(s): [${configured_csv}]."
    fi

    if ((CONFIG_LOADED)) && ((${#CONFIGURED_IDS[@]} != ARENA_TEAM_COUNT)); then
        report FAIL compose.config-drift \
            "Compose has ${#CONFIGURED_IDS[@]} team(s), but config/arena.env requires ${ARENA_TEAM_COUNT}." \
            "Run ./scripts/setup.sh to regenerate from the canonical configuration."
    elif ((CONFIG_LOADED)); then
        report PASS compose.config-drift \
            "Compose team count matches config/arena.env."
    fi

    if [[ -z "${COMPOSE_SUBNET}" ]]; then
        report FAIL network.config \
            "No CTF subnet was found in docker-compose.yml." \
            "Regenerate with ./scripts/setup.sh."
    elif ((CONFIG_LOADED)) && [[ "${COMPOSE_SUBNET}" != "${ARENA_CTF_SUBNET}" ]]; then
        report FAIL network.config \
            "Compose subnet ${COMPOSE_SUBNET} does not match config/arena.env (${ARENA_CTF_SUBNET})." \
            "Run ./scripts/setup.sh to regenerate from the canonical configuration."
    else
        report PASS network.config "Configured CTF subnet is ${COMPOSE_SUBNET}."
    fi
}

check_host_and_docker() {
    local host_os docker_os current_user socket_type
    host_os="$(uname -s 2>/dev/null || printf 'unknown')"
    report PASS host.os "Host OS is ${host_os}; firewall enforcement is proven in the container runtime."

    if ! command -v docker >/dev/null 2>&1; then
        report FAIL docker.cli \
            "Docker CLI is not installed or not in PATH." \
            "Install Docker Engine and the Compose plugin; see README.md#requirements."
        return
    fi
    report PASS docker.cli "$(docker --version 2>/dev/null || printf 'Docker CLI found')."

    if docker compose version >/dev/null 2>&1; then
        DOCKER_COMPOSE=1
        report PASS docker.compose "$(docker compose version 2>/dev/null | head -n 1)."
    else
        report FAIL docker.compose \
            "The Docker Compose plugin is unavailable." \
            "Install the docker compose plugin; see README.md#requirements."
    fi

    if docker info >/dev/null 2>&1; then
        DOCKER_DAEMON=1
        report PASS docker.daemon "Docker daemon is reachable."
        docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
        if [[ -n "${docker_os}" ]]; then
            report PASS docker.runtime "Docker runtime OS: ${docker_os}; firewall capability is checked inside sandcastle-firewall."
        fi
    else
        current_user="${USER:-$(id -un 2>/dev/null || printf '<user>')}"
        report FAIL docker.daemon \
            "Docker daemon is not reachable by ${current_user}." \
            "Start Docker and grant ${current_user} access to its socket; see README.md#permission-denied-on-varrundockersock."
    fi

    socket_type="$(stat -Lc '%F' "${DOCKER_SOCKET}" 2>/dev/null || true)"
    if [[ "${socket_type}" != "socket" ]]; then
        report FAIL docker.socket \
            "Required Docker socket ${DOCKER_SOCKET} is not a Unix socket." \
            "Configure Docker Engine with ${DOCKER_SOCKET}; Sandcastle bind-mounts this path into team vulnerable machines."
    elif [[ ! -r "${DOCKER_SOCKET}" || ! -w "${DOCKER_SOCKET}" ]]; then
        current_user="${USER:-$(id -un 2>/dev/null || printf '<user>')}"
        report FAIL docker.socket \
            "${current_user} cannot read and write ${DOCKER_SOCKET}." \
            "Grant ${current_user} Docker access, then start a new login session; see README.md#permission-denied-on-varrundockersock."
    else
        report WARN docker.socket \
            "${DOCKER_SOCKET} is accessible and will grant team vulnerable machines host-level Docker control." \
            "Use only trusted local participants. Read docs/THREAT_MODEL.md (Docker Socket section) before sharing this arena."
    fi

    if ((DOCKER_COMPOSE)) && [[ -f "${COMPOSE_FILE}" ]]; then
        if docker compose -f "${COMPOSE_FILE}" config --quiet >/dev/null 2>&1; then
            report PASS compose.syntax "Docker Compose configuration is valid."
        else
            report FAIL compose.syntax \
                "Docker Compose rejected ${COMPOSE_FILE#"${ROOT}"/}." \
                "Run docker compose -f docker-compose.yml config and fix scripts/setup.sh before regenerating."
        fi
    fi
}

load_container_state() {
    local name state
    ((DOCKER_DAEMON)) || return

    while IFS=$'\t' read -r name state; do
        [[ -n "${name}" ]] || continue
        container_state_set "${name}" "${state}"
    done < <(docker ps -a --format '{{.Names}}{{"\t"}}{{.State}}' 2>/dev/null)
}

check_required_ports() {
    local -a conflicts=()
    local -a missing=()
    local -a missing_config=()
    local -a ports=()
    local port owner state expected_port listeners=""

    for id in "${CONFIGURED_IDS[@]}"; do
        owner="team${id}-ssh"
        if ! required_port_owner_has_value "${owner}"; then
            missing_config+=("${owner}")
            continue
        fi
        if ((CONFIG_LOADED)); then
            expected_port=$((ARENA_SSH_BASE_PORT + id))
            if [[ "$(required_port_owner_get "${expected_port}")" != "${owner}" ]]; then
                missing_config+=("${owner}:expected-${expected_port}")
            fi
        fi
    done
    if ((CONFIG_LOADED)); then
        required_port_owner_set "${ARENA_FIREWALL_WS_PORT}" "sandcastle-firewall"
        required_port_owner_set "${ARENA_FIREWALL_PROXY_PORT}" "sandcastle-firewall"
    fi
    ports=()
    while IFS= read -r port; do
        ports+=("${port}")
    done < <(required_port_owner_keys | sort -n)

    if ((${#missing_config[@]} > 0)); then
        report FAIL host.ports \
            "No published SSH port is configured for: $(join_csv "${missing_config[@]}")." \
            "Regenerate with ./scripts/setup.sh --teams ${#CONFIGURED_IDS[@]} and inspect docker-compose.yml."
        return
    fi

    if ! command -v ss >/dev/null 2>&1; then
        report WARN host.ports \
            "Cannot inspect required ports because the ss command is unavailable." \
            "Install iproute2, then rerun ./scripts/doctor.sh."
        return
    fi
    listeners="$(ss -H -ltn 2>/dev/null || true)"

    for port in "${ports[@]}"; do
        owner="$(required_port_owner_get "${port}")"
        state="$(container_state_get "${owner}" absent)"
        if awk -v port="${port}" '$4 ~ (":" port "$") { found=1 } END { exit !found }' <<< "${listeners}"; then
            if [[ "${state}" != "running" ]]; then
                conflicts+=("${port} (${owner} is ${state})")
            fi
        elif [[ "${state}" == "running" ]]; then
            missing+=("${port} (${owner})")
        fi
    done

    if ((${#conflicts[@]} > 0)); then
        report FAIL host.ports \
            "Required port conflicts: $(join_csv "${conflicts[@]}")." \
            "Stop the conflicting listeners or change the generated port allocation, then rerun ./scripts/doctor.sh."
    elif ((${#missing[@]} > 0)); then
        report FAIL host.ports \
            "Running Sandcastle services are not listening on: $(join_csv "${missing[@]}")." \
            "Inspect docker compose ps and docker compose logs, then restart the affected services."
    else
        report PASS host.ports \
            "Required SSH, WebSocket, and proxy ports are available or owned by running Sandcastle services."
    fi
}

check_subnet_conflicts() {
    local name project subnet route destination device
    local -a conflicts=()
    local -a route_conflicts=()

    [[ -n "${DESIRED_SUBNET}" ]] || return
    if ! command -v python3 >/dev/null 2>&1; then
        report WARN network.subnet \
            "Cannot evaluate CIDR overlap because Python 3 is unavailable." \
            "Install Python 3, then rerun ./scripts/doctor.sh."
        return
    fi
    ((DOCKER_DAEMON)) || {
        report WARN network.subnet \
            "Docker subnet conflicts were not checked because the daemon is unavailable." \
            "Restore Docker access, then rerun ./scripts/doctor.sh."
        return
    }

    while IFS=$'\t' read -r name project; do
        [[ -n "${name}" ]] || continue
        while IFS= read -r subnet; do
            [[ -n "${subnet}" ]] || continue
            if cidr_overlaps "${DESIRED_SUBNET}" "${subnet}"; then
                if [[ "${project}" == "sandcastle" && "${subnet}" == "${DESIRED_SUBNET}" ]]; then
                    continue
                fi
                conflicts+=("${name}:${subnet}")
            fi
        done < <(
            docker network inspect \
                --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' \
                "${name}" 2>/dev/null
        )
    done < <(
        docker network ls \
            --format '{{.Name}}{{"\t"}}{{.Label "com.docker.compose.project"}}' \
            2>/dev/null
    )

    if command -v ip >/dev/null 2>&1; then
        while IFS= read -r route; do
            destination="${route%% *}"
            [[ "${destination}" == */* ]] || continue
            device=""
            if [[ "${route}" =~ dev[[:space:]]+([^[:space:]]+) ]]; then
                device="${BASH_REMATCH[1]}"
            fi
            if [[ "${device}" == br-* || "${device}" == docker* ]]; then
                continue
            fi
            if cidr_overlaps "${DESIRED_SUBNET}" "${destination}"; then
                route_conflicts+=("${destination} via ${device:-unknown}")
            fi
        done < <(ip -o -4 route show 2>/dev/null || true)
    fi

    if ((${#conflicts[@]} > 0 || ${#route_conflicts[@]} > 0)); then
        report FAIL network.subnet \
            "CTF subnet ${DESIRED_SUBNET} overlaps existing networks/routes: $(join_csv "${conflicts[@]}" "${route_conflicts[@]}")." \
            "Remove the conflicting Docker network/route or change the CTF subnet in scripts/setup.sh and regenerate."
    else
        report PASS network.subnet "No conflicting Docker network or non-Docker host route overlaps ${DESIRED_SUBNET}."
    fi
}

check_workspaces() {
    local team_dir service_dir required path name id
    local -a incomplete=()
    local -a unmarked=()
    local -a extras=()
    local -a required_paths=(
        "Dockerfile"
        "docker-compose.yml"
        "app/app.py"
        "app/requirements.txt"
    )

    for id in "${CONFIGURED_IDS[@]}"; do
        team_dir="${ROOT}/teams/generated/team${id}"
        service_dir="${team_dir}/example-vuln"
        if [[ ! -d "${service_dir}" ]]; then
            incomplete+=("team${id}:missing-directory")
            continue
        fi
        for required in "${required_paths[@]}"; do
            path="${service_dir}/${required}"
            if [[ ! -f "${path}" || ! -s "${path}" ]]; then
                incomplete+=("team${id}:${required}")
            fi
        done
        if [[ ! -f "${team_dir}/.sandcastle-generated" ]]; then
            unmarked+=("team${id}")
        fi
    done

    if ((${#incomplete[@]} > 0)); then
        report FAIL workspace.completeness \
            "Incomplete generated service workspaces: $(join_csv "${incomplete[@]}")." \
            "For disposable team copies run ./scripts/setup.sh --teams ${#CONFIGURED_IDS[@]} --overwrite-services; this deletes generated patches."
    else
        report PASS workspace.completeness \
            "All configured team workspaces contain the required service files."
    fi

    if ((${#unmarked[@]} > 0)); then
        report WARN workspace.markers \
            "Configured directories are not marked as generated: $(join_csv "${unmarked[@]}")." \
            "Inspect them before cleanup; regenerate disposable copies with --overwrite-services."
    else
        report PASS workspace.markers "All configured team directories have generation markers."
    fi

    shopt -s nullglob
    for team_dir in "${ROOT}"/teams/generated/team*; do
        [[ -d "${team_dir}" ]] || continue
        name="$(basename "${team_dir}")"
        [[ "${name}" =~ ^team([0-9]+)$ ]] || continue
        id="${BASH_REMATCH[1]}"
        if ! array_contains "${id}" "${CONFIGURED_IDS[@]}"; then
            extras+=("${name}")
        fi
    done
    shopt -u nullglob

    if ((${#extras[@]} > 0)); then
        report WARN workspace.extras \
            "Generated workspace directories exist outside the configured topology: $(join_csv "${extras[@]}")." \
            "Inspect them, then rerun ./scripts/setup.sh --teams ${#CONFIGURED_IDS[@]} to prune marked extras."
    else
        report PASS workspace.extras "No extra generated team workspace directories were found."
    fi
}

check_runtime_topology() {
    local name state id expected
    local -a running_orphans=()
    local -a stopped_orphans=()
    local -a missing=()

    ((DOCKER_DAEMON)) || return

    while IFS= read -r name; do
        if [[ "${name}" =~ ^team([0-9]+)-(ssh|vuln|vuln-app|dind)$ ]]; then
            id="${BASH_REMATCH[1]}"
            if ! array_contains "${id}" "${CONFIGURED_IDS[@]}"; then
                if [[ "$(container_state_get "${name}")" == "running" ]]; then
                    running_orphans+=("${name}")
                else
                    stopped_orphans+=("${name}")
                fi
            fi
        fi
    done < <(container_state_keys)

    if ((${#running_orphans[@]} > 0)); then
        report FAIL runtime.orphans \
            "Running team containers are outside the configured topology: $(join_csv "${running_orphans[@]}")." \
            "For a disposable arena run ./scripts/cleanup.sh --keep-images, regenerate, and restart."
    elif ((${#stopped_orphans[@]} > 0)); then
        report WARN runtime.orphans \
            "Stopped team containers are outside the configured topology: $(join_csv "${stopped_orphans[@]}")." \
            "Inspect them, then remove stale containers or run ./scripts/cleanup.sh --keep-images."
    else
        report PASS runtime.orphans "No orphan team containers were found."
    fi

    for id in "${CONFIGURED_IDS[@]}"; do
        for expected in "team${id}-ssh" "team${id}-vuln"; do
            if [[ "$(container_state_get "${expected}" absent)" == "running" ]]; then
                RUNTIME_ACTIVE=1
            fi
        done
        if [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" &&
              "$(container_state_get "team${id}-dind" absent)" == "running" ]]; then
            RUNTIME_ACTIVE=1
        fi
        if [[ "$(container_state_get "team${id}-vuln-app" absent)" == "running" ]]; then
            RUNTIME_ACTIVE=1
        fi
    done
    if [[ "$(container_state_get sandcastle-firewall absent)" == "running" ]]; then
        RUNTIME_ACTIVE=1
    fi

    if ((RUNTIME_ACTIVE == 0)); then
        report PASS runtime.topology "Arena infrastructure is stopped; runtime readiness checks are in pre-start mode."
        return
    fi

    for id in "${CONFIGURED_IDS[@]}"; do
        for expected in "team${id}-ssh" "team${id}-vuln"; do
            state="$(container_state_get "${expected}" absent)"
            if [[ "${state}" != "running" ]]; then
                missing+=("${expected}:${state}")
            fi
        done
        if [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" ]]; then
            state="$(container_state_get "team${id}-dind" absent)"
            if [[ "${state}" != "running" ]]; then
                missing+=("team${id}-dind:${state}")
            fi
        fi
    done
    state="$(container_state_get sandcastle-firewall absent)"
    if [[ "${state}" != "running" ]]; then
        missing+=("sandcastle-firewall:${state}")
    fi

    if ((${#missing[@]} > 0)); then
        report FAIL runtime.topology \
            "Arena infrastructure is partially running: $(join_csv "${missing[@]}")." \
            "Inspect docker compose ps and logs, then run ./scripts/arena.sh restart."
    else
        report PASS runtime.topology "All configured gateways, vulnerable machines, and the firewall are running."
    fi
}

check_runtime_docker_access() {
    local id name mount host_mount
    local -a broken=()

    ((DOCKER_DAEMON && RUNTIME_ACTIVE)) || return

    for id in "${CONFIGURED_IDS[@]}"; do
        name="team${id}-vuln"
        [[ "$(container_state_get "${name}" absent)" == "running" ]] || continue
        if [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" ]]; then
            host_mount="$(
                docker inspect \
                    --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Type}}|{{.RW}}{{end}}{{end}}' \
                    "${name}" 2>/dev/null || true
            )"
            if [[ -n "${host_mount}" ]]; then
                broken+=("${name}:host-socket-mounted")
                continue
            fi
            if ! docker exec "${name}" sh -lc \
                'case "${DOCKER_HOST:-}" in unix://*) test -S "${DOCKER_HOST#unix://}" ;; *) exit 1 ;; esac && docker info >/dev/null' \
                >/dev/null 2>&1; then
                broken+=("${name}:dind-access")
            fi
            continue
        fi
        mount="$(
            docker inspect \
                --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Type}}|{{.RW}}{{end}}{{end}}' \
                "${name}" 2>/dev/null || true
        )"
        if [[ "${mount}" != "bind|true" ]]; then
            broken+=("${name}:socket-mount")
            continue
        fi
        if ! docker exec "${name}" sh -lc \
            'command -v docker >/dev/null && test -S /var/run/docker.sock' \
            >/dev/null 2>&1; then
            broken+=("${name}:docker-access")
        fi
    done

    if ((${#broken[@]} > 0)); then
        report FAIL runtime.docker-access \
            "Vulnerable machines lack required Docker access: $(join_csv "${broken[@]}")." \
            "Inspect docker/vuln/Dockerfile and generated socket mounts, then run ./scripts/arena.sh restart."
    elif [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" ]]; then
        report PASS runtime.docker-access \
            "Running vulnerable machines use team-local Docker-in-Docker daemons without the host socket."
    elif [[ "${ARENA_ISOLATION_MODE:-trusted}" == "isolated" ]]; then
        report PASS runtime.docker-access \
            "Running vulnerable machines use per-team filtered Docker sockets."
    else
        report WARN runtime.docker-access \
            "Running vulnerable machines have host Docker control as required by trusted-local mode." \
            "Do not use this mode with untrusted participants. Read docs/THREAT_MODEL.md for escape paths and required controls."
    fi
}

check_app_health() {
    local id app machine state
    local -a stopped=()
    local -a unhealthy=()

    ((DOCKER_DAEMON)) || return
    if ((CONFIG_LOADED == 0)); then
        report WARN runtime.apps \
            "App health checks were skipped because the canonical arena configuration is invalid." \
            "Fix config/arena.env, then rerun ./scripts/doctor.sh."
        return
    fi
    if ((RUNTIME_ACTIVE == 0)); then
        report PASS runtime.apps "Arena is stopped; app health checks were skipped."
        return
    fi

    for id in "${CONFIGURED_IDS[@]}"; do
        machine="team${id}-vuln"
        app="team${id}-vuln-app"
        if [[ "${ARENA_ISOLATION_MODE:-trusted}" == "dind" ]]; then
            state="$(
                docker exec "${machine}" \
                    docker inspect --format '{{.State.Status}}' "${app}" 2>/dev/null ||
                    printf 'absent'
            )"
            state="${state:-absent}"
        else
            state="$(container_state_get "${app}" absent)"
        fi
        if [[ "${state}" != "running" ]]; then
            stopped+=("${app}:${state}")
            continue
        fi
        if ! docker exec "${machine}" \
            curl -fsS --max-time 3 "http://${machine}:${ARENA_SERVICE_PORT}/health" \
            >/dev/null 2>&1; then
            unhealthy+=("${app}")
        fi
    done

    if ((${#stopped[@]} > 0)); then
        report FAIL runtime.apps \
            "Required vulnerable apps are not running: $(join_csv "${stopped[@]}")." \
            "Run ./scripts/arena.sh up to recreate all configured vulnerable apps."
    elif ((${#unhealthy[@]} > 0)); then
        report FAIL runtime.apps \
            "Vulnerable apps failed /health: $(join_csv "${unhealthy[@]}")." \
            "Inspect docker logs team<N>-vuln-app, then run ./scripts/arena.sh restart."
    else
        report PASS runtime.apps "All configured vulnerable apps passed /health."
    fi
}

check_firewall() {
    local bridge_value rules listing rule_line packets

    ((DOCKER_DAEMON)) || return
    if [[ "$(container_state_get sandcastle-firewall absent)" != "running" ]]; then
        if ((RUNTIME_ACTIVE)); then
            report FAIL firewall.runtime \
                "The firewall is not running while other arena infrastructure is active." \
                "Inspect docker compose logs firewall, then run ./scripts/arena.sh restart."
        else
            report PASS firewall.runtime "Arena is stopped; firewall runtime checks were skipped."
        fi
        return
    fi
    report PASS firewall.runtime "Firewall container is running."

    bridge_value="$(
        docker exec sandcastle-firewall \
            sh -ec 'cat /proc/sys/net/bridge/bridge-nf-call-iptables' \
            2>/dev/null || true
    )"
    if [[ "${bridge_value}" == "1" ]]; then
        report PASS firewall.bridge-netfilter "Bridge netfilter is visible and enabled inside sandcastle-firewall."
    else
        report FAIL firewall.bridge-netfilter \
            "sandcastle-firewall sees net.bridge.bridge-nf-call-iptables as '${bridge_value:-unavailable}', so bridge traffic may bypass the redirect." \
            "Inspect docker compose logs firewall and the Docker runtime networking environment, then rerun ./scripts/arena.sh restart."
    fi

    rules="$(
        docker exec sandcastle-firewall \
            iptables -t nat -S PREROUTING 2>/dev/null || true
    )"
    if [[ "${rules}" != *"${RULE_COMMENT}"* ]]; then
        report FAIL firewall.rule \
            "The Sandcastle PREROUTING redirect rule is missing." \
            "Inspect docker compose logs firewall and restart the firewall service."
        return
    fi
    report PASS firewall.rule "The Sandcastle PREROUTING redirect rule exists."

    listing="$(
        docker exec sandcastle-firewall \
            iptables -t nat -L PREROUTING -n -v -x --line-numbers \
            2>/dev/null || true
    )"
    rule_line="$(grep -F "${RULE_COMMENT}" <<< "${listing}" | head -n 1 || true)"
    packets="$(awk '{print $2}' <<< "${rule_line}")"
    if [[ ! "${packets}" =~ ^[0-9]+$ ]]; then
        report FAIL firewall.traffic \
            "Could not read the redirect rule packet counter." \
            "Run docker exec sandcastle-firewall iptables -t nat -L PREROUTING -n -v -x --line-numbers and inspect the rule."
    elif ((packets == 0)); then
        report FAIL firewall.traffic \
            "The redirect rule has seen zero packets, so required enforcement has not been proven." \
            "Run ./scripts/smoke-network.sh and inspect firewall logs; arena startup must fail until this passes."
    else
        report PASS firewall.traffic "The redirect rule has processed ${packets} packet(s)."
    fi
}

check_bot_api() {
    local -a missing=()
    local health_rc=1
    local listeners=""

    if ((CONFIG_LOADED == 0)); then
        report WARN bot.api \
            "Bot API checks were skipped because the canonical arena configuration is invalid." \
            "Fix config/arena.env, then rerun ./scripts/doctor.sh."
        return
    fi

    command -v python3 >/dev/null 2>&1 || missing+=("python3")
    [[ -f "${ROOT}/bot/bot_api.py" ]] || missing+=("bot/bot_api.py")
    [[ -x "${ROOT}/bot/deploy.sh" ]] || missing+=("executable bot/deploy.sh")

    if ((${#missing[@]} > 0)); then
        report FAIL bot.prerequisites \
            "Bot API prerequisites are missing: $(join_csv "${missing[@]}")." \
            "Install Python 3 and restore the bot files from the repository."
        return
    fi

    if ! (
        cd "${ROOT}" &&
            PYTHONDONTWRITEBYTECODE=1 python3 -B -c \
                'import sys; sys.path.insert(0, "bot"); import bot_api' \
                >/dev/null 2>&1
    ); then
        report FAIL bot.prerequisites \
            "Python cannot import bot/bot_api.py and its local modules." \
            "Inspect bot/bot_api.py, then rebuild the bot-controller service."
        return
    fi
    report PASS bot.prerequisites "Bot API source, Python imports, and deploy script are available."

    if python3 -B -c '
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
        body = __import__("json").load(response)
        raise SystemExit(0 if response.status == 200 and body.get("ok") is True else 1)
except Exception:
    raise SystemExit(1)
' "http://${ARENA_BOT_API_HOST}:${ARENA_BOT_API_PORT}/health" >/dev/null 2>&1; then
        health_rc=0
    fi

    if ((health_rc == 0)); then
        report PASS bot.api \
            "Bot controller is healthy at http://${ARENA_BOT_API_HOST}:${ARENA_BOT_API_PORT}."
        return
    fi

    if command -v ss >/dev/null 2>&1; then
        listeners="$(ss -H -ltn 2>/dev/null || true)"
    fi
    if awk -v port="${ARENA_BOT_API_PORT}" \
        '$4 ~ (":" port "$") { found=1 } END { exit !found }' <<< "${listeners}"; then
        report WARN bot.api \
            "Port ${ARENA_BOT_API_PORT} is occupied, but the Sandcastle bot controller health check failed." \
            "Stop the conflicting process or inspect docker compose logs bot-controller."
    else
        report WARN bot.api \
            "Bot controller is not running." \
            "Run ./scripts/arena.sh up; the controller starts with the arena."
    fi
}

main() {
    if [[ "${FORMAT}" == "text" ]]; then
        echo "Sandcastle doctor (read-only)"
        echo "Root: ${ROOT}"
        echo
    fi

    check_arena_configuration
    load_compose_metadata
    check_host_and_docker
    load_container_state
    check_required_ports
    check_subnet_conflicts
    check_workspaces
    check_runtime_topology
    check_runtime_docker_access
    check_app_health
    check_firewall
    check_bot_api

    if [[ "${FORMAT}" == "text" ]]; then
        echo
        printf 'Summary: %d PASS, %d WARN, %d FAIL\n' \
            "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"
    else
        printf 'SUMMARY\tdoctor.summary\t%d PASS, %d WARN, %d FAIL\t\n' \
            "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"
    fi

    if ((FAIL_COUNT > 0)); then
        return 1
    fi
    return 0
}

main
