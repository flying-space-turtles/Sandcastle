#!/usr/bin/env python3
"""Sandcastle network monitor.

Sniffs all TCP traffic on the ctf-network bridge (10.10.0.0/16), resolves IPs
to container names, classifies events by attack type, and broadcasts JSON over
WebSocket on port 6789.

Requires:
  network_mode: host
  cap_add: [NET_ADMIN, NET_RAW]
"""

import asyncio
import ipaddress
import json
import queue
import re
import time
import uuid

import websockets
from scapy.all import AsyncSniffer, IP, TCP, Raw, get_if_addr, get_if_list

# ── Configuration ─────────────────────────────────────────────────────────────

CTF_NETWORK = ipaddress.ip_network("10.10.0.0/16")
WS_PORT = 6789

# Only capture traffic to/from the vulnerable service port.
# Change this list if your service runs on a different port.
CAPTURE_PORTS: set[int] = {8080, 80, 443}

# ── Interface auto-detection ──────────────────────────────────────────────────


def _find_bridge_iface() -> str:
    """Return the interface that carries the ctf-network subnet.

    When the monitor runs with network_mode:host this is the Docker bridge
    (e.g. br-xxxxx).  When it sits directly on ctf-network it is typically
    eth0.  Either way, we look for the first interface whose address falls
    inside 10.10.0.0/16.
    """
    for iface in get_if_list():
        try:
            addr = get_if_addr(iface)
            if ipaddress.ip_address(addr) in CTF_NETWORK:
                print(f"[monitor] Using interface {iface!r} ({addr})", flush=True)
                return iface
        except Exception:
            pass
    print("[monitor] WARNING: bridge interface not found, sniffing all interfaces", flush=True)
    return "any"


# ── IP → container name ───────────────────────────────────────────────────────
# IP layout:  10.10.0.2 = firewall  10.10.0.3 = monitor
#             10.10.N.1 = gateway   10.10.N.2 = teamN-ssh   10.10.N.3 = teamN-vuln


