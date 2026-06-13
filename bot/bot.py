#!/usr/bin/env python3
"""Sandcastle CTF bot runner.

Runs inside a team's SSH container. The runner is intentionally small: it loads
JSON config, asks a planner for target/action tasks, and executes registered
actions. New AI agents can plug in by providing a planner object with:

    plan(ctx, override_target=None) -> iterable of BotTask(target_team, action_id)

Deploy from the host with bot/deploy.sh.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from collections import defaultdict
from typing import Any

from bot_lib.actions import ACTION_REGISTRY, WatchdogAction, action_catalog, log_action_result
from bot_lib.arena import ARENA_DEFAULTS
from bot_lib.config import BotConfig, load_config_file, merge_config
from bot_lib.planners import BotTask, load_planner, planner_catalog
from bot_lib.runtime import BotContext, detect_my_team, err, info, ok, ping_all, ping_team, warn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sandcastle CTF bot runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--teams",
        type=int,
        default=ARENA_DEFAULTS.team_count,
        metavar="N",
        help="total number of teams from arena configuration",
    )
    parser.add_argument("--my-team", type=int, default=None, metavar="N", help="override own team ID")
    parser.add_argument("--loop", type=int, default=0, metavar="SEC", help="repeat every SEC seconds")
    parser.add_argument("--watchdog", action="store_true", help="run self watchdog before each round")

    parser.add_argument("--ping", action="store_true", help="ping sweep and exit")
    parser.add_argument("--attack-team", type=int, default=None, metavar="TEAM", help="attack one team and exit")
    parser.add_argument("--fake-flag", type=int, default=None, metavar="TEAM", help="probe /internal/plant and exit")
    parser.add_argument("--catalog", action="store_true", help="print action/planner catalog as JSON and exit")

    parser.add_argument("--bot-name", type=str, default=None, metavar="NAME")
    parser.add_argument("--planner", type=str, default=None, metavar="ID")
    parser.add_argument("--target-policy", type=str, default=None, choices=["all_opponents", "selected"])
    parser.add_argument("--target-teams", type=str, default=None, metavar="LIST")
    parser.add_argument("--actions", type=str, default=None, metavar="LIST", help="comma-separated action ids")

    # Backward compatible flags from the original exploit runner.
    parser.add_argument("--exploits", type=str, default=None, metavar="LIST")
    parser.add_argument("--service-port", type=int, default=None, metavar="PORT")
    parser.add_argument("--flag-re", type=str, default=None, metavar="REGEX")
    parser.add_argument("--ip-pattern", type=str, default=None, metavar="PATTERN")
    parser.add_argument("--no-stop-on-first", action="store_true")
    parser.add_argument("--timeout", type=int, default=None, metavar="SEC")
    return parser


def cli_config(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for attr, key in (
        ("bot_name", "bot_name"),
        ("planner", "planner"),
        ("target_policy", "target_policy"),
        ("target_teams", "target_teams"),
        ("actions", "actions"),
        ("exploits", "exploits"),
        ("service_port", "service_port"),
        ("flag_re", "flag_re"),
        ("ip_pattern", "ip_pattern"),
        ("timeout", "timeout"),
    ):
        value = getattr(args, attr)
        if value is not None:
            data[key] = value

    if args.no_stop_on_first:
        data["stop_on_success"] = False
    return data


def config_summary(config: BotConfig) -> str:
    return (
        f"name={config.bot_name!r} planner={config.planner} "
        f"policy={config.target_policy} actions={config.actions} "
        f"port={config.service_port} ip={config.ip_pattern} timeout={config.timeout}s "
        f"deployment={config.deployment_id or 'standalone'}"
    )


def should_run_watchdog(config: BotConfig, cli_watchdog: bool) -> bool:
    return cli_watchdog or "maintain.watchdog" in config.actions


def run_watchdog(ctx: BotContext) -> None:
    ctx.emit("action.started", action_id="maintain.watchdog", target_team=ctx.my_team)
    result = WatchdogAction().run(ctx)
    log_action_result(result)
    ctx.emit(
        "action.completed",
        action_id=result.action_id,
        target_team=result.target_team,
        status=result.status,
        message=result.message,
    )


def execute_tasks(ctx: BotContext, tasks: list[BotTask]) -> dict[int, list[str]]:
    flags_by_team: dict[int, list[str]] = defaultdict(list)
    completed_after_success: set[tuple[int, str]] = set()

    for task in tasks:
        action = ACTION_REGISTRY.get(task.action_id)
        if action is None:
            warn(f"unknown action skipped: {task.action_id}")
            continue

        if action.scope != "target":
            continue

        if ctx.config.stop_on_success and (task.target_team, action.category) in completed_after_success:
            continue

        if task.target_team == ctx.my_team:
            continue

        if action.category == "Exploit" and not ping_team(ctx, task.target_team):
            warn(f"team{task.target_team} [{action.id}] no ping response")
            ctx.emit(
                "action.completed",
                action_id=action.id,
                target_team=task.target_team,
                status="unreachable",
            )
            continue

        ctx.emit("action.started", action_id=action.id, target_team=task.target_team)
        result = action.run(ctx, task.target_team)
        log_action_result(result)
        ctx.emit(
            "action.completed",
            action_id=result.action_id,
            target_team=result.target_team,
            status=result.status,
            message=result.message,
            flag_count=len(result.flags),
        )
        if result.flags:
            flags_by_team[task.target_team].extend(result.flags)
            for flag in sorted(set(result.flags)):
                fingerprint = ctx.flag_fingerprint(flag)
                ctx.emit(
                    "flag.captured",
                    action_id=result.action_id,
                    target_team=task.target_team,
                    flag_fingerprint=fingerprint,
                )
                outcome = ctx.submit_flag(flag, task.target_team, result.action_id)
                code = str(outcome.get("code", "UNKNOWN"))
                if outcome.get("accepted"):
                    ok(f"team{task.target_team} [{result.action_id}] submitted {fingerprint}: {code}")
                else:
                    warn(f"team{task.target_team} [{result.action_id}] submission {fingerprint}: {code}")
            if ctx.config.stop_on_success:
                completed_after_success.add((task.target_team, action.category))

    return {team: sorted(set(flags)) for team, flags in flags_by_team.items()}


def run_round(ctx: BotContext, override_target: int | None = None) -> dict[int, list[str]]:
    planner = load_planner(ctx.config.planner)
    tasks = list(planner.plan(ctx, override_target))

    if override_target is not None:
        info(f"Attacking team{override_target} as team{ctx.my_team}")
    else:
        targets = sorted({task.target_team for task in tasks})
        info(f"Round plan: {len(tasks)} action(s), targets={targets}")
    ctx.emit(
        "round.planned",
        target_override=override_target,
        task_count=len(tasks),
        targets=sorted({task.target_team for task in tasks}),
        actions=[task.action_id for task in tasks],
    )

    return execute_tasks(ctx, tasks)


def main() -> int:
    args = build_parser().parse_args()

    if args.catalog:
        import json

        print(json.dumps({"actions": action_catalog(), "planners": planner_catalog()}, indent=2))
        return 0

    config = merge_config(load_config_file(), cli_config(args))

    my_team = args.my_team
    if my_team is None:
        env_team = os.environ.get("MY_TEAM", "")
        my_team = int(env_team) if env_team.isdigit() else detect_my_team()

    ctx = BotContext(config=config, num_teams=args.teams, my_team=my_team)
    ctx.emit("deployment.started", bot_name=config.bot_name, planner=config.planner)

    if my_team is None:
        warn("Could not detect own team. Pass --my-team N or set MY_TEAM=N.")
    else:
        info(f"Running as team{my_team} (hostname: {socket.gethostname()})")

    info(f"Config: {config_summary(config)}")

    if args.ping:
        ping_all(ctx)
        return 0

    if args.fake_flag is not None:
        action = ACTION_REGISTRY["probe.plant_endpoint"]
        result = action.run(ctx, args.fake_flag)
        log_action_result(result)
        return 0 if result.status == "ok" else 1

    if args.attack_team is not None:
        flags = run_round(ctx, args.attack_team)
        return 0 if flags else 1

    info(f"Bot started - interval={args.loop}s watchdog={should_run_watchdog(config, args.watchdog)}")
    first = True
    while True:
        if not first:
            info(f"Sleeping {args.loop}s")
            ctx.emit("deployment.sleeping", seconds=args.loop)
            time.sleep(args.loop)
        first = False

        if should_run_watchdog(config, args.watchdog):
            run_watchdog(ctx)

        try:
            ctx.emit("round.started")
            results = run_round(ctx)
        except Exception as exc:
            err(f"round failed: {exc}")
            ctx.emit("round.failed", message=str(exc)[:200])
            results = {}

        total = sum(len(flags) for flags in results.values())
        ok(f"Round done - {total} flag(s) captured from {len(results)} team(s)")
        ctx.emit("round.completed", flag_count=total, target_count=len(results))

        if args.loop == 0:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
