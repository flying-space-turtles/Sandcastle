"""Background tick engine.

Runs as an asyncio task started during FastAPI startup. Each tick:

    1. Increment the round counter.
    2. Plant a flag in every team's vulnerable service.
    3. Run an SLA check against every team in parallel.
    4. Recompute round scores from the recorded data.
    5. Emit events that the dashboard can consume.

The engine can be paused/resumed from the API, and a single tick can be
forced manually.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .checker import check_team_service
from .config import CONFIG
from .db import Database
from .planter import plant_flag
from .scoring import calculate_scores

logger = logging.getLogger(__name__)

ROUND_KEY = "round"
PAUSED_KEY = "paused"
TICK_AT_KEY = "last_tick_at"


class TickEngine:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._tick_now = asyncio.Event()
        self._busy = asyncio.Lock()

    @property
    def round(self) -> int:
        raw = self.db.get_state(ROUND_KEY, "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0

    @property
    def paused(self) -> bool:
        return (self.db.get_state(PAUSED_KEY, "false") or "false").lower() == "true"

    def set_paused(self, paused: bool) -> None:
        self.db.set_state(PAUSED_KEY, "true" if paused else "false")
        self.db.add_event(
            "tick.paused" if paused else "tick.resumed",
            "Competition paused" if paused else "Competition resumed",
            round_no=self.round,
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="tick-engine")
        logger.info("Tick engine started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._tick_now.set()
        await self._task
        self._task = None
        logger.info("Tick engine stopped")

    async def force_tick(self) -> None:
        self._tick_now.set()

    async def _loop(self) -> None:
        # First tick happens immediately so the dashboard has data.
        self._tick_now.set()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._tick_now.wait(), timeout=CONFIG.tick_duration)
            except asyncio.TimeoutError:
                pass
            self._tick_now.clear()
            if self._stop.is_set():
                break
            if self.paused:
                continue
            try:
                await self.run_tick()
            except Exception:  # noqa: BLE001
                logger.exception("tick failed")

    async def run_tick(self) -> int:
        async with self._busy:
            round_no = self.round + 1
            self.db.set_state(ROUND_KEY, str(round_no))
            self.db.set_state(TICK_AT_KEY, str(time.time()))
            self.db.add_event("tick.start", f"Round {round_no} started", round_no=round_no)
            logger.info("=== Round %d ===", round_no)

            teams = self.db.list_teams()
            if not teams:
                self.db.add_event("tick.skip", "No teams registered", round_no=round_no)
                return round_no

            async with httpx.AsyncClient() as client:
                # 1. Plant flags in parallel
                plant_tasks = [plant_flag(client, t["id"], round_no) for t in teams]
                plant_results = await asyncio.gather(*plant_tasks, return_exceptions=False)
                team_to_plant = {t["id"]: r for t, r in zip(teams, plant_results)}

                for team, result in zip(teams, plant_results):
                    self.db.insert_flag(
                        flag=result.flag,
                        team_id=team["id"],
                        round_no=round_no,
                        note_id=result.note_id,
                    )
                    if result.success:
                        self.db.add_event(
                            "flag.planted",
                            f"Planted flag in {team['name']} (note {result.note_id})",
                            round_no=round_no,
                            team_id=team["id"],
                        )
                    else:
                        self.db.add_event(
                            "flag.plant_failed",
                            f"Plant failed for {team['name']}: {result.detail}",
                            round_no=round_no,
                            team_id=team["id"],
                        )

                # 2. SLA checks in parallel
                async def _sla(team_row):
                    plant = team_to_plant.get(team_row["id"])
                    expected_flag = plant.flag if plant and plant.success else None
                    expected_note = plant.note_id if plant and plant.success else None
                    return await check_team_service(
                        client, team_row["id"], expected_flag, expected_note
                    )

                sla_tasks = [_sla(t) for t in teams]
                sla_results = await asyncio.gather(*sla_tasks, return_exceptions=False)
                for team, result in zip(teams, sla_results):
                    self.db.record_sla(team["id"], round_no, result.status, result.detail)
                    self.db.add_event(
                        f"sla.{result.status.lower()}",
                        f"{team['name']}: {result.status} — {result.detail}",
                        round_no=round_no,
                        team_id=team["id"],
                    )

            # 3. Expire old flags & 4. score
            self.db.expire_flags_older_than(round_no, CONFIG.flag_expiry_rounds)
            calculate_scores(self.db, round_no)
            self.db.add_event("tick.end", f"Round {round_no} complete", round_no=round_no)
            return round_no
