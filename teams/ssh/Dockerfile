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

# A small MOTD so participants immediately know what to do.
RUN cat <<'EOF' > /etc/motd

================================================================
  AD-CTF Local Simulation — Team Box

  Your vulnerable service source code is in:   ~/service/
  Manage it with the standard docker-compose verbs:

    cd ~/service
    docker compose ps
    docker compose logs -f
    docker compose up -d --build
    docker compose down

  The gameserver lives at  http://10.10.0.2:8080
  (scoreboard, /api/state, /api/submit)
================================================================

EOF

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
