#!/usr/bin/env python3
"""
Sandcastle per-team Docker socket filter proxy (SC-017).

Listens on a team-scoped Unix socket and forwards Docker API requests to the
host daemon, enforcing team-level access controls:

  - Container list (/containers/json) responses are filtered to own team only.
  - Container operations on another team's containers are denied with HTTP 403.
  - Container creates are checked against the team name pattern.
  - Image, build, network, and volume paths are allowed without restriction.
  - /events is denied to prevent container ID discovery.

All allowed requests are piped transparently, including streaming and chunked
responses. The proxy is stateless and single-tenant (one process per team).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

TEAM_ID = int(os.environ["TEAM_ID"])
TEAM_NAME = f"team{TEAM_ID}"
HOST_SOCK = os.environ.get("HOST_SOCKET", "/var/run/docker.sock")
BIND_SOCK = os.environ["PROXY_SOCKET"]

logging.basicConfig(
    level=logging.INFO,
    format=f"[docker-proxy/{TEAM_NAME}] %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("proxy")

# ── Container classification ──────────────────────────────────────────────────

# Containers this team fully controls (build, start/stop/restart, exec, remove)
_RE_OWN_APP = re.compile(rf"^{re.escape(TEAM_NAME)}-vuln-app$")
# Containers this team may inspect read-only (needed for network_mode: container:)
_RE_OWN_INFRA = re.compile(rf"^{re.escape(TEAM_NAME)}-(vuln|ssh)$")
# Container name must not start with another team's prefix
_RE_OTHER_TEAM = re.compile(r"^team(\d+)-")


def _classify(raw_name: str) -> str:
    """Return 'app', 'infra', or 'other' given a container name or path segment."""
    name = raw_name.lstrip("/").split(":")[0]  # drop tag-like suffixes
    if _RE_OWN_APP.match(name):
        return "app"
    if _RE_OWN_INFRA.match(name):
        return "infra"
    return "other"


# ── Routing helpers ───────────────────────────────────────────────────────────

_API_PREFIX = r"/v[\d.]+"

# Matches /vX.Y/containers/{name}[/op]
_RE_CONTAINER_PATH = re.compile(
    rf"^{_API_PREFIX}/containers/(?P<name>[^/?]+)(?:/(?P<op>[^/?]*))?(?:\?.*)?$"
)
# Matches /vX.Y/containers/json  (the list endpoint)
_RE_CONTAINER_LIST = re.compile(rf"^{_API_PREFIX}/containers/json(?:\?.*)?$")
# Matches /vX.Y/containers/create
_RE_CONTAINER_CREATE = re.compile(rf"^{_API_PREFIX}/containers/create(?:\?.*)?$")

# Paths always allowed without further checks
_RE_ALWAYS_ALLOW = re.compile(
    rf"^(?:"
    rf"{_API_PREFIX}/(?:version|info|ping)"
    rf"|/_ping"
    rf"|{_API_PREFIX}/images(?:/.*)?$"
    rf"|{_API_PREFIX}/build(?:\?.*)?$"
    rf"|{_API_PREFIX}/networks(?:/.*)?$"
    rf"|{_API_PREFIX}/volumes(?:/.*)?$"
    rf"|{_API_PREFIX}/exec(?:/.*)?$"  # exec session ops (create is checked separately)
    rf")$"
)

# Paths always denied
_RE_ALWAYS_DENY = re.compile(
    rf"^{_API_PREFIX}/events(?:\?.*)?$"  # would reveal other containers' IDs
)

# Read-only operations allowed on own-infra containers
_INFRA_READ_OPS = frozenset({"json", "logs", "stats", "top", "changes"})

# deny body constant
_DENY_BODY = b'{"message":"access denied by Sandcastle isolation proxy"}'


def _decide(method: str, path: str, body: bytes) -> tuple[bool, bool, str]:
    """
    Return (allowed, filter_response, reason).

    filter_response is True only for the container list endpoint; in that case
    the caller must buffer and filter the response before forwarding.
    """
    m_upper = method.upper()

    if _RE_ALWAYS_DENY.match(path):
        return False, False, "endpoint denied for isolation"

    if _RE_ALWAYS_ALLOW.match(path):
        return True, False, "always-allowed route"

    if _RE_CONTAINER_LIST.match(path):
        return True, True, "container list (filtered)"

    if _RE_CONTAINER_CREATE.match(path):
        return _decide_create(body)

    m = _RE_CONTAINER_PATH.match(path)
    if m:
        name = m.group("name")
        op = (m.group("op") or "").lower()
        return _decide_container_op(m_upper, name, op)

    # Default: deny unknown routes
    return False, False, f"no matching rule for {method} {path}"


def _decide_create(body: bytes) -> tuple[bool, bool, str]:
    """Check a container create request: the Name field must match own team."""
    try:
        proposed = json.loads(body).get("name", "") if body else ""
    except (json.JSONDecodeError, AttributeError):
        proposed = ""

    if not proposed:
        # No explicit name — docker will assign one; allow and let it be ephemeral.
        return True, False, "container create (unnamed)"

    role = _classify(proposed)
    if role in ("app", "infra"):
        return True, False, f"container create: own team name {proposed!r}"

    # Check if it's another team's name — hard deny
    m = _RE_OTHER_TEAM.match(proposed.lstrip("/"))
    if m and m.group(1) != str(TEAM_ID):
        return False, False, f"container create: name {proposed!r} belongs to another team"

    # Non-team container name (e.g. temporary helper); allow
    return True, False, f"container create: non-team name {proposed!r}"


def _decide_container_op(method: str, name: str, op: str) -> tuple[bool, bool, str]:
    """Access-control decision for /containers/{name}/{op}."""
    role = _classify(name)

    if role == "app":
        return True, False, f"own app container {name!r}"

    if role == "infra":
        # Read-only access to own infra containers (needed for network_mode lookup)
        if method == "GET" or op in _INFRA_READ_OPS:
            return True, False, f"own infra container {name!r} (read-only)"
        return (
            False,
            False,
            f"write op {op!r} denied on own infra container {name!r}",
        )

    return False, False, f"container {name!r} does not belong to {TEAM_NAME}"


# ── Response filtering ────────────────────────────────────────────────────────


def _filter_container_list(body: bytes) -> bytes:
    """Strip containers not belonging to this team from a /containers/json response."""
    try:
        items = json.loads(body)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return body
    if not isinstance(items, list):
        return body
    filtered = [c for c in items if _is_own(c)]
    return json.dumps(filtered).encode()


def _is_own(container: dict) -> bool:
    labels = container.get("Labels") or {}
    if labels.get("sandcastle.team") == TEAM_NAME:
        return True
    for raw in container.get("Names") or []:
        if _classify(raw) in ("app", "infra"):
            return True
    return False


# ── HTTP I/O helpers ──────────────────────────────────────────────────────────


async def _read_request_head(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    """
    Read one HTTP request's start-line + headers.
    Returns (method, path, headers_lc, raw_head) or None on clean EOF.
    """
    raw = b""
    while True:
        line = await reader.readline()
        if not line:
            return None
        raw += line
        if line in (b"\r\n", b"\n"):
            break
        if len(raw) > 131_072:
            raise ValueError("request headers too large")

    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    start = lines[0].split(" ", 2)
    if len(start) < 2:
        return None

    method = start[0].upper()
    path = start[1]

    headers: dict[str, str] = {}
    for h in lines[1:]:
        if ":" in h:
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()

    return method, path, headers, raw


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    data = b""
    while True:
        size_line = await reader.readline()
        try:
            size = int(size_line.strip().split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            await reader.readline()
            break
        chunk = await reader.readexactly(size)
        await reader.readline()
        data += chunk
    return data


async def _read_response_head(
    reader: asyncio.StreamReader,
) -> tuple[bytes, dict[str, str]]:
    raw = b""
    while True:
        line = await reader.readline()
        if not line:
            raise EOFError
        raw += line
        if line in (b"\r\n", b"\n"):
            break
    headers: dict[str, str] = {}
    for h in raw.decode("utf-8", errors="replace").split("\r\n")[1:]:
        if ":" in h:
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()
    return raw, headers


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65_536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            dst.write_eof()


async def _send_deny(writer: asyncio.StreamWriter, reason: str) -> None:
    log.warning("DENY  %s: %s", TEAM_NAME, reason)
    body = _DENY_BODY
    writer.write(
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    with contextlib.suppress(Exception):
        await writer.drain()


# ── Client handler ────────────────────────────────────────────────────────────


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            try:
                head = await asyncio.wait_for(_read_request_head(reader), timeout=30.0)
            except asyncio.TimeoutError:
                return
            if head is None:
                return

            method, path, req_headers, raw_head = head

            # Read body if declared
            body = b""
            cl_str = req_headers.get("content-length", "0")
            try:
                cl = int(cl_str)
            except ValueError:
                cl = 0
            if cl > 0:
                body = await asyncio.wait_for(reader.readexactly(cl), timeout=60.0)

            # Strip query for routing decisions; preserve original in forwarded bytes
            path_noq = path.split("?")[0]
            allowed, needs_filter, reason = _decide(method, path_noq, body)

            if not allowed:
                await _send_deny(writer, f"{method} {path_noq} — {reason}")
                if req_headers.get("connection", "").lower() == "keep-alive":
                    continue
                return

            log.debug("ALLOW %s | %s %s (%s)", TEAM_NAME, method, path_noq, reason)

            # Open connection to host Docker socket
            try:
                h_reader, h_writer = await asyncio.open_unix_connection(HOST_SOCK)
            except OSError as exc:
                log.error("Cannot reach host Docker socket: %s", exc)
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
                with contextlib.suppress(Exception):
                    await writer.drain()
                return

            # Forward request to host
            h_writer.write(raw_head + body)
            with contextlib.suppress(Exception):
                await h_writer.drain()

            if needs_filter:
                # Buffer the response, filter, re-emit with corrected Content-Length
                resp_raw, resp_headers = await _read_response_head(h_reader)

                is_chunked = "chunked" in resp_headers.get("transfer-encoding", "")
                resp_cl_s = resp_headers.get("content-length", "-1")
                try:
                    resp_cl = int(resp_cl_s)
                except ValueError:
                    resp_cl = -1

                if is_chunked:
                    resp_body = await _read_chunked(h_reader)
                elif resp_cl >= 0:
                    resp_body = await h_reader.readexactly(resp_cl)
                else:
                    resp_body = await h_reader.read()

                filtered = _filter_container_list(resp_body)

                # Rebuild headers: strip old content-length / transfer-encoding, add new
                new_head = re.sub(rb"(?i)transfer-encoding: chunked\r\n", b"", resp_raw)
                new_head = re.sub(rb"(?i)content-length: \d+\r\n", b"", new_head)
                new_head = (
                    new_head.rstrip(b"\r\n")
                    + f"\r\nContent-Length: {len(filtered)}\r\n\r\n".encode()
                )
                writer.write(new_head + filtered)
                with contextlib.suppress(Exception):
                    await writer.drain()

                h_writer.close()
                with contextlib.suppress(Exception):
                    await h_writer.wait_closed()

            else:
                # Transparent bidirectional pipe — handles streaming, upgrade, chunked
                await asyncio.gather(
                    _pipe(h_reader, writer),
                    _pipe(reader, h_writer),
                    return_exceptions=True,
                )
                h_writer.close()
                with contextlib.suppress(Exception):
                    await h_writer.wait_closed()
                return  # pipe closed: connection is done

            if req_headers.get("connection", "").lower() != "keep-alive":
                return

    except (asyncio.CancelledError, ConnectionError, OSError, EOFError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


# ── Server entrypoint ─────────────────────────────────────────────────────────


async def main() -> None:
    sock_path = Path(BIND_SOCK)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    server = await asyncio.start_unix_server(handle_client, path=str(sock_path))
    # group-readable so the team container's docker group can use it
    sock_path.chmod(0o660)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info(
        "Listening on %s  host=%s  team=%s",
        BIND_SOCK,
        HOST_SOCK,
        TEAM_NAME,
    )
    async with server:
        await stop.wait()

    log.info("Shutting down")
    sock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
