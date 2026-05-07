"""Docker control surface — used by the dashboard's admin actions.

If the gameserver has access to the host's Docker socket it can pause/start
team containers (simulating a team taking their service offline). When the
socket is not available (e.g. running in a sandbox) the helpers degrade to
no-ops with descriptive errors.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .config import CONFIG

logger = logging.getLogger(__name__)


try:
    import docker
    from docker.errors import APIError, NotFound
except ImportError:  # pragma: no cover
    docker = None  # type: ignore[assignment]
    APIError = NotFound = Exception  # type: ignore[assignment]


@dataclass
class DockerStatus:
    available: bool
    detail: str


def _client():
    if docker is None:
        return None
    if not os.path.exists("/var/run/docker.sock"):
        return None
    try:
        return docker.from_env()
    except Exception:  # noqa: BLE001
        logger.warning("docker.from_env() failed", exc_info=True)
        return None


def docker_status() -> DockerStatus:
    cli = _client()
    if cli is None:
        return DockerStatus(False, "docker socket unavailable")
    try:
        cli.ping()
    except Exception as exc:  # noqa: BLE001
        return DockerStatus(False, f"ping failed: {exc}")
    return DockerStatus(True, "ok")


def container_state(name: str) -> str:
    cli = _client()
    if cli is None:
        return "unknown"
    try:
        container = cli.containers.get(name)
    except NotFound:
        return "missing"
    except APIError as exc:
        logger.warning("docker get %s failed: %s", name, exc)
        return "unknown"
    return container.status


def take_down(team_id: int) -> str:
    name = CONFIG.team_container_name(team_id, "vuln")
    cli = _client()
    if cli is None:
        return "docker socket unavailable"
    try:
        cli.containers.get(name).stop(timeout=2)
    except NotFound:
        return f"{name} not found"
    except APIError as exc:
        return f"stop failed: {exc}"
    return f"{name} stopped"


def bring_up(team_id: int) -> str:
    name = CONFIG.team_container_name(team_id, "vuln")
    cli = _client()
    if cli is None:
        return "docker socket unavailable"
    try:
        container = cli.containers.get(name)
    except NotFound:
        return f"{name} not found"
    except APIError as exc:
        return f"lookup failed: {exc}"
    try:
        container.start()
    except APIError as exc:
        return f"start failed: {exc}"
    return f"{name} started"


def restart(team_id: int) -> str:
    name = CONFIG.team_container_name(team_id, "vuln")
    cli = _client()
    if cli is None:
        return "docker socket unavailable"
    try:
        cli.containers.get(name).restart(timeout=2)
    except NotFound:
        return f"{name} not found"
    except APIError as exc:
        return f"restart failed: {exc}"
    return f"{name} restarted"
