from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Iterable, Protocol

from .actions import ACTION_REGISTRY
from .runtime import BotContext


@dataclass(frozen=True)
class BotTask:
    target_team: int
    action_id: str


class Planner(Protocol):
    id: str
    label: str
    description: str

    def targets(self, ctx: BotContext, override_target: int | None = None) -> list[int]: ...

    def plan(self, ctx: BotContext, override_target: int | None = None) -> Iterable[BotTask]: ...


class ScriptedPlanner:
    id = "scripted"
    label = "Scripted"
    description = "Run selected actions in order against each eligible opponent."

    def targets(self, ctx: BotContext, override_target: int | None = None) -> list[int]:
        if override_target is not None:
            return [override_target]

        if ctx.config.target_policy == "selected":
            candidates = ctx.config.target_teams
        else:
            candidates = list(range(1, ctx.num_teams + 1))

        return [team for team in candidates if team != ctx.my_team]

    def plan(self, ctx: BotContext, override_target: int | None = None) -> Iterable[BotTask]:
        action_ids = [
            action_id
            for action_id in ctx.config.actions
            if ACTION_REGISTRY.get(action_id) and ACTION_REGISTRY[action_id].scope == "target"
        ]
        for team in self.targets(ctx, override_target):
            for action_id in action_ids:
                yield BotTask(team, action_id)


class ReconFirstPlanner(ScriptedPlanner):
    id = "recon_first"
    label = "Recon, then exploits"
    description = "Run recon across all targets, then run offensive actions."

    def plan(self, ctx: BotContext, override_target: int | None = None) -> Iterable[BotTask]:
        targets = self.targets(ctx, override_target)
        action_ids = [
            action_id
            for action_id in ctx.config.actions
            if ACTION_REGISTRY.get(action_id) and ACTION_REGISTRY[action_id].scope == "target"
        ]
        recon = [
            action_id for action_id in action_ids if ACTION_REGISTRY[action_id].category == "Recon"
        ]
        other = [action_id for action_id in action_ids if action_id not in recon]

        for action_id in recon:
            for team in targets:
                yield BotTask(team, action_id)
        for team in targets:
            for action_id in other:
                yield BotTask(team, action_id)


PLANNER_REGISTRY: dict[str, Planner] = {
    ScriptedPlanner.id: ScriptedPlanner(),
    ReconFirstPlanner.id: ReconFirstPlanner(),
}


def planner_catalog() -> list[dict[str, str]]:
    return [
        {
            "id": planner.id,
            "label": planner.label,
            "description": planner.description,
        }
        for planner in PLANNER_REGISTRY.values()
    ] + [
        {
            "id": "model",
            "label": "Model-backed agent",
            "description": (
                "Delegates planning to an LLM via the bot-controller /plan endpoint. "
                "Requires PLAN_ENDPOINT and PLAN_TOKEN environment variables. "
                "LLM credentials stay on the host, not in team containers."
            ),
        },
        {
            "id": "module:package.object",
            "label": "External module",
            "description": "Import a custom planner object with plan(ctx, override_target).",
        },
    ]


def load_planner(planner_id: str) -> Planner:
    if planner_id == "model":
        from .model_planner import make_model_planner  # lazy: avoids circular import

        return make_model_planner()

    if planner_id in PLANNER_REGISTRY:
        return PLANNER_REGISTRY[planner_id]

    if ":" not in planner_id:
        raise ValueError(f"unknown planner: {planner_id}")

    module_name, object_name = planner_id.split(":", 1)
    module = importlib.import_module(module_name)
    planner = getattr(module, object_name)
    if isinstance(planner, type):
        planner = planner()
    return planner
