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
        sudo \
        curl \
        ca-certificates \
        gnupg \
        vim \
        nano \
        net-tools \
        iputils-ping \
        dnsutils \
        nmap \
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
    && mkdir -p "/home/${TEAM_USER}/service" \
    && chown -R "${TEAM_USER}:${TEAM_USER}" "/home/${TEAM_USER}/service"

RUN mkdir -p /run/sshd \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

RUN cat > /usr/local/bin/redeploy-service <<'EOF' \
    && chmod 0755 /usr/local/bin/redeploy-service
#!/usr/bin/env bash
set -euo pipefail

cd "${HOME}/service"
exec sudo env COMPOSE_IGNORE_ORPHANS=1 docker compose -p sandcastle -f sandcastle-compose.yml up -d --build
EOF

RUN printf '%s\n' \
    '' \
    '================================================================' \
    '  Sandcastle CTF Infrastructure - Team Box' \
    '' \
    '  This container is your team SSH gateway.' \
    '' \
    '  Service source is bind-mounted at:' \
    '    ~/service' \
    '' \
    '  After patching, rebuild your vulnerable service with:' \
    '    redeploy-service' \
    '' \
    '  Health check:' \
    "    curl http://team${TEAM_ID}-vuln:8080/health" \
    '================================================================' \
    '' \
    > /etc/motd

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
