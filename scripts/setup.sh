#!/usr/bin/env bash
# Generate a local Sandcastle Attack & Defense scaffold from config/arena.env.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TEAMS_DIR="${ROOT}/teams"
GENERATED_TEAMS_DIR="${TEAMS_DIR}/generated"
COMPOSE_FILE="${ROOT}/docker-compose.yml"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

REQUESTED_TEAM_COUNT=""
TEMPLATE_OVERRIDE=""
OVERWRITE_SERVICES=0
PRUNE_EXTRA_TEAMS=1
REMOVE_ORPHAN_CONTAINERS=0
ALLOW_ORPHAN_CONTAINERS=0
SHOW_ACCESS=0
REQUESTED_ISOLATION_MODE=""

declare -a REQUIRED_SERVICE_PATHS=(
    "Dockerfile"
    "checker.py"
    "app/app.py"
    "app/requirements.txt"
)

usage() {
    cat <<'EOF'
Usage:
  ./scripts/setup.sh
  ./scripts/setup.sh --teams N

Generate docker-compose.yml and per-team service workspaces from
config/arena.env. Passing --teams updates ARENA_TEAM_COUNT in that file.

Options:
  --teams, -t N
                Persist N as ARENA_TEAM_COUNT, then generate N teams.
  --template DIR
                Use DIR for this generation instead of ARENA_SERVICE_TEMPLATE.
  --overwrite-services
                Destructively replace every configured generated service copy.
  --no-prune
                Keep marked generated team directories above the team count.
  --remove-orphan-containers
                Remove stale team containers outside the requested topology.
  --allow-orphan-containers
                Generate despite stale team containers, with an explicit warning.
  --show-access
                Print development SSH credentials and connection commands.
  --dind
                Persist ARENA_ISOLATION_MODE=dind, then generate a production
                topology with one Docker-in-Docker daemon per team.
  --help, -h
                Show this help text.

Compatibility:
  ./scripts/setup.sh N  is equivalent to --teams N.
EOF
}

die() {
    echo "setup.sh: $*" >&2
    exit 1
}

warn() {
    echo "[!] $*" >&2
}

parse_args() {
    local provided=0

    while (($#)); do
        case "$1" in
            --teams|-t)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                REQUESTED_TEAM_COUNT="$2"
                provided=$((provided + 1))
                shift 2
                ;;
            --teams=*)
                REQUESTED_TEAM_COUNT="${1#*=}"
                provided=$((provided + 1))
                shift
                ;;
            --template)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                TEMPLATE_OVERRIDE="$2"
                shift 2
                ;;
            --template=*)
                TEMPLATE_OVERRIDE="${1#*=}"
                shift
                ;;
            --overwrite-services)
                OVERWRITE_SERVICES=1
                shift
                ;;
            --no-prune)
                PRUNE_EXTRA_TEAMS=0
                shift
                ;;
            --remove-orphan-containers)
                REMOVE_ORPHAN_CONTAINERS=1
                shift
                ;;
            --allow-orphan-containers)
                ALLOW_ORPHAN_CONTAINERS=1
                shift
                ;;
            --show-access)
                SHOW_ACCESS=1
                shift
                ;;
            --dind)
                [[ -z "${REQUESTED_ISOLATION_MODE}" ]] ||
                    die "isolation mode provided more than once"
                REQUESTED_ISOLATION_MODE="dind"
                ARENA_ISOLATION_MODE="dind"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            [0-9]*)
                REQUESTED_TEAM_COUNT="$1"
                provided=$((provided + 1))
                shift
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done

    ((provided <= 1)) || die "team count provided more than once"
    if ((REMOVE_ORPHAN_CONTAINERS && ALLOW_ORPHAN_CONTAINERS)); then
        die "--remove-orphan-containers and --allow-orphan-containers are mutually exclusive"
    fi
}

validate_requested_team_count() {
    [[ -n "${REQUESTED_TEAM_COUNT}" ]] || return 0
    ARENA_TEAM_COUNT="${REQUESTED_TEAM_COUNT}"
    arena_config_require_int ARENA_TEAM_COUNT 1 250 ||
        die "invalid --teams value"
    arena_config_validate_port_layout || die "requested team count conflicts with host ports"
}

