from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArenaDefaults:
    team_count: int
    ctf_subnet: str
    network_prefix: str
    ssh_base_port: int
    service_port: int
    service_ip_pattern: str
    bot_api_host: str
    bot_api_port: int
    bot_loop_seconds: int


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    configured = os.environ.get("ARENA_CONFIG_FILE")
    if configured:
        paths.append(Path(configured))
    paths.append(Path(__file__).resolve().parents[2] / "config" / "arena.env")
    paths.append(Path("/tmp/arena.env"))
    return paths


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid arena config line: {raw_line!r}")
        values[key] = value
    return values


def _required_int(values: dict[str, str], key: str, minimum: int, maximum: int) -> int:
    raw = values.get(key, "")
    if not raw.isdigit():
        raise ValueError(f"{key} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def load_arena_defaults(path: Path | None = None) -> ArenaDefaults:
    config_path = path
    if config_path is None:
        config_path = next((candidate for candidate in _candidate_paths() if candidate.is_file()), None)
    if config_path is None:
        raise RuntimeError("arena configuration not found")

    values = _parse_env(config_path)
    team_count = _required_int(values, "ARENA_TEAM_COUNT", 1, 250)
    ssh_base_port = _required_int(values, "ARENA_SSH_BASE_PORT", 1024, 65285)
    service_port = _required_int(values, "ARENA_SERVICE_PORT", 1, 65535)
    bot_api_port = _required_int(values, "ARENA_BOT_API_PORT", 1, 65535)
    bot_loop_seconds = _required_int(values, "ARENA_BOT_LOOP_SECONDS", 0, 86400)
    if ssh_base_port + team_count > 65535:
        raise ValueError("ARENA_SSH_BASE_PORT + ARENA_TEAM_COUNT must not exceed 65535")

    subnet = values.get("ARENA_CTF_SUBNET", "")
    match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.0\.0/16", subnet)
    if not match or any(int(octet) > 255 for octet in match.groups()):
        raise ValueError("ARENA_CTF_SUBNET must use the supported A.B.0.0/16 form")
    network_prefix = f"{int(match.group(1))}.{int(match.group(2))}"

    bot_api_host = values.get("ARENA_BOT_API_HOST", "")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", bot_api_host):
        raise ValueError("ARENA_BOT_API_HOST contains unsupported characters")

    return ArenaDefaults(
        team_count=team_count,
        ctf_subnet=subnet,
        network_prefix=network_prefix,
        ssh_base_port=ssh_base_port,
        service_port=service_port,
        service_ip_pattern=f"{network_prefix}.{{team}}.3",
        bot_api_host=bot_api_host,
        bot_api_port=bot_api_port,
        bot_loop_seconds=bot_loop_seconds,
    )


ARENA_DEFAULTS = load_arena_defaults()
