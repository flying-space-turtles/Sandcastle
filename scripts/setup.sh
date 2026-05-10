#!/usr/bin/env bash
# Generate a local Sandcastle Attack & Defense scaffold for N teams.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEAMS_DIR="${ROOT}/teams"
DEFAULT_SERVICE_TEMPLATE="${ROOT}/services/example-vuln"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
DEFAULT_TEAMS=3
MAX_TEAMS=250
NUM_TEAMS="${DEFAULT_TEAMS}"
TEMPLATE_DIR="${DEFAULT_SERVICE_TEMPLATE}"
OVERWRITE_SERVICES=0
PRUNE_EXTRA_TEAMS=1

usage() {
    cat <<EOF
Usage:
  ./scripts/setup.sh --teams N
  ./scripts/setup.sh -t N

Options:
  --teams, -t   Number of teams to generate. Defaults to ${DEFAULT_TEAMS}.
  --template    Service template directory to copy. Defaults to services/example-vuln.
  --overwrite-services
                Replace existing teams/team<N>/service directories with the template.
  --no-prune    Keep generated teams/team<N> directories above the requested count.
  --help, -h    Show this help text.

Compatibility:
  ./scripts/setup.sh N  also works for the old positional form.
EOF
}

die() {
    echo "setup.sh: $*" >&2
    exit 1
}

