#!/usr/bin/env bash
# Prove transparent TCP enforcement, source masking, and event emission.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# shellcheck source=scripts/lib/arena_config.sh
source "${ROOT}/scripts/lib/arena_config.sh"

TMP_DIR="$(mktemp -d)"
EVENT_FILE="${TMP_DIR}/event.json"
EVENT_LOG="${TMP_DIR}/subscriber.log"
TOKEN="sc004-$(date +%s)-$$"
READY_FILE="/tmp/${TOKEN}.ready"
PROBE_READY_FILE="/tmp/${TOKEN}.probe-ready"
SUBSCRIBER_PID=""

cleanup() {
    if [[ -n "${SUBSCRIBER_PID}" ]]; then
        kill "${SUBSCRIBER_PID}" >/dev/null 2>&1 || true
        wait "${SUBSCRIBER_PID}" >/dev/null 2>&1 || true
    fi
    docker exec sandcastle-firewall rm -f "${READY_FILE}" >/dev/null 2>&1 || true
    docker exec team2-vuln rm -f "${PROBE_READY_FILE}" >/dev/null 2>&1 || true
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

die() {
    echo "smoke-network.sh: $*" >&2
    if [[ -s "${EVENT_LOG}" ]]; then
        sed 's/^/  subscriber: /' "${EVENT_LOG}" >&2
    fi
    exit 1
}

container_state() {
    docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null || true
}

redirect_packets() {
    docker exec sandcastle-firewall sh -ec '
        iptables -t nat -L PREROUTING -n -v -x --line-numbers |
            awk "/sandcastle-firewall-transparent-proxy/ { print \$2; exit }"
    '
}

wait_for_probe() {
    local attempt
    for ((attempt = 1; attempt <= ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS; attempt++)); do
        if docker exec team2-vuln test -f "${PROBE_READY_FILE}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_subscriber() {
    local attempt
    for ((attempt = 1; attempt <= ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS; attempt++)); do
        if docker exec sandcastle-firewall test -f "${READY_FILE}" >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "${SUBSCRIBER_PID}" >/dev/null 2>&1; then
            return 1
        fi
        sleep 1
    done
    return 1
}

arena_config_load "${ROOT}" || exit 1
((ARENA_TEAM_COUNT >= 2)) ||
    die "at least two configured teams are required"

command -v docker >/dev/null 2>&1 ||
    die "Docker CLI is not installed"
docker info >/dev/null 2>&1 ||
    die "Docker daemon is not reachable"

required_containers=(team1-vuln team2-vuln sandcastle-firewall)
if [[ "${ARENA_ISOLATION_MODE}" != "dind" ]]; then
    required_containers+=(team1-vuln-app team2-vuln-app)
fi

for container in "${required_containers[@]}"; do
    [[ "$(container_state "${container}")" == "running" ]] ||
        die "required container is not running: ${container}"
done

source_ip="${ARENA_NETWORK_PREFIX}.1.3"
destination_ip="${ARENA_NETWORK_PREFIX}.2.3"
expected_mask="${ARENA_CTF_GATEWAY}"

before_packets="$(redirect_packets)"
[[ "${before_packets}" =~ ^[0-9]+$ ]] ||
    die "could not read the transparent redirect counter"

docker exec -d team2-vuln python3 -c '
import socket
import sys
from pathlib import Path

port = int(sys.argv[1])
timeout = int(sys.argv[2])
ready_file = sys.argv[3]
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", port))
server.listen(1)
server.settimeout(timeout)
Path(ready_file).write_text("ready\n")
connection, peer = server.accept()
connection.settimeout(timeout)
connection.recv(65536)
body = peer[0].encode("ascii")
connection.sendall(
    b"HTTP/1.1 200 OK\r\n"
    + b"Content-Type: text/plain\r\n"
    + b"Content-Length: "
    + str(len(body)).encode("ascii")
    + b"\r\nConnection: close\r\n\r\n"
    + body
)
connection.close()
server.close()
' \
    "${ARENA_FIREWALL_PROBE_PORT}" \
    "${ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS}" \
    "${PROBE_READY_FILE}"

wait_for_probe ||
    die "Team 2 probe listener did not become ready"

docker exec sandcastle-firewall python -c '
import asyncio
import json
import pathlib
import sys
import time

import websockets

url, token, ready_file, timeout, expected_src, expected_dst, expected_mask = sys.argv[1:]

async def receive():
    deadline = time.monotonic() + int(timeout)
    async with websockets.connect(url) as websocket:
        pathlib.Path(ready_file).write_text("ready\n")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("matching firewall event was not received")
            raw = await asyncio.wait_for(websocket.recv(), remaining)
            event = json.loads(raw)
            if token not in str(event.get("detail", "")):
                continue
            if event.get("srcIp") != expected_src:
                raise RuntimeError("unexpected source: %s" % event.get("srcIp"))
            if event.get("dstIp") != expected_dst:
                raise RuntimeError("unexpected destination: %s" % event.get("dstIp"))
            if event.get("maskedSrcIp") != expected_mask:
                raise RuntimeError("unexpected mask: %s" % event.get("maskedSrcIp"))
            print(json.dumps(event, sort_keys=True))
            return

asyncio.run(receive())
' \
    "ws://127.0.0.1:${ARENA_FIREWALL_WS_PORT}" \
    "${TOKEN}" \
    "${READY_FILE}" \
    "${ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS}" \
    "${source_ip}" \
    "${destination_ip}" \
    "${expected_mask}" \
    >"${EVENT_FILE}" 2>"${EVENT_LOG}" &
SUBSCRIBER_PID=$!

wait_for_subscriber ||
    die "firewall WebSocket subscriber did not become ready"

observed_source="$(
    docker exec team1-vuln \
        curl -fsS \
        --max-time "${ARENA_FIREWALL_SMOKE_TIMEOUT_SECONDS}" \
        "http://${destination_ip}:${ARENA_FIREWALL_PROBE_PORT}/${TOKEN}"
)"
[[ "${observed_source}" == "${expected_mask}" ]] ||
    die "Team 2 observed ${observed_source:-<empty>} instead of masked source ${expected_mask}"

set +e
wait "${SUBSCRIBER_PID}"
subscriber_rc=$?
set -e
SUBSCRIBER_PID=""
((subscriber_rc == 0)) ||
    die "matching firewall WebSocket event was not emitted"
[[ -s "${EVENT_FILE}" ]] ||
    die "firewall subscriber returned no event"

after_packets="$(redirect_packets)"
[[ "${after_packets}" =~ ^[0-9]+$ ]] ||
    die "could not read the redirect counter after the request"
((after_packets > before_packets)) ||
    die "redirect counter did not increase (${before_packets} -> ${after_packets})"

echo "network smoke: ok"
echo "  route: ${source_ip} -> ${destination_ip}:${ARENA_FIREWALL_PROBE_PORT}"
echo "  destination observed: ${observed_source}"
echo "  redirect packets: ${before_packets} -> ${after_packets}"
echo "  event: $(<"${EVENT_FILE}")"
