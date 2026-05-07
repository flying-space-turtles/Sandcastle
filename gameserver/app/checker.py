"""SLA checker for the notes service.

Returns one of UP / DOWN / MUMBLE / CORRUPT per round per team.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

import httpx

from .config import CONFIG

logger = logging.getLogger(__name__)


@dataclass
class SLAResult:
    status: str  # UP | DOWN | MUMBLE | CORRUPT
    detail: str


async def check_team_service(
    client: httpx.AsyncClient,
    team_id: int,
    expected_flag: str | None,
    expected_note_id: int | None,
) -> SLAResult:
    base_url = CONFIG.team_service_url(team_id)

    try:
        # 1. Reachability
        root = await client.get(f"{base_url}/", timeout=CONFIG.sla_timeout)
        if root.status_code != 200:
            return SLAResult("MUMBLE", f"banner status {root.status_code}")

        # 2. Core functionality: register + create + read a fresh note
        bot = f"sla-{secrets.token_hex(3)}"
        reg = await client.post(
            f"{base_url}/api/register",
            json={"username": bot},
            timeout=CONFIG.sla_timeout,
        )
        if reg.status_code != 201:
            return SLAResult("MUMBLE", f"register status {reg.status_code}")
        token = reg.json().get("token")
        if not token:
            return SLAResult("MUMBLE", "register missing token")

        canary = secrets.token_hex(8)
        note = await client.post(
            f"{base_url}/api/notes",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "sla", "content": f"sla-canary-{canary}"},
            timeout=CONFIG.sla_timeout,
        )
        if note.status_code != 201:
            return SLAResult("MUMBLE", f"create-note status {note.status_code}")
        note_id = note.json().get("id")
        if not isinstance(note_id, int):
            return SLAResult("MUMBLE", "create-note missing id")

        readback = await client.get(
            f"{base_url}/api/note/{note_id}", timeout=CONFIG.sla_timeout
        )
        if readback.status_code != 200:
            return SLAResult("MUMBLE", f"readback status {readback.status_code}")
        if canary not in readback.text:
            return SLAResult("MUMBLE", "readback missing canary")

        # 3. Flag integrity from previous plant — only if we actually planted
        if expected_flag is not None and expected_note_id is not None:
            flag_check = await client.get(
                f"{base_url}/api/note/{expected_note_id}",
                timeout=CONFIG.sla_timeout,
            )
            if flag_check.status_code != 200:
                return SLAResult(
                    "CORRUPT",
                    f"planted note {expected_note_id} status {flag_check.status_code}",
                )
            if expected_flag not in flag_check.text:
                return SLAResult("CORRUPT", "planted flag missing from note")

        return SLAResult("UP", "all checks passed")

    except httpx.ConnectError as exc:
        return SLAResult("DOWN", f"connection refused: {exc}")
    except httpx.ReadTimeout:
        return SLAResult("DOWN", "read timeout")
    except httpx.RequestError as exc:
        return SLAResult("DOWN", f"network error: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("SLA check failed for team %d", team_id)
        return SLAResult("MUMBLE", f"unexpected: {exc}")
