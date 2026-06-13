from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class MatchState(str, enum.Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


class RoundState(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FlagState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


# Valid transitions for match states.
# Self-transitions are handled separately as idempotent no-ops.
VALID_TRANSITIONS = {
    MatchState.CREATED: {MatchState.RUNNING, MatchState.FAILED},
    MatchState.RUNNING: {MatchState.PAUSED, MatchState.FINISHED, MatchState.FAILED},
    MatchState.PAUSED: {MatchState.RUNNING, MatchState.FINISHED, MatchState.FAILED},
    MatchState.FINISHED: set(),
    MatchState.FAILED: set(),
}


def validate_state_transition(current: str | MatchState, target: str | MatchState) -> MatchState:
    """Validate transition from current state to target state.

    Allows idempotent self-transitions. Raises ValueError if invalid.
    """
    try:
        curr_enum = MatchState(current)
    except ValueError:
        raise ValueError(f"Invalid current state: {current}")

    try:
        tgt_enum = MatchState(target)
    except ValueError:
        raise ValueError(f"Invalid target state: {target}")

    if curr_enum == tgt_enum:
        return tgt_enum

    if tgt_enum not in VALID_TRANSITIONS[curr_enum]:
        raise ValueError(
            f"Invalid match state transition from {curr_enum.value} to {tgt_enum.value}"
        )

    return tgt_enum


@dataclass
class Match:
    id: int
    status: MatchState
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: tuple) -> Match:
        return cls(
            id=row[0],
            status=MatchState(row[1]),
            created_at=row[2],
            updated_at=row[3],
        )


@dataclass
class Team:
    id: int
    name: str
    token_hash: str
    ip_address: str

    @classmethod
    def from_row(cls, row: tuple) -> Team:
        return cls(
            id=row[0],
            name=row[1],
            token_hash=row[2],
            ip_address=row[3],
        )


@dataclass
class Service:
    id: int
    name: str
    port: int

    @classmethod
    def from_row(cls, row: tuple) -> Service:
        return cls(
            id=row[0],
            name=row[1],
            port=row[2],
        )


@dataclass
class Round:
    id: int
    match_id: int
    round_number: int
    status: RoundState
    started_at: str
    deadline_at: str
    completed_at: Optional[str]
    duration_seconds: int
    error: Optional[str]


@dataclass
class Flag:
    id: int
    flag: str
    match_id: int
    team_id: int
    service_id: int
    round_number: int
    target_host: str
    service_name: str
    service_port: int
    status: FlagState
    expires_after_round: int
    created_at: str
    expired_at: Optional[str]


@dataclass
class CheckerResult:
    id: int
    match_id: int
    team_id: int
    service_id: int
    round_number: int
    operation: str  # PUT, GET, CHECK
    plugin_name: str
    plugin_version: str
    status: str  # UP, DOWN, MUMBLE, CORRUPT
    message: str
    duration_ms: int
    data_json: str
    created_at: str


@dataclass
class Submission:
    id: int
    flag: str
    attacker_id: int
    submitted_at: str
    status: str  # ACCEPTED, REJECTED


@dataclass
class ScoreEvent:
    id: int
    team_id: int
    round_number: int
    event_type: str  # ATTACK, DEFENSE, SLA
    points: float
    details: Optional[str]
    submission_id: Optional[int]
    created_at: str
