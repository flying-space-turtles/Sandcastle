"""Plant flags inside team services and check that the data is intact.

The gameserver acts as a *legitimate user* of each team's notes service:

    1. Register a per-round bot account ("checker_round_<N>")
    2. POST a note whose body contains the flag string
    3. Remember (note_id, flag) so the SLA checker can verify it later

This is the "Service API" planting strategy described in
LOCAL_AD_CTF_SIMULATION.md §6.3 (Method 3).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

import httpx

from .config import CONFIG

logger = logging.getLogger(__name__)


FLAG_HEX_LEN = 32  # FLAG{[a-f0-9]{32}}


def generate_flag() -> str:
    return f"FLAG{{{secrets.token_hex(FLAG_HEX_LEN // 2)}}}"


@dataclass
class PlantResult:
    flag: str
    note_id: int | None
    success: bool
    detail: str


async def plant_flag(client: httpx.AsyncClient, team_id: int, round_no: int) -> PlantResult:
    """Plant a flag in team `team_id`'s notes service. Returns the result.

    On error (service down, bad response) the result still carries the
    generated flag so the caller can persist it as planted-but-unreachable.
    """

    flag = generate_flag()
    url = CONFIG.team_service_url(team_id)
    bot_username = f"checker-r{round_no}-{secrets.token_hex(2)}"

    try:
        reg = await client.post(
            f"{url}/api/register",
            json={"username": bot_username},
            timeout=CONFIG.sla_timeout,
        )
        if reg.status_code != 201:
            return PlantResult(flag, None, False, f"register status {reg.status_code}")
        token = reg.json().get("token")
        if not token:
            return PlantResult(flag, None, False, "register missing token")

        note = await client.post(
            f"{url}/api/notes",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": f"round {round_no} secret", "content": flag},
            timeout=CONFIG.sla_timeout,
        )
        if note.status_code != 201:
            return PlantResult(flag, None, False, f"create-note status {note.status_code}")
        note_id = note.json().get("id")
        if not isinstance(note_id, int):
            return PlantResult(flag, None, False, "create-note missing id")

        return PlantResult(flag, note_id, True, "ok")
    except httpx.RequestError as exc:
        return PlantResult(flag, None, False, f"network: {exc}")
    except Exception as exc:  # noqa: BLE001 — best-effort planting
        logger.exception("plant_flag failed for team %d", team_id)
        return PlantResult(flag, None, False, f"unexpected: {exc}")
