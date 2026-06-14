#!/usr/bin/env bash
# Shared loader and validator for config/arena.env.

arena_config_error() {
    ARENA_CONFIG_ERROR="$*"
    if [[ "${ARENA_CONFIG_SILENT:-0}" != "1" ]]; then
        printf 'arena config: %s\n' "$*" >&2
    fi
}

arena_config_require_mem_limit() {
    local name="$1"
    local value="${!name:-}"
    if [[ -z "${value}" || ! "${value}" =~ ^[0-9]+[bBkKmMgG]$ ]]; then
        arena_config_error "${name} must be a Docker memory value like 256m or 1g, got '${value}'"
        return 1
    fi
}

arena_config_require_cpu_limit() {
    local name="$1"
    local value="${!name:-}"
    if [[ -z "${value}" || ! "${value}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        arena_config_error "${name} must be a decimal CPU fraction like 0.50 or 1.00, got '${value}'"
        return 1
    fi
}

arena_config_require_int() {
    local name="$1"
    local min="$2"
    local max="$3"
    local value="${!name:-}"

    if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
        arena_config_error "${name} must be an integer, got '${value}'"
        return 1
    fi
    value="$((10#${value}))"
    if ((value < min || value > max)); then
        arena_config_error "${name} must be between ${min} and ${max}, got ${value}"
        return 1
    fi
    printf -v "${name}" '%s' "${value}"
}

arena_config_validate_pattern() {
    local name="$1"
    local value="${!name:-}"

    if [[ -z "${value}" || "${value}" != *"{team}"* ]]; then
        arena_config_error "${name} must contain the literal {team} placeholder"
        return 1
    fi
    if [[ ! "${value}" =~ ^[a-zA-Z0-9_.{}-]+$ ]]; then
        arena_config_error "${name} contains unsupported characters"
        return 1
    fi
}

arena_config_validate_port_layout() {
    local ssh_last_port=$((ARENA_SSH_BASE_PORT + ARENA_TEAM_COUNT))
    local -a host_ports=(
        "${ARENA_FIREWALL_WS_PORT}"
        "${ARENA_FIREWALL_PROXY_PORT}"
        "${ARENA_BOT_API_PORT}"
        "${ARENA_GAMESERVER_PORT}"
    )
    local port

    if ((ssh_last_port > 65535)); then
        arena_config_error \
            "ARENA_SSH_BASE_PORT + ARENA_TEAM_COUNT must not exceed 65535"
        return 1
    fi

    for port in "${host_ports[@]}"; do
        if ((port > ARENA_SSH_BASE_PORT && port <= ssh_last_port)); then
            arena_config_error \
                "host port ${port} collides with the configured team SSH range"
            return 1
        fi
    done
    if [[ "${ARENA_FIREWALL_WS_PORT}" == "${ARENA_FIREWALL_PROXY_PORT}" ||
          "${ARENA_FIREWALL_WS_PORT}" == "${ARENA_BOT_API_PORT}" ||
          "${ARENA_FIREWALL_WS_PORT}" == "${ARENA_GAMESERVER_PORT}" ||
          "${ARENA_FIREWALL_PROXY_PORT}" == "${ARENA_BOT_API_PORT}" ||
          "${ARENA_FIREWALL_PROXY_PORT}" == "${ARENA_GAMESERVER_PORT}" ||
          "${ARENA_BOT_API_PORT}" == "${ARENA_GAMESERVER_PORT}" ]]; then
        arena_config_error "firewall, gameserver and bot API host ports must be distinct"
        return 1
    fi
    if [[ "${ARENA_FIREWALL_PROBE_PORT}" == "${ARENA_SERVICE_PORT}" ||
          "${ARENA_FIREWALL_PROBE_PORT}" == "22" ]]; then
        arena_config_error \
            "ARENA_FIREWALL_PROBE_PORT must not collide with the team service or SSH port"
        return 1
    fi
}

arena_config_load() {
    local root="$1"
    local config_file="${SANDCASTLE_ARENA_CONFIG:-${root}/config/arena.env}"
    local subnet_a subnet_b gateway_expected octet token_sample
    local -a required=(
        ARENA_TEAM_COUNT
        ARENA_CTF_SUBNET
        ARENA_CTF_GATEWAY
        ARENA_SSH_BASE_PORT
        ARENA_SERVICE_PORT
        ARENA_TEAM_USERNAME_PATTERN
        ARENA_TEAM_PASSWORD_PATTERN
        ARENA_TEAM_TOKEN_PATTERN
        ARENA_SERVICE_TEMPLATE
        ARENA_FIREWALL_WS_PORT
        ARENA_FIREWALL_PROXY_PORT
        ARENA_FIREWALL_PROBE_PORT
        ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS
        ARENA_FIREWALL_EVENT_QUEUE_SIZE
        ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES
        ARENA_FIREWALL_RECENT_ICMP_LIMIT
        ARENA_BOT_API_HOST
        ARENA_BOT_API_PORT
        ARENA_BOT_LOOP_SECONDS
        ARENA_STARTUP_TIMEOUT_SECONDS
        ARENA_ROUND_DURATION_SECONDS
        ARENA_FLAG_EXPIRY_ROUNDS
        ARENA_CHECKER_MAX_CONCURRENCY
        ARENA_GAMESERVER_PORT
        ARENA_OPERATOR_TOKEN
        ARENA_SUBMISSION_RATE_LIMIT
        ARENA_SUBMISSION_RATE_WINDOW_SECONDS
        ARENA_SCORE_ATTACK_POINTS
        ARENA_SCORE_DEFENSE_POINTS
        ARENA_SCORE_SLA_POINTS
        ARENA_CHECKER_SECRET
    )

    ARENA_CONFIG_ERROR=""
    if [[ ! -f "${config_file}" ]]; then
        arena_config_error "missing ${config_file}"
        return 1
    fi

    local name
    for name in "${required[@]}"; do
        unset "${name}"
    done

    # The file is committed project configuration, not a user secrets file.
    # shellcheck disable=SC1090
    source "${config_file}"

    # Resource limit defaults (all optional; safe values for development)
    ARENA_TEAM_VULN_MEM_LIMIT="${ARENA_TEAM_VULN_MEM_LIMIT:-512m}"
    ARENA_TEAM_VULN_CPU_LIMIT="${ARENA_TEAM_VULN_CPU_LIMIT:-0.50}"
    ARENA_TEAM_VULN_PIDS_LIMIT="${ARENA_TEAM_VULN_PIDS_LIMIT:-200}"
    ARENA_TEAM_SSH_MEM_LIMIT="${ARENA_TEAM_SSH_MEM_LIMIT:-128m}"
    ARENA_TEAM_SSH_CPU_LIMIT="${ARENA_TEAM_SSH_CPU_LIMIT:-0.25}"
    ARENA_TEAM_SSH_PIDS_LIMIT="${ARENA_TEAM_SSH_PIDS_LIMIT:-100}"
    ARENA_TEAM_APP_MEM_LIMIT="${ARENA_TEAM_APP_MEM_LIMIT:-256m}"
    ARENA_TEAM_APP_CPU_LIMIT="${ARENA_TEAM_APP_CPU_LIMIT:-0.50}"
    ARENA_TEAM_APP_PIDS_LIMIT="${ARENA_TEAM_APP_PIDS_LIMIT:-100}"
    ARENA_TEAM_MAX_RESTARTS="${ARENA_TEAM_MAX_RESTARTS:-5}"
    ARENA_GAMESERVER_MEM_LIMIT="${ARENA_GAMESERVER_MEM_LIMIT:-512m}"
    ARENA_GAMESERVER_CPU_LIMIT="${ARENA_GAMESERVER_CPU_LIMIT:-1.00}"
    ARENA_BOT_MEM_LIMIT="${ARENA_BOT_MEM_LIMIT:-256m}"
    ARENA_BOT_CPU_LIMIT="${ARENA_BOT_CPU_LIMIT:-0.50}"
    ARENA_FIREWALL_MEM_LIMIT="${ARENA_FIREWALL_MEM_LIMIT:-128m}"
    ARENA_FIREWALL_CPU_LIMIT="${ARENA_FIREWALL_CPU_LIMIT:-0.50}"
    ARENA_LOG_MAX_SIZE="${ARENA_LOG_MAX_SIZE:-50m}"
    ARENA_LOG_MAX_FILES="${ARENA_LOG_MAX_FILES:-3}"

    # Default to 8000 if not specified
    ARENA_GAMESERVER_PORT="${ARENA_GAMESERVER_PORT:-8000}"
    ARENA_OPERATOR_TOKEN="${ARENA_OPERATOR_TOKEN:-sandcastle-local-operator-token-change-me}"
    ARENA_CHECKER_MAX_CONCURRENCY="${ARENA_CHECKER_MAX_CONCURRENCY:-8}"
    ARENA_SUBMISSION_RATE_LIMIT="${ARENA_SUBMISSION_RATE_LIMIT:-60}"
    ARENA_SUBMISSION_RATE_WINDOW_SECONDS="${ARENA_SUBMISSION_RATE_WINDOW_SECONDS:-60}"
    ARENA_SCORE_ATTACK_POINTS="${ARENA_SCORE_ATTACK_POINTS:-10}"
    ARENA_SCORE_DEFENSE_POINTS="${ARENA_SCORE_DEFENSE_POINTS:-2}"
    ARENA_SCORE_SLA_POINTS="${ARENA_SCORE_SLA_POINTS:-1}"
    ARENA_CHECKER_SECRET="${ARENA_CHECKER_SECRET:-sandcastle-local-checker-secret-change-me}"
    ARENA_ISOLATION_MODE="${ARENA_ISOLATION_MODE:-trusted}"

    for name in "${required[@]}"; do
        if [[ -z "${!name:-}" ]]; then
            arena_config_error "${name} is required in ${config_file}"
            return 1
        fi
    done

    arena_config_require_int ARENA_TEAM_COUNT 1 250 || return 1
    arena_config_require_int ARENA_SSH_BASE_PORT 1024 65285 || return 1
    arena_config_require_int ARENA_SERVICE_PORT 1 65535 || return 1
    arena_config_require_int ARENA_FIREWALL_WS_PORT 1 65535 || return 1
    arena_config_require_int ARENA_FIREWALL_PROXY_PORT 1 65535 || return 1
    arena_config_require_int ARENA_FIREWALL_PROBE_PORT 1 65535 || return 1
    arena_config_require_int ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS 1 300 || return 1
    arena_config_require_int ARENA_FIREWALL_EVENT_QUEUE_SIZE 1 1000000 || return 1
    arena_config_require_int ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES 65536 268435456 || return 1
    arena_config_require_int ARENA_FIREWALL_RECENT_ICMP_LIMIT 1 1000000 || return 1
    arena_config_require_int ARENA_BOT_API_PORT 1 65535 || return 1
    arena_config_require_int ARENA_BOT_LOOP_SECONDS 0 86400 || return 1
    arena_config_require_int ARENA_STARTUP_TIMEOUT_SECONDS 1 86400 || return 1
    arena_config_require_int ARENA_ROUND_DURATION_SECONDS 1 86400 || return 1
    arena_config_require_int ARENA_FLAG_EXPIRY_ROUNDS 1 10000 || return 1
    arena_config_require_int ARENA_CHECKER_MAX_CONCURRENCY 1 256 || return 1
    arena_config_require_int ARENA_GAMESERVER_PORT 1 65535 || return 1
    arena_config_require_int ARENA_SUBMISSION_RATE_LIMIT 1 100000 || return 1
    arena_config_require_int ARENA_SUBMISSION_RATE_WINDOW_SECONDS 1 86400 || return 1
    arena_config_require_int ARENA_SCORE_ATTACK_POINTS 0 100000 || return 1
    arena_config_require_int ARENA_SCORE_DEFENSE_POINTS 0 100000 || return 1
    arena_config_require_int ARENA_SCORE_SLA_POINTS 0 100000 || return 1

    arena_config_require_mem_limit ARENA_TEAM_VULN_MEM_LIMIT || return 1
    arena_config_require_cpu_limit ARENA_TEAM_VULN_CPU_LIMIT || return 1
    arena_config_require_int ARENA_TEAM_VULN_PIDS_LIMIT 1 65535 || return 1
    arena_config_require_mem_limit ARENA_TEAM_SSH_MEM_LIMIT || return 1
    arena_config_require_cpu_limit ARENA_TEAM_SSH_CPU_LIMIT || return 1
    arena_config_require_int ARENA_TEAM_SSH_PIDS_LIMIT 1 65535 || return 1
    arena_config_require_mem_limit ARENA_TEAM_APP_MEM_LIMIT || return 1
    arena_config_require_cpu_limit ARENA_TEAM_APP_CPU_LIMIT || return 1
    arena_config_require_int ARENA_TEAM_APP_PIDS_LIMIT 1 65535 || return 1
    arena_config_require_int ARENA_TEAM_MAX_RESTARTS 0 100 || return 1
    arena_config_require_mem_limit ARENA_GAMESERVER_MEM_LIMIT || return 1
    arena_config_require_cpu_limit ARENA_GAMESERVER_CPU_LIMIT || return 1
    arena_config_require_mem_limit ARENA_BOT_MEM_LIMIT || return 1
    arena_config_require_cpu_limit ARENA_BOT_CPU_LIMIT || return 1
    arena_config_require_mem_limit ARENA_FIREWALL_MEM_LIMIT || return 1
    arena_config_require_cpu_limit ARENA_FIREWALL_CPU_LIMIT || return 1
    arena_config_require_mem_limit ARENA_LOG_MAX_SIZE || return 1
    arena_config_require_int ARENA_LOG_MAX_FILES 1 100 || return 1

    if ((${#ARENA_OPERATOR_TOKEN} < 24)); then
        arena_config_error "ARENA_OPERATOR_TOKEN must contain at least 24 characters"
        return 1
    fi
    if ((${#ARENA_CHECKER_SECRET} < 16)); then
        arena_config_error "ARENA_CHECKER_SECRET must contain at least 16 characters"
        return 1
    fi

    case "${ARENA_ISOLATION_MODE}" in
        trusted|isolated) ;;
        *)
            arena_config_error \
                "ARENA_ISOLATION_MODE must be 'trusted' or 'isolated', got '${ARENA_ISOLATION_MODE}'"
            return 1
            ;;
    esac

    if [[ ! "${ARENA_CTF_SUBNET}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.0\.0/16$ ]]; then
        arena_config_error \
            "ARENA_CTF_SUBNET must use the supported A.B.0.0/16 form"
        return 1
    fi
    subnet_a="${BASH_REMATCH[1]}"
    subnet_b="${BASH_REMATCH[2]}"
    for octet in "${subnet_a}" "${subnet_b}"; do
        if ((10#${octet} > 255)); then
            arena_config_error "ARENA_CTF_SUBNET contains an invalid octet"
            return 1
        fi
    done
    ARENA_NETWORK_PREFIX="$((10#${subnet_a})).$((10#${subnet_b}))"
    gateway_expected="${ARENA_NETWORK_PREFIX}.0.1"
    if [[ "${ARENA_CTF_GATEWAY}" != "${gateway_expected}" ]]; then
        arena_config_error \
            "ARENA_CTF_GATEWAY must be ${gateway_expected} for ${ARENA_CTF_SUBNET}"
        return 1
    fi

    arena_config_validate_pattern ARENA_TEAM_USERNAME_PATTERN || return 1
    arena_config_validate_pattern ARENA_TEAM_PASSWORD_PATTERN || return 1
    arena_config_validate_pattern ARENA_TEAM_TOKEN_PATTERN || return 1
    token_sample="${ARENA_TEAM_TOKEN_PATTERN//\{team\}/1}"
    if ((${#token_sample} < 24)); then
        arena_config_error "ARENA_TEAM_TOKEN_PATTERN must render at least 24 characters"
        return 1
    fi

    if [[ ! "${ARENA_BOT_API_HOST}" =~ ^[a-zA-Z0-9_.:-]+$ ]]; then
        arena_config_error "ARENA_BOT_API_HOST contains unsupported characters"
        return 1
    fi

    arena_config_validate_port_layout || return 1

    if [[ "${ARENA_SERVICE_TEMPLATE}" == /* ]]; then
        ARENA_SERVICE_TEMPLATE_PATH="${ARENA_SERVICE_TEMPLATE}"
    else
        ARENA_SERVICE_TEMPLATE_PATH="${root}/${ARENA_SERVICE_TEMPLATE}"
    fi
    if [[ ! -d "${ARENA_SERVICE_TEMPLATE_PATH}" ]]; then
        arena_config_error \
            "ARENA_SERVICE_TEMPLATE does not exist: ${ARENA_SERVICE_TEMPLATE_PATH}"
        return 1
    fi

    ARENA_SERVICE_IP_PATTERN="${ARENA_NETWORK_PREFIX}.{team}.3"
    ARENA_CONFIG_FILE="${config_file}"
    export \
        ARENA_TEAM_COUNT \
        ARENA_CTF_SUBNET \
        ARENA_CTF_GATEWAY \
        ARENA_NETWORK_PREFIX \
        ARENA_SSH_BASE_PORT \
        ARENA_SERVICE_PORT \
        ARENA_SERVICE_IP_PATTERN \
        ARENA_TEAM_USERNAME_PATTERN \
        ARENA_TEAM_PASSWORD_PATTERN \
        ARENA_TEAM_TOKEN_PATTERN \
        ARENA_SERVICE_TEMPLATE \
        ARENA_SERVICE_TEMPLATE_PATH \
        ARENA_FIREWALL_WS_PORT \
        ARENA_FIREWALL_PROXY_PORT \
        ARENA_FIREWALL_PROBE_PORT \
        ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS \
        ARENA_FIREWALL_EVENT_QUEUE_SIZE \
        ARENA_FIREWALL_CAPTURE_RCVBUF_BYTES \
        ARENA_FIREWALL_RECENT_ICMP_LIMIT \
        ARENA_BOT_API_HOST \
        ARENA_BOT_API_PORT \
        ARENA_BOT_LOOP_SECONDS \
        ARENA_STARTUP_TIMEOUT_SECONDS \
        ARENA_ROUND_DURATION_SECONDS \
        ARENA_FLAG_EXPIRY_ROUNDS \
        ARENA_CHECKER_MAX_CONCURRENCY \
        ARENA_GAMESERVER_PORT \
        ARENA_OPERATOR_TOKEN \
        ARENA_SUBMISSION_RATE_LIMIT \
        ARENA_SUBMISSION_RATE_WINDOW_SECONDS \
        ARENA_SCORE_ATTACK_POINTS \
        ARENA_SCORE_DEFENSE_POINTS \
        ARENA_SCORE_SLA_POINTS \
        ARENA_CHECKER_SECRET \
        ARENA_ISOLATION_MODE \
        ARENA_TEAM_VULN_MEM_LIMIT \
        ARENA_TEAM_VULN_CPU_LIMIT \
        ARENA_TEAM_VULN_PIDS_LIMIT \
        ARENA_TEAM_SSH_MEM_LIMIT \
        ARENA_TEAM_SSH_CPU_LIMIT \
        ARENA_TEAM_SSH_PIDS_LIMIT \
        ARENA_TEAM_APP_MEM_LIMIT \
        ARENA_TEAM_APP_CPU_LIMIT \
        ARENA_TEAM_APP_PIDS_LIMIT \
        ARENA_TEAM_MAX_RESTARTS \
        ARENA_GAMESERVER_MEM_LIMIT \
        ARENA_GAMESERVER_CPU_LIMIT \
        ARENA_BOT_MEM_LIMIT \
        ARENA_BOT_CPU_LIMIT \
        ARENA_FIREWALL_MEM_LIMIT \
        ARENA_FIREWALL_CPU_LIMIT \
        ARENA_LOG_MAX_SIZE \
        ARENA_LOG_MAX_FILES \
        ARENA_CONFIG_FILE
}

arena_config_render_team_value() {
    local pattern="$1"
    local team_id="$2"
    printf '%s' "${pattern//\{team\}/${team_id}}"
}

arena_config_set_team_count() {
    local config_file="$1"
    local team_count="$2"
    local temp_file

    temp_file="$(mktemp "${config_file}.tmp.XXXXXX")" || return 1
    awk -v value="${team_count}" '
        BEGIN { updated = 0 }
        /^ARENA_TEAM_COUNT=/ {
            print "ARENA_TEAM_COUNT=" value
            updated = 1
            next
        }
        { print }
        END {
            if (!updated) {
                exit 2
            }
        }
    ' "${config_file}" > "${temp_file}" || {
        rm -f "${temp_file}"
        return 1
    }
    chmod --reference="${config_file}" "${temp_file}"
    mv "${temp_file}" "${config_file}"
}
