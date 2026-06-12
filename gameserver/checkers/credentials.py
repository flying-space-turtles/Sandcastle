from __future__ import annotations

import hashlib
import hmac
import re

from checkers.contract import CheckerCredentials


def _derive(master_secret: str, purpose: str, team_id: int, service_name: str) -> str:
    if not master_secret:
        raise ValueError("checker master secret must be non-empty")
    scope = f"sandcastle-checker-v1:{purpose}:{team_id}:{service_name}"
    return hmac.new(
        master_secret.encode("utf-8"),
        scope.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def derive_checker_credentials(
    master_secret: str,
    team_id: int,
    service_name: str,
) -> CheckerCredentials:
    if team_id <= 0:
        raise ValueError("team_id must be positive")
    if not service_name:
        raise ValueError("service_name must be non-empty")

    slug = re.sub(r"[^a-zA-Z0-9_]", "_", service_name).strip("_") or "service"
    username = f"checker_t{team_id}_{slug}"[:64]
    return CheckerCredentials(
        team_id=team_id,
        service_name=service_name,
        values={
            "username": username,
            "password": _derive(master_secret, "password", team_id, service_name),
            "plant_token": _derive(master_secret, "plant", team_id, service_name),
        },
    )
