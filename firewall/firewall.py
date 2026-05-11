#!/usr/bin/env python3
"""Sandcastle transparent firewall and activity feed.

The service installs an idempotent iptables REDIRECT rule for TCP traffic whose
source and destination are both inside the CTF subnet. Redirected connections
land on the local transparent proxy; the proxy recovers the original
destination with SO_ORIGINAL_DST, opens a new outbound connection, and emits
organizer-visible activity events over WebSocket.
"""

import asyncio
import contextlib
import ipaddress
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import time
import uuid

import websockets

# Configuration

CTF_NETWORK = ipaddress.ip_network(os.environ.get("CTF_NETWORK", "10.10.0.0/16"))
WS_PORT = int(os.environ.get("WS_PORT", "6789"))
PROXY_PORT = int(os.environ.get("PROXY_PORT", "15000"))
RULE_COMMENT = "sandcastle-firewall-transparent-proxy"
SO_ORIGINAL_DST = 80
BUFFER_SIZE = 64 * 1024
FIRST_PAYLOAD_TIMEOUT = 1.0
ETH_P_IP = 0x0800
ICMP_ECHO_REQUEST = 8

# Attack classifiers

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
    if payload.startswith(("GET ", "POST ", "PUT ", "PATCH ", "DELETE ", "HEAD ", "OPTIONS ")):
        return "http"
    if dport in (80, 443, 8080):
        return "http"
    return "tcp"


