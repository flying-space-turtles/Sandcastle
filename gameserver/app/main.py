"""FastAPI entry point for the gameserver.

Public endpoints (consumed by the React dashboard and by attacker teams):

    GET  /api/state                - Full snapshot of the competition.
    GET  /api/scoreboard           - Cumulative scores per team.
    GET  /api/events?limit=N       - Recent events (most recent first).
    GET  /api/teams                - Team roster (without secret tokens).
    POST /api/submit               - Submit a stolen flag.

Operator endpoints (used by the dashboard's "Actions" panel):

    POST /api/admin/tick           - Force a tick.
    POST /api/admin/pause          - Pause the tick engine.
    POST /api/admin/resume         - Resume the tick engine.
    POST /api/admin/team/{id}/down - docker stop the team's vuln container.
    POST /api/admin/team/{id}/up   - docker start the team's vuln container.
    POST /api/admin/team/{id}/restart
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import CONFIG
from .db import Database, open_db, rows_dicts
from .docker_admin import bring_up, container_state, docker_status, restart, take_down
from .teams import ensure_teams
from .tick import TickEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


FLAG_PATTERN = re.compile(r"^FLAG\{[a-f0-9]{32}\}$")


class SubmitFlag(BaseModel):
    team_token: str = Field(min_length=1, max_length=128)
    flag: str = Field(min_length=1, max_length=128)


class AdminAck(BaseModel):
    status: str
    detail: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = open_db()
    ensure_teams(db)
    db.add_event("server.start", "Gameserver started")
    engine = TickEngine(db)
    app.state.db = db
    app.state.engine = engine
    if CONFIG.auto_start:
        await engine.start()
    try:
        yield
    finally:
        await engine.stop()
        db.add_event("server.stop", "Gameserver stopped")


app = FastAPI(title="AD-CTF Gameserver", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _db(req_app: FastAPI) -> Database:
    return req_app.state.db


def _engine(req_app: FastAPI) -> TickEngine:
    return req_app.state.engine


# ---------- Public read API ------------------------------------------------


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "ad-ctf-gameserver",
        "config": {
            "num_teams": CONFIG.num_teams,
            "tick_duration": CONFIG.tick_duration,
            "flag_expiry_rounds": CONFIG.flag_expiry_rounds,
        },
    }


@app.get("/api/teams")
def get_teams() -> list[dict[str, Any]]:
    db: Database = app.state.db
    teams = []
    for row in db.list_teams():
        teams.append(
            {
                "id": row["id"],
                "name": row["name"],
                "ip_address": row["ip_address"],
                "service_url": CONFIG.team_service_url(row["id"]),
            }
        )
    return teams


@app.get("/api/scoreboard")
def get_scoreboard() -> list[dict[str, Any]]:
    db: Database = app.state.db
    return rows_dicts(db.cumulative_scores())


@app.get("/api/events")
def get_events(limit: int = 50) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit out of range")
    db: Database = app.state.db
    return rows_dicts(db.recent_events(limit=limit))


def _team_view(db: Database, team_row, latest_sla_map) -> dict[str, Any]:
    flag_row = db.latest_flag_for_team(team_row["id"])
    sla_row = latest_sla_map.get(team_row["id"])
    return {
        "id": team_row["id"],
        "name": team_row["name"],
        "ip_address": team_row["ip_address"],
        "service_url": CONFIG.team_service_url(team_row["id"]),
        "container": {
            "ssh": CONFIG.team_container_name(team_row["id"], "ssh"),
            "vuln": CONFIG.team_container_name(team_row["id"], "vuln"),
            "vuln_state": container_state(
                CONFIG.team_container_name(team_row["id"], "vuln")
            ),
        },
        "latest_flag_round": flag_row["round"] if flag_row else None,
        "sla_status": sla_row["status"] if sla_row else None,
        "sla_detail": sla_row["details"] if sla_row else None,
        "submission_token": team_row["token"],  # exposed locally for dashboard
    }


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    db: Database = app.state.db
    engine: TickEngine = app.state.engine
    teams_rows = db.list_teams()
    sla_map = db.latest_sla_per_team()
    teams = [_team_view(db, t, sla_map) for t in teams_rows]
    return {
        "config": {
            "num_teams": CONFIG.num_teams,
            "tick_duration": CONFIG.tick_duration,
            "flag_expiry_rounds": CONFIG.flag_expiry_rounds,
        },
        "round": engine.round,
        "paused": engine.paused,
        "last_tick_at": float(db.get_state("last_tick_at", "0") or "0"),
        "now": time.time(),
        "docker": docker_status().__dict__,
        "teams": teams,
        "scoreboard": rows_dicts(db.cumulative_scores()),
        "events": rows_dicts(db.recent_events(limit=50)),
    }


# ---------- Submission API -------------------------------------------------


@app.post("/api/submit")
def submit_flag(body: SubmitFlag) -> dict[str, Any]:
    if not FLAG_PATTERN.match(body.flag):
        raise HTTPException(status_code=400, detail="malformed flag")
    db: Database = app.state.db
    engine: TickEngine = app.state.engine

    attacker = db.find_team_by_token(body.team_token)
    if attacker is None:
        raise HTTPException(status_code=403, detail="invalid team token")

    flag_row = db.find_flag(body.flag)
    if flag_row is None:
        raise HTTPException(status_code=404, detail="flag not recognised")
    if flag_row["expired"]:
        raise HTTPException(status_code=404, detail="flag expired")
    if flag_row["team_id"] == attacker["id"]:
        raise HTTPException(status_code=403, detail="cannot submit your own flag")

    accepted = db.record_submission(flag_row["id"], attacker["id"])
    if not accepted:
        raise HTTPException(status_code=409, detail="already submitted")

    db.add_event(
        "flag.captured",
        f"{attacker['name']} captured a flag from team {flag_row['team_id']}",
        round_no=engine.round,
        team_id=attacker["id"],
    )
    return {"status": "accepted", "round": flag_row["round"]}


# ---------- Operator / Admin API ------------------------------------------


@app.post("/api/admin/tick", response_model=AdminAck)
async def admin_tick() -> AdminAck:
    engine: TickEngine = app.state.engine
    await engine.run_tick()
    return AdminAck(status="ok", detail=f"round {engine.round}")


@app.post("/api/admin/pause", response_model=AdminAck)
def admin_pause() -> AdminAck:
    engine: TickEngine = app.state.engine
    engine.set_paused(True)
    return AdminAck(status="paused")


@app.post("/api/admin/resume", response_model=AdminAck)
def admin_resume() -> AdminAck:
    engine: TickEngine = app.state.engine
    engine.set_paused(False)
    return AdminAck(status="resumed")


def _ensure_team(team_id: int):
    db: Database = app.state.db
    rows = [t for t in db.list_teams() if t["id"] == team_id]
    if not rows:
        raise HTTPException(status_code=404, detail="team not found")
    return rows[0]


@app.post("/api/admin/team/{team_id}/down", response_model=AdminAck)
def admin_team_down(team_id: int) -> AdminAck:
    team = _ensure_team(team_id)
    detail = take_down(team_id)
    db: Database = app.state.db
    db.add_event(
        "container.stopped",
        f"Operator stopped {team['name']}: {detail}",
        team_id=team_id,
    )
    return AdminAck(status="stopped", detail=detail)


@app.post("/api/admin/team/{team_id}/up", response_model=AdminAck)
def admin_team_up(team_id: int) -> AdminAck:
    team = _ensure_team(team_id)
    detail = bring_up(team_id)
    db: Database = app.state.db
    db.add_event(
        "container.started",
        f"Operator started {team['name']}: {detail}",
        team_id=team_id,
    )
    return AdminAck(status="started", detail=detail)


@app.post("/api/admin/team/{team_id}/restart", response_model=AdminAck)
def admin_team_restart(team_id: int) -> AdminAck:
    team = _ensure_team(team_id)
    detail = restart(team_id)
    db: Database = app.state.db
    db.add_event(
        "container.restarted",
        f"Operator restarted {team['name']}: {detail}",
        team_id=team_id,
    )
    return AdminAck(status="restarted", detail=detail)