parse_args() {
    local value=""
    local provided=0

    while (($#)); do
        case "$1" in
            --teams|-t)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                value="$2"
                provided=1
                shift 2
                ;;
            --teams=*)
                value="${1#*=}"
                provided=1
                shift
                ;;
            --template)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                TEMPLATE_DIR="$2"
                shift 2
                ;;
            --template=*)
                TEMPLATE_DIR="${1#*=}"
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
            -h|--help)
                usage
                exit 0
                ;;
            [0-9]*)
                [[ "${provided}" -eq 0 ]] || die "team count provided more than once"
                value="$1"
                provided=1
                shift
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done

    if [[ "${provided}" -eq 0 ]]; then
        value="${DEFAULT_TEAMS}"
    fi

    [[ -n "${value}" ]] || die "team count must be provided"
    [[ "${value}" =~ ^[0-9]+$ ]] || die "team count must be a positive integer"
    value="$((10#${value}))"
    ((value >= 1 && value <= MAX_TEAMS)) || die "team count must be between 1 and ${MAX_TEAMS}"

    if [[ "${TEMPLATE_DIR}" != /* ]]; then
        TEMPLATE_DIR="${ROOT}/${TEMPLATE_DIR}"
    fi
    [[ -d "${TEMPLATE_DIR}" ]] || die "missing vulnerable service template: ${TEMPLATE_DIR}"

    NUM_TEAMS="${value}"
}

is_generated_team_dir() {
    local team_dir="$1"
    [[ -f "${team_dir}/.sandcastle-generated" ]]
}

prune_extra_teams() {
    local teams="$1"
    local team_dir name team_num

    [[ "${PRUNE_EXTRA_TEAMS}" -eq 1 ]] || return

    shopt -s nullglob
    for team_dir in "${TEAMS_DIR}"/team*; do
        [[ -d "${team_dir}" ]] || continue
        name="$(basename "${team_dir}")"
        [[ "${name}" =~ ^team([0-9]+)$ ]] || continue
        team_num="${BASH_REMATCH[1]}"
        team_num="$((10#${team_num}))"

        if ((team_num > teams)); then
            if is_generated_team_dir "${team_dir}"; then
                rm -rf "${team_dir}"
            else
                echo "[!] Keeping unmarked extra team directory: ${team_dir}" >&2
            fi
        fi
    done
    shopt -u nullglob
}

copy_service_template() {
    local team_dir="$1"
    local service_dir="${team_dir}/service"

    if [[ -d "${service_dir}" && "${OVERWRITE_SERVICES}" -eq 0 ]]; then
        return
    fi

    if [[ -d "${service_dir}" ]]; then
        rm -rf "${service_dir}"
    fi
    mkdir -p "${service_dir}"
    cp -a "${TEMPLATE_DIR}/." "${service_dir}/"
}

write_ssh_dockerfile() {
    local team_num="$1"
    local team_dir="$2"
    local ssh_dir="${team_dir}/ssh"
    local user="team${team_num}"
    local pass="team${team_num}pass"

    mkdir -p "${ssh_dir}"
    cat > "${ssh_dir}/Dockerfile" <<EOF
# syntax=docker/dockerfile:1.6
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \\
    TEAM_ID=${team_num} \\
    TEAM_NAME=team${team_num} \\
    TEAM_USER=${user}

RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
        openssh-server \\
        sudo \\
        curl \\
        ca-certificates \\
        gnupg \\
        vim \\
        nano \\
        net-tools \\
        iputils-ping \\
        dnsutils \\
        nmap \\
        python3 \\
        python3-pip \\
        git \\
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose plugin. The host Docker socket is mounted by compose.
RUN install -m 0755 -d /etc/apt/keyrings \\
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \\
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \\
    && chmod a+r /etc/apt/keyrings/docker.gpg \\
    && echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable" \\
        > /etc/apt/sources.list.d/docker.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends \\
        docker-ce-cli \\
        docker-compose-plugin \\
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash ${user} \\
    && echo '${user}:${pass}' | chpasswd \\
    && usermod -aG sudo ${user} \\
    && groupadd -f docker \\
    && usermod -aG docker ${user} \\
    && echo '${user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/${user} \\
    && chmod 0440 /etc/sudoers.d/${user}

RUN mkdir -p /run/sshd \\
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \\
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \\
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

COPY service /home/${user}/service
RUN chown -R ${user}:${user} /home/${user}/service

RUN printf '%s\\n' \\
    '' \\
    '================================================================' \\
    '  Sandcastle CTF Infrastructure - Team ${team_num}' \\
    '' \\
    '  This container is your team SSH gateway.' \\
    '' \\
    '  Network layout:' \\
    '    - SSH gateway:         10.10.${team_num}.2' \\
    '    - Vulnerable service:  10.10.${team_num}.3' \\
    '    - Shared network:      10.10.0.0/16' \\
    '' \\
    '  Reach your vulnerable app from this box:' \\
    '    curl http://team${team_num}-vuln:8080/health' \\
    '' \\
    '  Service source is in:' \\
    '    ~/service' \\
    '================================================================' \\
    '' \\
    > /etc/motd

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
EOF
}

write_compose() {
    local teams="$1"

    cat > "${COMPOSE_FILE}" <<EOF
# Auto-generated by ./scripts/setup.sh. Re-run that script instead of editing this file.
#
# Local Attack & Defense CTF topology:
#   - one SSH gateway container per team
#   - one vulnerable service container per team
#   - deterministic addresses on 10.10.0.0/16

name: sandcastle

networks:
  ctf-network:
    driver: bridge
    ipam:
      config:
        - subnet: 10.10.0.0/16
          gateway: 10.10.0.1

volumes:
EOF

    local i
    for ((i = 1; i <= teams; i++)); do
        printf '  team%d-data:\n' "${i}" >> "${COMPOSE_FILE}"
    done

    printf '\nservices:\n' >> "${COMPOSE_FILE}"
    for ((i = 1; i <= teams; i++)); do
        cat >> "${COMPOSE_FILE}" <<EOF
  team${i}-vuln:
    build:
      context: ./teams/team${i}/service
    image: sandcastle/team${i}-vuln:latest
    container_name: team${i}-vuln
    hostname: team${i}-vuln
    networks:
      ctf-network:
        ipv4_address: 10.10.${i}.3
    environment:
      TEAM_ID: "${i}"
      TEAM_NAME: "Team ${i}"
      SERVICE_PORT: "8080"
      SECRET_KEY: "sandcastle-team${i}-dev-secret"
    volumes:
      - team${i}-data:/app/data
    restart: unless-stopped

  team${i}-ssh:
    build:
      context: ./teams/team${i}
      dockerfile: ssh/Dockerfile
    image: sandcastle/team${i}-ssh:latest
    container_name: team${i}-ssh
    hostname: team${i}-ssh
    depends_on:
      - team${i}-vuln
    networks:
      ctf-network:
        ipv4_address: 10.10.${i}.2
    ports:
      - "$((2200 + i)):22"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    cap_add:
      - NET_ADMIN
    restart: unless-stopped

EOF
    done

    sed -i '${/^$/d;}' "${COMPOSE_FILE}"
}

print_summary() {
    local teams="$1"
    local i

    echo
    echo "Generated ${teams} team(s)."
    echo
    printf '%-8s %-15s %-15s %-9s %-12s %-12s\n' "Team" "SSH IP" "Service IP" "SSH Port" "Username" "Password"
    printf '%-8s %-15s %-15s %-9s %-12s %-12s\n' "----" "------" "----------" "--------" "--------" "--------"
    for ((i = 1; i <= teams; i++)); do
        printf '%-8s %-15s %-15s %-9s %-12s %-12s\n' \
            "team${i}" "10.10.${i}.2" "10.10.${i}.3" "$((2200 + i))" "team${i}" "team${i}pass"
    done
    echo
    echo "Next:"
    echo "  docker compose up --build"
    echo "  ssh -p 2201 team1@localhost"
}

main() {
    local teams
    parse_args "$@"
    teams="${NUM_TEAMS}"

    mkdir -p "${TEAMS_DIR}"
    prune_extra_teams "${teams}"

    local i team_dir
    for ((i = 1; i <= teams; i++)); do
        team_dir="${TEAMS_DIR}/team${i}"
        mkdir -p "${team_dir}"
        printf 'Generated by ./scripts/setup.sh for team%s.\n' "${i}" > "${team_dir}/.sandcastle-generated"
        copy_service_template "${team_dir}"
        write_ssh_dockerfile "${i}" "${team_dir}"
    done

    write_compose "${teams}"
    print_summary "${teams}"
}

main "$@"
