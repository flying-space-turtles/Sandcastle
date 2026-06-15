# syntax=docker/dockerfile:1.6
FROM ubuntu:22.04

ARG TEAM_ID=0
ARG TEAM_NAME=team0
ARG TEAM_USER=team0
ARG TEAM_PASS=changeme
ARG TEAM_UID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    TEAM_ID=${TEAM_ID} \
    TEAM_NAME=${TEAM_NAME} \
    TEAM_USER=${TEAM_USER}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server \
        openssh-client \
        sudo \
        curl \
        ca-certificates \
        gnupg \
        vim \
        nano \
        net-tools \
        iproute2 \
        iputils-ping \
        dnsutils \
        nmap \
        socat \
        python3 \
        python3-pip \
        git \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose plugin. The host Docker socket is mounted by compose.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-buildx-plugin \
        docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u "${TEAM_UID}" -s /bin/bash "${TEAM_USER}" \
    && echo "${TEAM_USER}:${TEAM_PASS}" | chpasswd \
    && usermod -aG sudo "${TEAM_USER}" \
    && groupadd -f docker \
    && usermod -aG docker "${TEAM_USER}" \
    && echo "${TEAM_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${TEAM_USER}" \
    && chmod 0440 "/etc/sudoers.d/${TEAM_USER}" \
    && mkdir -p "/home/${TEAM_USER}/example-vuln" \
    && chown -R "${TEAM_USER}:${TEAM_USER}" "/home/${TEAM_USER}/example-vuln"

COPY bot/service_control.py /usr/local/bin/sandcastle-service-control
COPY bot/bot_lib /usr/local/lib/sandcastle-bot/bot_lib

RUN mkdir -p /run/sshd \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

RUN printf '%s\n' \
    '' \
    '================================================================' \
    '  Sandcastle CTF Infrastructure - Vulnerable Machine' \
    '' \
    '  This container is your vulnerable Linux machine.' \
    '' \
    '  Vulnerable app source is mounted at:' \
    '    ~/example-vuln' \
    '' \
    '  Start or rebuild the app container with:' \
    '    cd ~/example-vuln && docker compose up -d --build' \
    '' \
    "  Health check after startup:" \
    "    curl http://team${TEAM_ID}-vuln:8080/health" \
    '================================================================' \
    '' \
    > /etc/motd

RUN cat > /usr/local/bin/sandcastle-vuln-entrypoint <<'EOF' \
    && chmod 0755 /usr/local/bin/sandcastle-vuln-entrypoint
#!/usr/bin/env bash
set -euo pipefail

docker_socket="/var/run/docker.sock"
if [[ "${DOCKER_HOST:-}" == unix://* ]]; then
    docker_socket="${DOCKER_HOST#unix://}"
fi

if [[ "${ARENA_ISOLATION_MODE:-}" == "dind" ]]; then
    for _ in $(seq 1 60); do
        [[ -S "${docker_socket}" ]] && break
        sleep 1
    done
    if [[ -S "${docker_socket}" ]]; then
        ln -sfn "${docker_socket}" /var/run/docker.sock
    fi
    printf 'export DOCKER_HOST=%q\n' "${DOCKER_HOST:-unix://${docker_socket}}" \
        > /etc/profile.d/sandcastle-docker.sh
    chmod 0644 /etc/profile.d/sandcastle-docker.sh
fi

if [[ -S "${docker_socket}" ]]; then
    socket_gid="$(stat -c '%g' "${docker_socket}")"
    socket_group="$(getent group "${socket_gid}" | cut -d: -f1 || true)"

    if [[ -z "${socket_group}" ]]; then
        if groupmod -o -g "${socket_gid}" docker 2>/dev/null; then
            socket_group="docker"
        else
            socket_group="docker-host"
            groupadd -o -g "${socket_gid}" "${socket_group}"
        fi
    fi

    usermod -aG "${socket_group}" "${TEAM_USER}"
fi

if [[ "${ARENA_ISOLATION_MODE:-}" == "dind" ]]; then
    service_port="${ARENA_SERVICE_PORT:-8080}"
    dind_target="${SANDCASTLE_DIND_TARGET:-team${TEAM_ID}-dind}"
    (
        while true; do
            socat \
                "TCP-LISTEN:${service_port},fork,reuseaddr,bind=0.0.0.0" \
                "TCP:${dind_target}:${service_port}" || true
            sleep 1
        done
    ) &
fi

python3 /usr/local/bin/sandcastle-service-control &

exec "$@"
EOF

EXPOSE 22
EXPOSE 7979

ENTRYPOINT ["/usr/local/bin/sandcastle-vuln-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
