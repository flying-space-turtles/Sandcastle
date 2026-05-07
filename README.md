# Sandcastle

Small Docker sandbox for testing basic “user vs bot” traffic patterns.

This repo spins up two containers (both based on the same Apache image):

- **User app** (`user_app1`): serves [user_docker/user_app1/html/index.html](user_docker/user_app1/html/index.html) at `192.168.100.1` (also published on `http://localhost:8080`).
- **Bot app** (`bot1_app1`): sits at `192.168.101.1` and repeatedly sends HTTP requests to the user app (`curl http://192.168.100.1`).

## Prerequisites

- Docker + Docker Compose
- A Linux host (or a Linux VM/WSL2) if you want to use `macvlan`

## Build the image

The Compose files reference an image named `vulnerable_web`, so build it once:

```bash
docker build -t vulnerable_web ./vulnerable_web
```

## Network setup (macvlan)

Both Compose files use static IPs `192.168.100.1` and `192.168.101.1`.
Create a `macvlan` network that contains **both** ranges (e.g. `192.168.100.0/23`).

```bash
sudo docker network create -d macvlan \
  --subnet=192.168.100.0/23 \
  --gateway=192.168.101.254 \
  -o parent=enp4s0 \
  global_macvlan_net
```

Optional: add a host-side macvlan interface so the host can reach the containers.

```bash
sudo ip link add ctflan link eth0 type macvlan mode bridge
sudo ip addr add 192.168.100.100/23 dev ctflan
sudo ip link set ctflan up
```

Adjust `parent=enp4s0` / `link eth0` to match your actual network interface.

## Run

```bash
docker compose -f user_docker/user-docker-compose.yml up -d
docker compose -f bot_docker/bot1-docker-compose.yml up -d
```

## Verify

- User page: `http://localhost:8080` (or `http://192.168.100.1` if you can route to it)
- Bot activity:

```bash
docker logs -f bot1_app1
```

## Stop

```bash
docker compose -f bot_docker/bot1-docker-compose.yml down
docker compose -f user_docker/user-docker-compose.yml down
```

## Notes

- Both services mount `*/html/` into `/var/www/html`, which overrides the image’s built-in `index.html`.
- The bot container currently overrides the image command to run a `curl` loop, so Apache inside `bot1_app1` is not started (the `8082:80` mapping is therefore unused unless you change the bot command).
- The `:z` suffix in the Compose volume mounts is meant for SELinux relabeling; if you’re not on SELinux (or you’re on Docker Desktop), you may need to remove it.