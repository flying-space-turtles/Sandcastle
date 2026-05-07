"""Runtime configuration for the gameserver.

All values are read from environment variables so that ``docker compose``
can configure the simulation without rebuilding the image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    num_teams: int = _int("NUM_TEAMS", 3)
    tick_duration: int = _int("TICK_DURATION", 30)
    flag_expiry_rounds: int = _int("FLAG_EXPIRY_ROUNDS", 5)
    sla_timeout: float = float(os.environ.get("SLA_TIMEOUT", "3.0"))
    db_path: str = os.environ.get("GAMESERVER_DB", "/app/data/gameserver.sqlite")
    service_port: int = _int("SERVICE_PORT", 8080)
    notes_admin_token: str = os.environ.get("NOTES_ADMIN_TOKEN", "change-me")
    auto_start: bool = os.environ.get("AUTO_START", "true").lower() in {"1", "true", "yes"}

    def team_subnet_ip(self, team_id: int, suffix: int = 3) -> str:
        # Mirrors the IP scheme described in LOCAL_AD_CTF_SIMULATION.md
        # team N's vuln service lives at 10.10.N.3
        return f"10.10.{team_id}.{suffix}"

    def team_service_url(self, team_id: int) -> str:
        return f"http://{self.team_subnet_ip(team_id)}:{self.service_port}"

    def team_container_name(self, team_id: int, kind: str) -> str:
        return f"team{team_id}-{kind}"


CONFIG = Config()
