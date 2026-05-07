FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

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

# Docker CLI + Compose plugin (no daemon — uses the mounted host socket)
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu jammy stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

ARG TEAM_USER=ctfuser
ARG TEAM_PASS=changeme
ARG TEAM_NAME=team
ENV TEAM_USER=${TEAM_USER} \
    TEAM_NAME=${TEAM_NAME}

RUN useradd -m -s /bin/bash ${TEAM_USER} \
    && echo "${TEAM_USER}:${TEAM_PASS}" | chpasswd \
    && usermod -aG sudo ${TEAM_USER} \
    && groupadd -f docker \
    && usermod -aG docker ${TEAM_USER} \
    && echo "${TEAM_USER} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/${TEAM_USER} \
    && chmod 0440 /etc/sudoers.d/${TEAM_USER}

RUN mkdir -p /run/sshd \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

# A small MOTD so participants understand the scaffold.
RUN cat <<'EOF' > /etc/motd

================================================================
  Sandcastle CTF Infrastructure - Team Box

  This container is your team's SSH gateway.

  Network layout:
    - SSH gateway:              10.10.<team>.2
    - Vulnerable app slot:      10.10.<team>.3
    - Shared Docker network:    10.10.0.0/16

  Challenge source and app-specific tooling are supplied separately.
  The host Docker socket is mounted so organizers can provide workflows
  for managing each team's future vulnerable application container.
================================================================

EOF

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
