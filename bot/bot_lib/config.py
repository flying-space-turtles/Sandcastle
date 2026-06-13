from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .arena import ARENA_DEFAULTS

CONFIG_FILE = "/tmp/bot_config.json"

LEGACY_EXPLOIT_ACTIONS = {
    "path_traversal": "exploit.path_traversal",
    "cmdi": "exploit.cmdi",
    "sqli": "exploit.sqli",
}


@dataclass
class BotConfig:
    bot_name: str = "Scripted Attacker"
    planner: str = "scripted"
    target_policy: str = "all_opponents"
    target_teams: list[int] = field(default_factory=list)
    actions: list[str] = field(default_factory=lambda: ["recon.health"])
    service_port: int = field(
        default_factory=lambda: int(os.environ.get("SERVICE_PORT", str(ARENA_DEFAULTS.service_port)))
    )
    flag_re: str = field(default_factory=lambda: os.environ.get("FLAG_RE", r"FLAG\{[a-f0-9]{32}\}"))
    ip_pattern: str = field(
        default_factory=lambda: os.environ.get("IP_PATTERN", ARENA_DEFAULTS.service_ip_pattern)
    )
    stop_on_success: bool = True
    timeout: int = 6
    deployment_id: str = ""
    gameserver_url: str = ""
    submission_token: str = ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int_list(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]

    teams: list[int] = []
    for item in raw_items:
        text = str(item).strip()
        if text.isdigit():
            teams.append(int(text))
    return teams


def normalize_action_id(action_id: str) -> str:
    action_id = action_id.strip()
    return LEGACY_EXPLOIT_ACTIONS.get(action_id, action_id)


def _as_action_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_actions = value.split(",")
    elif isinstance(value, list):
        raw_actions = value
    else:
        raw_actions = [value]
    return [normalize_action_id(str(item)) for item in raw_actions if str(item).strip()]


def merge_config(config: BotConfig, data: dict[str, Any]) -> BotConfig:
    """Merge JSON or CLI data into a BotConfig.

    The merge accepts both the new action names and the older exploit-centric
    keys used by the first bot UI (`exploits`, `stop_on_first`).
    """
    if not data:
        return config

    updates: dict[str, Any] = {}
    scalar_keys = {
        "bot_name": str,
        "planner": str,
        "target_policy": str,
        "flag_re": str,
        "ip_pattern": str,
        "service_port": int,
        "timeout": int,
        "deployment_id": str,
        "gameserver_url": str,
        "submission_token": str,
    }
    for key, caster in scalar_keys.items():
        if key in data and data[key] is not None:
            updates[key] = caster(data[key])

    if "actions" in data:
        updates["actions"] = _as_action_list(data["actions"])
    elif "exploits" in data:
        updates["actions"] = _as_action_list(data["exploits"])

    if "target_teams" in data:
        updates["target_teams"] = _as_int_list(data["target_teams"])

    if "stop_on_success" in data:
        updates["stop_on_success"] = _as_bool(data["stop_on_success"])
    elif "stop_on_first" in data:
        updates["stop_on_success"] = _as_bool(data["stop_on_first"])

    return replace(config, **updates)


def load_config_file(path: str | None = None) -> BotConfig:
    path = path or os.environ.get("BOT_CONFIG_FILE", CONFIG_FILE)
    config = BotConfig()
    file_path = Path(path)
    if not file_path.exists():
        return config

    try:
        raw = json.loads(file_path.read_text())
    except Exception as exc:
        print(f"[!] Could not load {path}: {exc}")
        return config

    if not isinstance(raw, dict):
        print(f"[!] Ignoring {path}: expected a JSON object")
        return config

    return merge_config(config, raw)