def _ip_to_name(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4 and parts[0] == "10" and parts[1] == "10":
        team, host = parts[2], parts[3]
        if team == "0" and host == "2":
            return "firewall"
        if team == "0" and host == "3":
            return "monitor"
        if host == "1":
            return "gateway"
        if host == "2":
            return f"team{team}-ssh"
        if host == "3":
            return f"team{team}-vuln"
    return ip


def _team_id(name: str) -> str | None:
    """Extract team number from a resolved container name, or None."""
    if name.startswith("team"):
        return name.split("-")[0]  # "team1-ssh" → "team1"
    return None


# ── Attack classifiers ────────────────────────────────────────────────────────

_SQLI = re.compile(
    r"union\s+select|'\s*or\s*'|or\s+1\s*=\s*1|drop\s+table|--\s|\bselect\b.+\bfrom\b",
    re.IGNORECASE,
)
_CMDI = re.compile(
    r";\s*(ls|id|whoami|cat|wget|curl|bash|sh|python|perl|nc)\b|&&\s*\w|\|\s*(id|ls|whoami|cat)",
    re.IGNORECASE,
)
_TRAV = re.compile(r"(\.\./|%2e%2e|\.\.%2f|%2f\.\.)", re.IGNORECASE)


def _classify(payload: str, dport: int) -> str:
    if dport == 22:
        return "ssh"
    if _SQLI.search(payload):
        return "sqli"
    if _CMDI.search(payload):
        return "cmdi"
    if _TRAV.search(payload):
        return "path-traversal"
    if dport in (80, 443, 8080):
        return "http"
    return "tcp"


# ── Thread-safe event queue ───────────────────────────────────────────────────

_event_queue: queue.Queue = queue.Queue()

# ── Connected WebSocket clients ───────────────────────────────────────────────

_clients: set = set()

# ── Packet deduplication ──────────────────────────────────────────────────────
# With firewall routing, the bridge sees each packet twice.  A small cache
# keyed on (src, dst, sport, dport, payload_hash) with a time window filters
# the duplicates.

_DEDUP_WINDOW: float = 0.5   # seconds
_DEDUP_MAX_SIZE: int = 4096
_seen_packets: dict[tuple, float] = {}


# ── Packet handler (runs in sniffer thread) ───────────────────────────────────


def _on_packet(pkt) -> None:
    if not (pkt.haslayer(IP) and pkt.haslayer(TCP)):
        return

    src_ip: str = pkt[IP].src
    dst_ip: str = pkt[IP].dst

    try:
        src_in = ipaddress.ip_address(src_ip) in CTF_NETWORK
        dst_in = ipaddress.ip_address(dst_ip) in CTF_NETWORK
    except ValueError:
        return

    if not src_in and not dst_in:
        return

    dport: int = pkt[TCP].dport
    sport: int = pkt[TCP].sport

    # Only track ports we care about (attacker → vuln app)
    if dport not in CAPTURE_PORTS and sport not in CAPTURE_PORTS:
        return

    # Must have actual application payload — drop bare SYN/ACK/FIN handshakes
    if not pkt.haslayer(Raw):
        return

    payload = ""
    try:
        payload = pkt[Raw].load.decode("utf-8", errors="replace")
    except Exception:
        pass

    if not payload.strip():
        return

    src_name = _ip_to_name(src_ip)
    dst_name = _ip_to_name(dst_ip)

    # Drop gateway, firewall and monitor traffic — it's routing noise, not attacks
    if src_name in ("gateway", "firewall", "monitor") or dst_name in ("gateway", "firewall", "monitor"):
        return

    # Skip packets where neither endpoint maps to a known container name
    if src_name == src_ip and dst_name == dst_ip:
        return

    # Skip same-team traffic (ssh↔vuln within the same team) — not an attack
    if _team_id(src_name) is not None and _team_id(src_name) == _team_id(dst_name):
        return

    # ── Deduplication ─────────────────────────────────────────────────────
    # The firewall routes all traffic, so the bridge sees each packet twice
    # (sender→firewall and firewall→receiver).  Deduplicate using a hash of
    # the packet's key fields within a short time window.
    pkt_key = (src_ip, dst_ip, sport, dport, hash(payload[:64]))
    now = time.monotonic()
    if pkt_key in _seen_packets and (now - _seen_packets[pkt_key]) < _DEDUP_WINDOW:
        return
    _seen_packets[pkt_key] = now

    # Periodically prune old entries to avoid unbounded growth
    if len(_seen_packets) > _DEDUP_MAX_SIZE:
        cutoff = now - _DEDUP_WINDOW
        stale = [k for k, t in _seen_packets.items() if t < cutoff]
        for k in stale:
            del _seen_packets[k]

    etype = _classify(payload, dport)
    # First non-empty line of the payload (HTTP request line or data)
    detail = payload.split("\r\n")[0].strip()[:200]

    event = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "src": src_name,
        "dst": dst_name,
        "srcIp": src_ip,
        "dstIp": dst_ip,
        "type": etype,
        "proto": "TCP",
        "port": dport,
        "detail": detail,
    }

    _event_queue.put(event)
    print(
        f"[event] {etype:<16}  {src_name} → {dst_name}  {detail[:70]}",
        flush=True,
    )


# ── WebSocket handler ─────────────────────────────────────────────────────────


async def _ws_handler(websocket) -> None:
    _clients.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        _clients.discard(websocket)


# ── Broadcast loop (drains queue → all WS clients) ────────────────────────────


async def _broadcast_loop() -> None:
    while True:
        try:
            event = _event_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue

        msg = json.dumps(event)
        dead: set = set()
        for ws in list(_clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        _clients.difference_update(dead)


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    iface = _find_bridge_iface()
    port_list = " or ".join(f"port {p}" for p in sorted(CAPTURE_PORTS))
    bpf_filter = f"tcp and net 10.10.0.0/16 and ({port_list})"

    sniffer = AsyncSniffer(
        iface=iface,
        filter=bpf_filter,
        prn=_on_packet,
        store=False,
    )
    sniffer.start()
    print(f"[monitor] Sniffing {iface!r} with filter {bpf_filter!r}", flush=True)

    async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
        print(f"[monitor] WebSocket listening on ws://0.0.0.0:{WS_PORT}", flush=True)
        await _broadcast_loop()


if __name__ == "__main__":
    asyncio.run(main())
