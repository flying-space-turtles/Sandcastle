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
        vim \
        nano \
        net-tools \
        iputils-ping \
        iproute2 \
        dnsutils \
        nmap \
        python3 \
        python3-pip \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u "${TEAM_UID}" -s /bin/bash "${TEAM_USER}" \
    && echo "${TEAM_USER}:${TEAM_PASS}" | chpasswd \
    && usermod -aG sudo "${TEAM_USER}" \
    && echo "${TEAM_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${TEAM_USER}" \
    && chmod 0440 "/etc/sudoers.d/${TEAM_USER}"

RUN mkdir -p /run/sshd \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

RUN printf '%s\n' \
    '' \
    '================================================================' \
    '  Sandcastle CTF Infrastructure - Team SSH Gateway' \
    '' \
    '  This container is your host-facing jump box.' \
    '' \
    '  Reach the vulnerable Linux machine with:' \
    "    ssh ${TEAM_USER}@team${TEAM_ID}-vuln" \
    '' \
    '  Use the same password you used for this gateway.' \
    '================================================================' \
    '' \
    > /etc/motd

RUN cat > /usr/local/bin/sandcastle-ssh-entrypoint <<'SCRIPT' \
    && chmod 0755 /usr/local/bin/sandcastle-ssh-entrypoint
#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${GATEWAY_OVERRIDE:-}" ]]; then
    ip route replace default via "${GATEWAY_OVERRIDE}" 2>/dev/null || true
fi

exec "$@"
SCRIPT

EXPOSE 22

ENTRYPOINT ["/usr/local/bin/sandcastle-ssh-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
