"""Bootstrap the team registry from configuration."""

from __future__ import annotations

import logging

from .config import CONFIG
from .db import Database

logger = logging.getLogger(__name__)


def ensure_teams(db: Database) -> None:
    existing = {t["id"] for t in db.list_teams()}
    for i in range(1, CONFIG.num_teams + 1):
        if i in existing:
            continue
        db.upsert_team(
            team_id=i,
            name=f"Team {i}",
            ip_address=CONFIG.team_subnet_ip(i),
        )
        logger.info(
            "registered Team %d at %s", i, CONFIG.team_subnet_ip(i)
        )