resolve_template() {
    if [[ -n "${TEMPLATE_OVERRIDE}" ]]; then
        if [[ "${TEMPLATE_OVERRIDE}" == /* ]]; then
            ARENA_SERVICE_TEMPLATE_PATH="${TEMPLATE_OVERRIDE}"
        else
            ARENA_SERVICE_TEMPLATE_PATH="${ROOT}/${TEMPLATE_OVERRIDE}"
        fi
    fi

    [[ -d "${ARENA_SERVICE_TEMPLATE_PATH}" ]] ||
        die "missing vulnerable service template: ${ARENA_SERVICE_TEMPLATE_PATH}"

    local required
    for required in "${REQUIRED_SERVICE_PATHS[@]}"; do
        if [[ ! -s "${ARENA_SERVICE_TEMPLATE_PATH}/${required}" ]]; then
            die "service template is incomplete: missing ${required}"
        fi
    done
}

is_generated_team_dir() {
    local team_dir="$1"
    [[ -f "${team_dir}/.sandcastle-generated" ]]
}

service_workspace_complete() {
    local service_dir="$1"
    local required

    [[ -d "${service_dir}" ]] || return 1
    for required in "${REQUIRED_SERVICE_PATHS[@]}"; do
        [[ -s "${service_dir}/${required}" ]] || return 1
    done
}

copy_missing_service_files() {
    local source_dir="$1"
    local target_dir="$2"
    local required source_path target_path

    for required in "${REQUIRED_SERVICE_PATHS[@]}"; do
        source_path="${source_dir}/${required}"
        target_path="${target_dir}/${required}"
        if [[ ! -s "${target_path}" ]]; then
            mkdir -p "$(dirname "${target_path}")"
            cp -a "${source_path}" "${target_path}"
        fi
    done
}

preflight_team_workspaces() {
    local teams="$1"
    local i team_dir service_dir

    for ((i = 1; i <= teams; i++)); do
        team_dir="${GENERATED_TEAMS_DIR}/team${i}"
        service_dir="${team_dir}/example-vuln"

        if [[ ! -e "${team_dir}" ]]; then
            continue
        fi
        if is_generated_team_dir "${team_dir}"; then
            continue
        fi
        if ((OVERWRITE_SERVICES)); then
            continue
        fi

        if service_workspace_complete "${service_dir}"; then
            die "team${i} workspace is participant-owned (unmarked); refusing to rewrite its generated Compose file. Move it outside teams/generated or pass --overwrite-services."
        fi
        die "team${i} workspace is incomplete and participant-owned (unmarked). Inspect it, then move it aside or pass --overwrite-services."
    done
}

find_orphan_containers() {
    local teams="$1"
    local name id

    command -v docker >/dev/null 2>&1 || return
    docker info >/dev/null 2>&1 || return

    while IFS= read -r name; do
        if [[ "${name}" =~ ^team([0-9]+)-(ssh|vuln|vuln-app)$ ]]; then
            id="$((10#${BASH_REMATCH[1]}))"
            if ((id > teams)); then
                printf '%s\n' "${name}"
            fi
        fi
    done < <(docker ps -a --format '{{.Names}}' 2>/dev/null)
}

handle_orphan_containers() {
    local teams="$1"
    local -a orphans=()

    while IFS= read -r orphan; do
        orphans+=("${orphan}")
    done < <(find_orphan_containers "${teams}" | sort -V)
    ((${#orphans[@]} > 0)) || return 0

    if ((REMOVE_ORPHAN_CONTAINERS)); then
        warn "DESTRUCTIVE: removing stale containers outside the ${teams}-team topology: ${orphans[*]}"
        docker rm -f "${orphans[@]}"
        return
    fi
    if ((ALLOW_ORPHAN_CONTAINERS)); then
        warn "Continuing with explicitly allowed orphan containers: ${orphans[*]}"
        return
    fi

    die "stale team containers exist outside the ${teams}-team topology: ${orphans[*]}. Re-run with --remove-orphan-containers to delete them or --allow-orphan-containers to keep them explicitly."
}

remove_legacy_generated_teams() {
    local team_dir

    shopt -s nullglob
    for team_dir in "${TEAMS_DIR}"/team*; do
        [[ -d "${team_dir}" ]] || continue
        if is_generated_team_dir "${team_dir}"; then
            rm -rf "${team_dir}"
        else
            warn "Keeping participant-owned legacy directory: ${team_dir}"
        fi
    done
    shopt -u nullglob
}

prune_extra_teams() {
    local teams="$1"
    local team_dir name team_num

    ((PRUNE_EXTRA_TEAMS)) || return

    shopt -s nullglob
    for team_dir in "${GENERATED_TEAMS_DIR}"/team*; do
        [[ -d "${team_dir}" ]] || continue
        name="$(basename "${team_dir}")"
        [[ "${name}" =~ ^team([0-9]+)$ ]] || continue
        team_num="$((10#${BASH_REMATCH[1]}))"

        if ((team_num > teams)); then
            if is_generated_team_dir "${team_dir}"; then
                rm -rf "${team_dir}"
            else
                warn "Keeping participant-owned extra directory: ${team_dir}"
            fi
        fi
    done
    shopt -u nullglob
}

prepare_service_workspace() {
    local team_num="$1"
    local team_dir="$2"
    local service_dir="${team_dir}/example-vuln"

    if ((OVERWRITE_SERVICES)); then
        warn "DESTRUCTIVE: replacing team${team_num} service workspace from ${ARENA_SERVICE_TEMPLATE_PATH}"
        rm -rf "${service_dir}"
        mkdir -p "${service_dir}"
        cp -a "${ARENA_SERVICE_TEMPLATE_PATH}/." "${service_dir}/"
    elif [[ ! -d "${service_dir}" ]]; then
        mkdir -p "${service_dir}"
        cp -a "${ARENA_SERVICE_TEMPLATE_PATH}/." "${service_dir}/"
    elif ! service_workspace_complete "${service_dir}"; then
        warn "Repairing missing files in marked generated workspace team${team_num}; existing files are preserved"
        copy_missing_service_files "${ARENA_SERVICE_TEMPLATE_PATH}" "${service_dir}"
    fi

    service_workspace_complete "${service_dir}" ||
        die "team${team_num} workspace remains incomplete after generation"

    printf 'Generated by ./scripts/setup.sh for team%s.\n' "${team_num}" \
        > "${team_dir}/.sandcastle-generated"
}

write_team_service_compose() {
    local team_num="$1"
    local team_dir="$2"
    local service_dir="${team_dir}/example-vuln"
    local service_name checker_values checker_username checker_password plant_token
    local dns_server
    local -a dind_dns_servers

    service_name="$(basename "${ARENA_SERVICE_TEMPLATE_PATH%/}")"
    checker_values="$(
        python3 "${ROOT}/gameserver/checker_credentials.py" \
            --secret "${ARENA_CHECKER_SECRET}" \
            --team "${team_num}" \
            --service "${service_name}"
    )" || die "failed to derive checker credentials for team${team_num}/${service_name}"
    IFS=$'\t' read -r checker_username checker_password plant_token <<< "${checker_values}"

    if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
        cat > "${service_dir}/docker-compose.yml" <<EOF
# Generated by scripts/setup.sh for Team ${team_num}.
# Source: config/arena.env. Re-run setup instead of editing this file.
# Organizer lifecycle: ./scripts/arena.sh up
# Teams may still rebuild their own patched app from inside team${team_num}-vuln.

name: sandcastle-team${team_num}

services:
  team${team_num}-vuln-app:
    build:
      context: .
      network: host
    image: sandcastle/team${team_num}-vuln-app:latest
    container_name: team${team_num}-vuln-app
    network_mode: host
EOF
        IFS=',' read -r -a dind_dns_servers <<< "${ARENA_DIND_DNS_SERVERS}"
        if ((${#dind_dns_servers[@]} > 0)); then
            printf '    dns:\n' >> "${service_dir}/docker-compose.yml"
            for dns_server in "${dind_dns_servers[@]}"; do
                [[ -n "${dns_server}" ]] || continue
                printf '      - %s\n' "${dns_server}" >> "${service_dir}/docker-compose.yml"
            done
        fi
        cat >> "${service_dir}/docker-compose.yml" <<EOF
    environment:
      TEAM_ID: "${team_num}"
      TEAM_NAME: "Team ${team_num}"
      SERVICE_PORT: "${ARENA_SERVICE_PORT}"
      SECRET_KEY: "sandcastle-team${team_num}-dev-secret"
      CHECKER_USERNAME: "${checker_username}"
      CHECKER_PASSWORD: "${checker_password}"
      PLANT_TOKEN: "${plant_token}"
    volumes:
      - team-data:/app/data
    labels:
      sandcastle.role: "vuln-app"
      sandcastle.team: "team${team_num}"
    restart: unless-stopped

volumes:
  team-data:
    name: sandcastle_team${team_num}-data
    labels:
      sandcastle.role: "vuln-data"
      sandcastle.team: "team${team_num}"
EOF
        return
    fi

    cat > "${service_dir}/docker-compose.yml" <<EOF
# Generated by scripts/setup.sh for Team ${team_num}.
# Source: config/arena.env. Re-run setup instead of editing this file.
# Organizer lifecycle: ./scripts/arena.sh up
# Teams may still rebuild their own patched app from inside team${team_num}-vuln.

name: sandcastle-team${team_num}

services:
  team${team_num}-vuln-app:
    build:
      context: .
    image: sandcastle/team${team_num}-vuln-app:latest
    container_name: team${team_num}-vuln-app
    network_mode: "container:team${team_num}-vuln"
    environment:
      TEAM_ID: "${team_num}"
      TEAM_NAME: "Team ${team_num}"
      SERVICE_PORT: "${ARENA_SERVICE_PORT}"
      SECRET_KEY: "sandcastle-team${team_num}-dev-secret"
      CHECKER_USERNAME: "${checker_username}"
      CHECKER_PASSWORD: "${checker_password}"
      PLANT_TOKEN: "${plant_token}"
    volumes:
      - team-data:/app/data
    labels:
      sandcastle.role: "vuln-app"
      sandcastle.team: "team${team_num}"
    deploy:
      resources:
        limits:
          memory: ${ARENA_TEAM_APP_MEM_LIMIT}
          cpus: '${ARENA_TEAM_APP_CPU_LIMIT}'
          pids: ${ARENA_TEAM_APP_PIDS_LIMIT}
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: "on-failure:${ARENA_TEAM_MAX_RESTARTS}"

volumes:
  team-data:
    name: sandcastle_team${team_num}-data
    labels:
      sandcastle.role: "vuln-data"
      sandcastle.team: "team${team_num}"
EOF
}

write_compose() {
    local teams="$1"
    local i username password
    local vuln_depends vuln_docker_mount vuln_environment vuln_networks
    local dns_server
    local -a dind_dns_servers

    cat > "${COMPOSE_FILE}" <<EOF
# Auto-generated by ./scripts/setup.sh from config/arena.env.
# Re-run that script instead of editing this file.

name: sandcastle

x-sandcastle-arena:
  team_count: ${teams}
  service_port: ${ARENA_SERVICE_PORT}
  ssh_base_port: ${ARENA_SSH_BASE_PORT}
  ssh_bind_host: ${ARENA_SSH_BIND_HOST}
  startup_timeout_seconds: ${ARENA_STARTUP_TIMEOUT_SECONDS}
  round_duration_seconds: ${ARENA_ROUND_DURATION_SECONDS}
  flag_expiry_rounds: ${ARENA_FLAG_EXPIRY_ROUNDS}
  checker_max_concurrency: ${ARENA_CHECKER_MAX_CONCURRENCY}
  gameserver_port: ${ARENA_GAMESERVER_PORT}
  visualizer_port: ${ARENA_VISUALIZER_PORT}
  submission_rate_limit: ${ARENA_SUBMISSION_RATE_LIMIT}
  submission_rate_window_seconds: ${ARENA_SUBMISSION_RATE_WINDOW_SECONDS}
  score_attack_points: ${ARENA_SCORE_ATTACK_POINTS}
  score_defense_points: ${ARENA_SCORE_DEFENSE_POINTS}
  score_sla_points: ${ARENA_SCORE_SLA_POINTS}
  team_vuln_mem_limit: ${ARENA_TEAM_VULN_MEM_LIMIT}
  team_vuln_cpu_limit: ${ARENA_TEAM_VULN_CPU_LIMIT}
  team_vuln_pids_limit: ${ARENA_TEAM_VULN_PIDS_LIMIT}
  team_ssh_mem_limit: ${ARENA_TEAM_SSH_MEM_LIMIT}
  team_ssh_cpu_limit: ${ARENA_TEAM_SSH_CPU_LIMIT}
  team_ssh_pids_limit: ${ARENA_TEAM_SSH_PIDS_LIMIT}
  team_app_mem_limit: ${ARENA_TEAM_APP_MEM_LIMIT}
  team_app_cpu_limit: ${ARENA_TEAM_APP_CPU_LIMIT}
  team_app_pids_limit: ${ARENA_TEAM_APP_PIDS_LIMIT}
  team_max_restarts: ${ARENA_TEAM_MAX_RESTARTS}
  visualizer_mem_limit: ${ARENA_VISUALIZER_MEM_LIMIT}
  visualizer_cpu_limit: ${ARENA_VISUALIZER_CPU_LIMIT}
  log_max_size: ${ARENA_LOG_MAX_SIZE}
  log_max_files: ${ARENA_LOG_MAX_FILES}
  agent_provider: ${ARENA_AGENT_PROVIDER}
  agent_model: ${ARENA_AGENT_MODEL:-disabled}
  agent_max_calls_per_round: ${ARENA_AGENT_MAX_CALLS_PER_ROUND}
  agent_max_calls_per_match: ${ARENA_AGENT_MAX_CALLS_PER_MATCH}
  agent_max_cost_usd_per_match: ${ARENA_AGENT_MAX_COST_USD_PER_MATCH}

networks:
  ctf-network:
    driver: bridge
    ipam:
      config:
        - subnet: ${ARENA_CTF_SUBNET}
          gateway: ${ARENA_CTF_GATEWAY}
  control-plane:
    driver: bridge
EOF

    if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
        for ((i = 1; i <= teams; i++)); do
            cat >> "${COMPOSE_FILE}" <<EOF
  team${i}-dind-network:
    driver: bridge
EOF
        done
    fi

    cat >> "${COMPOSE_FILE}" <<EOF

services:
EOF

    for ((i = 1; i <= teams; i++)); do
        username="$(arena_config_render_team_value "${ARENA_TEAM_USERNAME_PATTERN}" "${i}")"
        password="$(arena_config_render_team_value "${ARENA_TEAM_PASSWORD_PATTERN}" "${i}")"

        if ((i > 1)); then
            printf '\n' >> "${COMPOSE_FILE}"
        fi

        if [[ "${ARENA_ISOLATION_MODE}" == "isolated" ]]; then
            # In isolated mode the proxy holds the host socket and exposes a
            # filtered team-scoped socket. teamN-vuln mounts that filtered
            # socket instead, so it can only control its own containers.
            cat >> "${COMPOSE_FILE}" <<EOF
  team${i}-docker-proxy:
    build:
      context: .
      dockerfile: docker/docker-proxy/Dockerfile
    image: sandcastle/team${i}-docker-proxy:latest
    container_name: team${i}-docker-proxy
    hostname: team${i}-docker-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /run/sandcastle:/run/sandcastle
    environment:
      TEAM_ID: "${i}"
      PROXY_SOCKET: "/run/sandcastle/team${i}.sock"
    labels:
      sandcastle.role: "docker-proxy"
      sandcastle.team: "team${i}"
    deploy:
      resources:
        limits:
          memory: 64m
          cpus: '0.10'
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: unless-stopped

EOF
        fi

        if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
            cat >> "${COMPOSE_FILE}" <<EOF
  team${i}-dind:
    image: docker:27-dind
    container_name: team${i}-dind
    hostname: team${i}-dind
    privileged: true
    command:
      - dockerd
      - --host=unix:///var/run/docker.sock
EOF
            IFS=',' read -r -a dind_dns_servers <<< "${ARENA_DIND_DNS_SERVERS}"
            for dns_server in "${dind_dns_servers[@]}"; do
                [[ -n "${dns_server}" ]] || continue
                printf '      - --dns=%s\n' "${dns_server}" >> "${COMPOSE_FILE}"
            done
            cat >> "${COMPOSE_FILE}" <<EOF
    environment:
      DOCKER_TLS_CERTDIR: ""
    networks:
      - team${i}-dind-network
    volumes:
      - team${i}-dind-data:/var/lib/docker
      - team${i}-dind-run:/var/run
    labels:
      sandcastle.role: "dind-daemon"
      sandcastle.team: "team${i}"
    restart: unless-stopped

EOF
        fi

        if [[ "${ARENA_ISOLATION_MODE}" == "isolated" ]]; then
            vuln_depends="
    depends_on:
      - team${i}-docker-proxy"
            vuln_docker_mount="      - /run/sandcastle/team${i}.sock:/var/run/docker.sock"
            vuln_environment=""
            vuln_networks="      ctf-network:
        ipv4_address: ${ARENA_NETWORK_PREFIX}.${i}.3"
        elif [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
            vuln_depends="
    depends_on:
      - team${i}-dind"
            vuln_docker_mount="      - team${i}-dind-run:/var/run/dind"
            vuln_environment="
    environment:
      DOCKER_HOST: \"unix:///var/run/dind/docker.sock\"
      ARENA_ISOLATION_MODE: \"dind\"
      ARENA_SERVICE_PORT: \"${ARENA_SERVICE_PORT}\"
      SANDCASTLE_DIND_TARGET: \"team${i}-dind\""
            vuln_networks="      ctf-network:
        ipv4_address: ${ARENA_NETWORK_PREFIX}.${i}.3
      team${i}-dind-network: {}"
        else
            vuln_depends=""
            vuln_docker_mount="      - /var/run/docker.sock:/var/run/docker.sock"
            vuln_environment=""
            vuln_networks="      ctf-network:
        ipv4_address: ${ARENA_NETWORK_PREFIX}.${i}.3"
        fi

        cat >> "${COMPOSE_FILE}" <<EOF
  team${i}-vuln:
    build:
      context: .
      dockerfile: docker/vuln/Dockerfile
      args:
        TEAM_ID: "${i}"
        TEAM_NAME: "team${i}"
        TEAM_USER: "${username}"
        TEAM_PASS: "${password}"
        TEAM_UID: "1000"
    image: sandcastle/team${i}-vuln:latest
    container_name: team${i}-vuln
    hostname: team${i}-vuln${vuln_depends}${vuln_environment}
    networks:
${vuln_networks}
    volumes:
${vuln_docker_mount}
      - ./config/arena.env:/tmp/arena.env:ro
      - ./teams/generated/team${i}/example-vuln:/home/${username}/example-vuln
      - ./services/example-vuln:/srv/example-vuln:ro
    cap_add:
      - NET_ADMIN
    labels:
      sandcastle.role: "vuln-machine"
      sandcastle.team: "team${i}"
    deploy:
      resources:
        limits:
          memory: ${ARENA_TEAM_VULN_MEM_LIMIT}
          cpus: '${ARENA_TEAM_VULN_CPU_LIMIT}'
          pids: ${ARENA_TEAM_VULN_PIDS_LIMIT}
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: "on-failure:${ARENA_TEAM_MAX_RESTARTS}"

  team${i}-ssh:
    build:
      context: .
      dockerfile: docker/ssh/Dockerfile
      args:
        TEAM_ID: "${i}"
        TEAM_NAME: "team${i}"
        TEAM_USER: "${username}"
        TEAM_PASS: "${password}"
        TEAM_UID: "1000"
    image: sandcastle/team${i}-ssh:latest
    container_name: team${i}-ssh
    hostname: team${i}-ssh
    depends_on:
      - team${i}-vuln
    networks:
      ctf-network:
        ipv4_address: ${ARENA_NETWORK_PREFIX}.${i}.2
    ports:
      - "${ARENA_SSH_BIND_HOST}:$((ARENA_SSH_BASE_PORT + i)):22"
    cap_add:
      - NET_ADMIN
    labels:
      sandcastle.role: "ssh-gateway"
      sandcastle.team: "team${i}"
    deploy:
      resources:
        limits:
          memory: ${ARENA_TEAM_SSH_MEM_LIMIT}
          cpus: '${ARENA_TEAM_SSH_CPU_LIMIT}'
          pids: ${ARENA_TEAM_SSH_PIDS_LIMIT}
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: "on-failure:${ARENA_TEAM_MAX_RESTARTS}"
EOF
    done

    cat >> "${COMPOSE_FILE}" <<EOF

  firewall:
    build:
      context: ./firewall
    image: sandcastle/firewall:latest
    container_name: sandcastle-firewall
    hostname: sandcastle-firewall
    network_mode: host
    environment:
      CTF_NETWORK: "${ARENA_CTF_SUBNET}"
      CTF_GATEWAY: "${ARENA_CTF_GATEWAY}"
      WS_PORT: "${ARENA_FIREWALL_WS_PORT}"
      PROXY_PORT: "${ARENA_FIREWALL_PROXY_PORT}"
      EVENT_QUEUE_SIZE: "${ARENA_FIREWALL_EVENT_QUEUE_SIZE}"
      CAPTURE_RCVBUF_BYTES: "${ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES}"
      RECENT_ICMP_LIMIT: "${ARENA_FIREWALL_RECENT_ICMP_LIMIT}"
    cap_add:
      - NET_ADMIN
      - NET_RAW
    labels:
      sandcastle.role: "firewall"
    deploy:
      resources:
        limits:
          memory: ${ARENA_FIREWALL_MEM_LIMIT}
          cpus: '${ARENA_FIREWALL_CPU_LIMIT}'
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: unless-stopped

  gameserver:
    build:
      context: .
      dockerfile: gameserver/Dockerfile
    image: sandcastle/gameserver:latest
    container_name: sandcastle-gameserver
    hostname: sandcastle-gameserver
    networks:
      ctf-network:
        ipv4_address: ${ARENA_NETWORK_PREFIX}.0.2
      control-plane: {}
    ports:
      - "${ARENA_GAMESERVER_PORT}:8000"
    volumes:
      - gameserver-data:/app/data
      - ./config/arena.env:/app/config/arena.env:ro
    environment:
      CHECKER_MASTER_SECRET: "${ARENA_CHECKER_SECRET}"
      GAMESERVER_OPERATOR_TOKEN: "${ARENA_OPERATOR_TOKEN}"
    labels:
      sandcastle.role: "gameserver"
    deploy:
      resources:
        limits:
          memory: ${ARENA_GAMESERVER_MEM_LIMIT}
          cpus: '${ARENA_GAMESERVER_CPU_LIMIT}'
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: unless-stopped

  bot-controller:
    build:
      context: .
      dockerfile: bot/Dockerfile
    image: sandcastle/bot-controller:latest
    container_name: sandcastle-bot-controller
    hostname: sandcastle-bot-controller
    networks:
      ctf-network:
        ipv4_address: ${ARENA_NETWORK_PREFIX}.0.4
    ports:
      - "127.0.0.1:${ARENA_BOT_API_PORT}:${ARENA_BOT_API_PORT}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./config/arena.env:/app/config/arena.env:ro
      - ./challenges:/app/challenges
      - bot-controller-data:/data
    environment:
      ARENA_BOT_API_PORT: "${ARENA_BOT_API_PORT}"
      CHALLENGE_VALIDATION_NETWORK: "sandcastle_ctf-network"
      OPENAI_API_KEY: "\${OPENAI_API_KEY:-}"
      GEMINI_API_KEY: "\${GEMINI_API_KEY:-}"
    labels:
      sandcastle.role: "bot-controller"
      sandcastle.visualizer.hidden: "true"
    deploy:
      resources:
        limits:
          memory: ${ARENA_BOT_MEM_LIMIT}
          cpus: '${ARENA_BOT_CPU_LIMIT}'
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: unless-stopped

  visualizer:
    build:
      context: .
      dockerfile: visualizer/Dockerfile
    image: sandcastle/visualizer:latest
    container_name: sandcastle-visualizer
    hostname: sandcastle-visualizer
    networks:
      - control-plane
    ports:
      - "${ARENA_VISUALIZER_BIND_HOST}:${ARENA_VISUALIZER_PORT}:80"
    labels:
      sandcastle.role: "visualizer"
    deploy:
      resources:
        limits:
          memory: ${ARENA_VISUALIZER_MEM_LIMIT}
          cpus: '${ARENA_VISUALIZER_CPU_LIMIT}'
    logging:
      driver: json-file
      options:
        max-size: "${ARENA_LOG_MAX_SIZE}"
        max-file: "${ARENA_LOG_MAX_FILES}"
    restart: unless-stopped

volumes:
  gameserver-data:
    name: sandcastle_gameserver-data
    labels:
      sandcastle.role: "gameserver-data"
  bot-controller-data:
    name: sandcastle_bot-controller-data
    labels:
      sandcastle.role: "bot-controller-data"
EOF

    if [[ "${ARENA_ISOLATION_MODE}" == "dind" ]]; then
        for ((i = 1; i <= teams; i++)); do
            cat >> "${COMPOSE_FILE}" <<EOF
  team${i}-dind-data:
    name: sandcastle_team${i}-dind-data
    labels:
      sandcastle.role: "dind-data"
      sandcastle.team: "team${i}"
  team${i}-dind-run:
    name: sandcastle_team${i}-dind-run
    labels:
      sandcastle.role: "dind-run"
      sandcastle.team: "team${i}"
EOF
        done
    fi
}

print_summary() {
    local teams="$1"
    local i username password submission_token ssh_host

    echo
    echo "Generated ${teams} team(s) from ${ARENA_CONFIG_FILE#"${ROOT}"/}."
    echo
    printf '%-8s %-15s %-15s %-9s\n' \
        "Team" "SSH IP" "Vuln/App IP" "SSH Port"
    printf '%-8s %-15s %-15s %-9s\n' \
        "----" "------" "-----------" "--------"
    for ((i = 1; i <= teams; i++)); do
        printf '%-8s %-15s %-15s %-9s\n' \
            "team${i}" \
            "${ARENA_NETWORK_PREFIX}.${i}.2" \
            "${ARENA_NETWORK_PREFIX}.${i}.3" \
            "$((ARENA_SSH_BASE_PORT + i))"
    done
    echo
    echo "Service port: ${ARENA_SERVICE_PORT}"
    echo "Isolation mode: ${ARENA_ISOLATION_MODE}"
    echo "Round defaults: ${ARENA_ROUND_DURATION_SECONDS}s, expiry ${ARENA_FLAG_EXPIRY_ROUNDS} rounds, ${ARENA_CHECKER_MAX_CONCURRENCY} checker workers"

    if ((SHOW_ACCESS)); then
        echo
        echo "Development access details (contains credentials):"
        echo "Operator token: ${ARENA_OPERATOR_TOKEN}"
        for ((i = 1; i <= teams; i++)); do
            username="$(arena_config_render_team_value "${ARENA_TEAM_USERNAME_PATTERN}" "${i}")"
            password="$(arena_config_render_team_value "${ARENA_TEAM_PASSWORD_PATTERN}" "${i}")"
            submission_token="$(arena_config_render_team_value "${ARENA_TEAM_TOKEN_PATTERN}" "${i}")"
            ssh_host="${ARENA_SSH_BIND_HOST}"
            if [[ "${ssh_host}" == "127.0.0.1" || "${ssh_host}" == "::1" ]]; then
                ssh_host="localhost"
            fi
            echo
            echo "  team${i}"
            echo "    Gateway SSH:  ssh -p $((ARENA_SSH_BASE_PORT + i)) ${username}@${ssh_host}"
            echo "    Password:     ${password}"
            echo "    API token:    ${submission_token}"
            echo "    Vuln machine: ssh ${username}@team${i}-vuln"
            echo "    App target:   http://${ARENA_NETWORK_PREFIX}.${i}.3:${ARENA_SERVICE_PORT}"
            echo "    App health:   docker exec team${i}-vuln curl -fsS http://127.0.0.1:${ARENA_SERVICE_PORT}/health"
        done
        echo
        echo "  Firewall feed: ws://localhost:${ARENA_FIREWALL_WS_PORT}"
        echo "  Bot API:       http://${ARENA_BOT_API_HOST}:${ARENA_BOT_API_PORT}"
        echo "  Gameserver:    http://localhost:${ARENA_GAMESERVER_PORT}"
        echo "  Visualizer:    http://localhost:${ARENA_VISUALIZER_PORT}"
    else
        echo "Run ./scripts/setup.sh --show-access to print development credentials and connection commands."
    fi

    echo
    echo "Next:"
    echo "  ./scripts/arena.sh up"
    echo "  ./scripts/arena.sh status"
    echo "  ./scripts/doctor.sh"
}

main() {
    arena_config_load "${ROOT}" || exit 1
    parse_args "$@"
    validate_requested_team_count
    resolve_template
    preflight_team_workspaces "${ARENA_TEAM_COUNT}"
    handle_orphan_containers "${ARENA_TEAM_COUNT}"

    if ((OVERWRITE_SERVICES)); then
        warn "DESTRUCTIVE MODE ENABLED: configured generated service copies will be replaced"
    fi

    if [[ -n "${REQUESTED_TEAM_COUNT}" ]]; then
        arena_config_set_team_count "${ARENA_CONFIG_FILE}" "${ARENA_TEAM_COUNT}" ||
            die "could not persist ARENA_TEAM_COUNT in ${ARENA_CONFIG_FILE}"
    fi
    if [[ -n "${REQUESTED_ISOLATION_MODE}" ]]; then
        arena_config_set_isolation_mode "${ARENA_CONFIG_FILE}" "${ARENA_ISOLATION_MODE}" ||
            die "could not persist ARENA_ISOLATION_MODE in ${ARENA_CONFIG_FILE}"
    fi

    mkdir -p "${GENERATED_TEAMS_DIR}"
    remove_legacy_generated_teams
    prune_extra_teams "${ARENA_TEAM_COUNT}"

    local i team_dir
    for ((i = 1; i <= ARENA_TEAM_COUNT; i++)); do
        team_dir="${GENERATED_TEAMS_DIR}/team${i}"
        mkdir -p "${team_dir}"
        prepare_service_workspace "${i}" "${team_dir}"
        write_team_service_compose "${i}" "${team_dir}"
    done

    write_compose "${ARENA_TEAM_COUNT}"
    print_summary "${ARENA_TEAM_COUNT}"
}

main "$@"
