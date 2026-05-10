# syntax=docker/dockerfile:1.6
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TEAM_ID=6 \
    TEAM_NAME=team6 \
    TEAM_USER=team6

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
        docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash team6 \
    && echo 'team6:team6pass' | chpasswd \
    && usermod -aG sudo team6 \
    && groupadd -f docker \
    && usermod -aG docker team6 \
    && echo 'team6 ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/team6 \
    && chmod 0440 /etc/sudoers.d/team6

RUN mkdir -p /run/sshd \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

COPY service /home/team6/service
RUN chown -R team6:team6 /home/team6/service

RUN printf '%s\n' \
    '' \
    '================================================================' \
    '  Sandcastle CTF Infrastructure - Team 6' \
    '' \
    '  This container is your team SSH gateway.' \
    '' \
    '  Network layout:' \
    '    - SSH gateway:         10.10.6.2' \
    '    - Vulnerable service:  10.10.6.3' \
    '    - Shared network:      10.10.0.0/16' \
    '' \
    '  Reach your vulnerable app from this box:' \
    '    curl http://team6-vuln:8080/health' \
    '' \
    '  Service source is in:' \
    '    ~/service' \
    '================================================================' \
    '' \
    > /etc/motd

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
