"""Container health assessment for SC-018 resource limit monitoring.

Parses Docker inspect output to detect OOM kills and restart loops.
All functions are pure Python — no subprocess calls — so they can be tested
without Docker or a running arena.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

OOM_KILL_EVENT = "resource.oom_kill"
RESTART_LOOP_EVENT = "resource.restart_loop"
DISK_PRESSURE_EVENT = "resource.disk_pressure"

# A container that has restarted at least this many times is flagged as a loop.
RESTART_LOOP_THRESHOLD = 3
# Filesystem usage at or above this percentage triggers a disk-pressure violation.
DISK_PRESSURE_THRESHOLD_PCT = 80


@dataclass(frozen=True)
class ContainerStatus:
    name: str
    running: bool
    restart_count: int
    oom_killed: bool
    exit_code: int | None = None
    mem_limit_bytes: int | None = None
    disk_used_pct: int | None = None
    disk_available_bytes: int | None = None

    @property
    def is_restart_loop(self) -> bool:
        return self.restart_count >= RESTART_LOOP_THRESHOLD

    @property
    def health_label(self) -> str:
        if self.oom_killed:
            return "oom_killed"
        if self.is_restart_loop:
            return "restart_loop"
        if not self.running:
            return "stopped"
        return "running"


def parse_df_output(output: str) -> dict[str, int] | None:
    """Parse one `df -k <path>` output block.

    Returns a dict with used_bytes, available_bytes, and used_pct, or None if
    the output cannot be parsed (container not running, path missing, etc.).
    """
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Filesystem"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            return {
                "used_bytes": int(parts[2]) * 1024,
                "available_bytes": int(parts[3]) * 1024,
                "used_pct": int(parts[4].rstrip("%")),
            }
        except (ValueError, IndexError):
            continue
    return None


def parse_inspect(data: dict[str, Any]) -> ContainerStatus:
    """Build a ContainerStatus from one element of docker inspect JSON output."""
    state = data.get("State", {})
    host_config = data.get("HostConfig", {})
    mem = host_config.get("Memory", 0)
    return ContainerStatus(
        name=data.get("Name", "").lstrip("/"),
        running=bool(state.get("Running")),
        restart_count=int(data.get("RestartCount", 0)),
        oom_killed=bool(state.get("OOMKilled")),
        exit_code=state.get("ExitCode"),
        mem_limit_bytes=int(mem) if mem else None,
    )


def query_container(name: str) -> ContainerStatus | None:
    """Return a ContainerStatus by calling docker inspect, or None on failure."""
    try:
        out = subprocess.check_output(
            ["docker", "inspect", name],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        data_list = json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    if not data_list:
        return None
    return parse_inspect(data_list[0])


def team_container_names(num_teams: int) -> list[str]:
    """Return the canonical container names for all team containers."""
    return [
        name
        for i in range(1, num_teams + 1)
        for name in (f"team{i}-vuln", f"team{i}-ssh", f"team{i}-vuln-app")
    ]


def scan_team_resources(num_teams: int) -> list[ContainerStatus]:
    """Return health status for all reachable team containers."""
    results = []
    for name in team_container_names(num_teams):
        status = query_container(name)
        if status is not None:
            results.append(status)
    return results


def violations(statuses: list[ContainerStatus]) -> list[dict[str, Any]]:
    """Return a violation record for every unhealthy container.

    Checks OOM kills, restart loops, and disk pressure independently so a
    single container can appear in multiple categories.
    """
    out: list[dict[str, Any]] = []
    for s in statuses:
        if s.oom_killed:
            out.append(
                {
                    "container": s.name,
                    "type": OOM_KILL_EVENT,
                    "restart_count": s.restart_count,
                    "mem_limit_bytes": s.mem_limit_bytes,
                }
            )
        elif s.is_restart_loop:
            out.append(
                {
                    "container": s.name,
                    "type": RESTART_LOOP_EVENT,
                    "restart_count": s.restart_count,
                }
            )
        if s.disk_used_pct is not None and s.disk_used_pct >= DISK_PRESSURE_THRESHOLD_PCT:
            out.append(
                {
                    "container": s.name,
                    "type": DISK_PRESSURE_EVENT,
                    "disk_used_pct": s.disk_used_pct,
                    "disk_available_bytes": s.disk_available_bytes,
                }
            )
    return out