def _ip_to_name(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4 and parts[0] == "10" and parts[1] == "10":
        team, host = parts[2], parts[3]
        if host == "1":
            return "gateway"
        if host == "2":
            return f"team{team}-ssh"
        if host == "3":
            return f"team{team}-vuln"
    return ip


def _in_ctf_network(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in CTF_NETWORK
    except ValueError:
        return False


def _run_iptables(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["iptables", *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _rule_spec() -> list[str]:
    return [
        "-t",
        "nat",
        "-A",
        "PREROUTING",
        "-s",
        str(CTF_NETWORK),
        "-d",
        str(CTF_NETWORK),
        "-p",
        "tcp",
        "-m",
        "comment",
        "--comment",
        RULE_COMMENT,
        "-j",
        "REDIRECT",
        "--to-ports",
        str(PROXY_PORT),
    ]


def _delete_existing_rules() -> None:
    while True:
        proc = _run_iptables(
            [
                "-t",
                "nat",
                "-S",
                "PREROUTING",
            ],
            check=False,
        )
        if proc.returncode != 0:
            print(f"[firewall] WARNING: cannot inspect iptables: {proc.stderr.strip()}", flush=True)
            return

        matching = [line for line in proc.stdout.splitlines() if RULE_COMMENT in line]
        if not matching:
            return

        deleted = False
        for line in matching:
            delete_args = ["-t", "nat", *line.replace("-A", "-D", 1).split()]
            proc = _run_iptables(delete_args, check=False)
            if proc.returncode == 0:
                deleted = True
            else:
                print(f"[firewall] WARNING: could not delete rule {line!r}: {proc.stderr.strip()}", flush=True)

        if not deleted:
            return


def install_redirect_rule() -> None:
    _delete_existing_rules()
    _run_iptables(_rule_spec())
    print(
        f"[firewall] Redirecting TCP {CTF_NETWORK} -> {CTF_NETWORK} through local port {PROXY_PORT}",
        flush=True,
    )


def remove_redirect_rule() -> None:
    _delete_existing_rules()
    print("[firewall] Removed Sandcastle redirect rules", flush=True)


def _original_destination(sock: socket.socket) -> tuple[str, int]:
    raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port = struct.unpack_from("!H", raw, 2)[0]
    ip = socket.inet_ntoa(raw[4:8])
    return ip, port


def _payload_detail(payload: bytes) -> tuple[str, str]:
    text = payload.decode("utf-8", errors="replace")
    detail = text.split("\r\n")[0].strip()[:200]
    return text, detail


_event_queue: queue.Queue = queue.Queue()
_clients: set = set()


def _emit_event(
    *,
    src_ip: str,
    dst_ip: str,
    dst_port: int,
    masked_src_ip: str | None,
    first_payload: bytes,
) -> None:
    payload_text, detail = _payload_detail(first_payload)
    event = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "src": _ip_to_name(src_ip),
        "dst": _ip_to_name(dst_ip),
        "srcIp": src_ip,
        "dstIp": dst_ip,
        "maskedSrcIp": masked_src_ip,
        "type": _classify(payload_text, dst_port),
        "proto": "TCP",
        "port": dst_port,
        "detail": detail,
    }
    _event_queue.put(event)
    print(
        f"[event] {event['type']:<16} {event['src']} ({src_ip}) -> "
        f"{event['dst']} ({dst_ip}:{dst_port}) as {masked_src_ip or 'unknown'} {detail[:70]}",
        flush=True,
    )


def _emit_icmp_event(src_ip: str, dst_ip: str) -> None:
    event = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "src": _ip_to_name(src_ip),
        "dst": _ip_to_name(dst_ip),
        "srcIp": src_ip,
        "dstIp": dst_ip,
        "maskedSrcIp": None,
        "type": "icmp",
        "proto": "ICMP",
        "port": 0,
        "detail": "ICMP echo request",
    }
    _event_queue.put(event)
    print(f"[event] {'icmp':<16} {event['src']} ({src_ip}) -> {event['dst']} ({dst_ip}) echo request", flush=True)


async def _ws_handler(websocket) -> None:
    _clients.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        _clients.discard(websocket)


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


async def _icmp_sniff_loop(stop_event: asyncio.Event) -> None:
    if not hasattr(socket, "AF_PACKET"):
        print("[firewall] ICMP activity capture is not supported on this platform", flush=True)
        return

    try:
        raw_sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    except OSError as exc:
        print(f"[firewall] WARNING: ICMP activity capture disabled: {exc}", flush=True)
        return

    raw_sock.settimeout(0.5)
    print("[firewall] ICMP activity capture enabled", flush=True)

    try:
        while not stop_event.is_set():
            try:
                frame = await asyncio.to_thread(raw_sock.recv, BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                if not stop_event.is_set():
                    print(f"[firewall] WARNING: ICMP capture failed: {exc}", flush=True)
                break

            if len(frame) < 34:
                continue

            eth_type = struct.unpack_from("!H", frame, 12)[0]
            if eth_type != ETH_P_IP:
                continue

            ip_offset = 14
            protocol = frame[ip_offset + 9]
            if protocol != socket.IPPROTO_ICMP:
                continue

            ihl = (frame[ip_offset] & 0x0F) * 4
            icmp_offset = ip_offset + ihl
            if len(frame) <= icmp_offset or frame[icmp_offset] != ICMP_ECHO_REQUEST:
                continue

            src_ip = socket.inet_ntoa(frame[ip_offset + 12 : ip_offset + 16])
            dst_ip = socket.inet_ntoa(frame[ip_offset + 16 : ip_offset + 20])
            if _in_ctf_network(src_ip) and _in_ctf_network(dst_ip):
                _emit_icmp_event(src_ip, dst_ip)
    finally:
        raw_sock.close()


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError, OSError):
        raise
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    sock = writer.get_extra_info("socket")
    src_ip = peer[0] if peer else "unknown"

    if not sock:
        await _close_writer(writer)
        return

    try:
        dst_ip, dst_port = _original_destination(sock)
    except OSError as exc:
        print(f"[firewall] Could not recover original destination from {src_ip}: {exc}", flush=True)
        await _close_writer(writer)
        return

    if not (_in_ctf_network(src_ip) and _in_ctf_network(dst_ip)):
        await _close_writer(writer)
        return

    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(dst_ip, dst_port)
    except OSError as exc:
        print(f"[firewall] Upstream connect failed {src_ip} -> {dst_ip}:{dst_port}: {exc}", flush=True)
        await _close_writer(writer)
        return

    masked_src_ip = None
    upstream_sockname = upstream_writer.get_extra_info("sockname")
    if upstream_sockname:
        masked_src_ip = upstream_sockname[0]

    first_payload = b""
    try:
        first_payload = await asyncio.wait_for(reader.read(BUFFER_SIZE), timeout=FIRST_PAYLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        pass
    except OSError as exc:
        print(f"[firewall] Initial read failed from {src_ip}: {exc}", flush=True)

    if first_payload:
        _emit_event(
            src_ip=src_ip,
            dst_ip=dst_ip,
            dst_port=dst_port,
            masked_src_ip=masked_src_ip,
            first_payload=first_payload,
        )
        upstream_writer.write(first_payload)
        await upstream_writer.drain()
    else:
        _emit_event(
            src_ip=src_ip,
            dst_ip=dst_ip,
            dst_port=dst_port,
            masked_src_ip=masked_src_ip,
            first_payload=b"",
        )

    tasks = [
        asyncio.create_task(_pipe(reader, upstream_writer)),
        asyncio.create_task(_pipe(upstream_reader, writer)),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        with contextlib.suppress(Exception):
            task.result()
    await asyncio.gather(*pending, return_exceptions=True)
    await _close_writer(upstream_writer)
    await _close_writer(writer)


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    install_redirect_rule()

    proxy = await asyncio.start_server(_handle_client, "0.0.0.0", PROXY_PORT)
    ws_server = await websockets.serve(_ws_handler, "0.0.0.0", WS_PORT)
    broadcast_task = asyncio.create_task(_broadcast_loop())
    icmp_task = asyncio.create_task(_icmp_sniff_loop(stop_event))

    print(f"[firewall] Transparent proxy listening on 0.0.0.0:{PROXY_PORT}", flush=True)
    print(f"[firewall] WebSocket listening on ws://0.0.0.0:{WS_PORT}", flush=True)

    try:
        await stop_event.wait()
    finally:
        proxy.close()
        ws_server.close()
        broadcast_task.cancel()
        icmp_task.cancel()
        await proxy.wait_closed()
        await ws_server.wait_closed()
        await asyncio.gather(broadcast_task, icmp_task, return_exceptions=True)
        remove_redirect_rule()


if __name__ == "__main__":
    asyncio.run(main())
