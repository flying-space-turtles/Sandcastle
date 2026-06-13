from __future__ import annotations

import enum
import json
import math
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

import db
from models import FlagState
from security import verify_team_token


FLAG_PATTERN = re.compile(r"FLAG\{[a-f0-9]{32}\}")


class SubmissionCode(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    SELF_OWNED = "SELF_OWNED"
    EXPIRED = "EXPIRED"
    MALFORMED = "MALFORMED"
    UNKNOWN = "UNKNOWN"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class SubmissionResult:
    code: SubmissionCode
    submission_id: int | None = None
    score_event_id: int | None = None

    @property
    def accepted(self) -> bool:
        return self.code is SubmissionCode.ACCEPTED

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "code": self.code.value,
            "accepted": self.accepted,
        }
        if self.submission_id is not None:
            body["submission_id"] = self.submission_id
        if self.score_event_id is not None:
            body["score_event_id"] = self.score_event_id
        return body


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class TeamRateLimiter:
    """Thread-safe per-team sliding-window request limiter."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0:
            raise ValueError("rate limit must be positive")
        if window_seconds <= 0:
            raise ValueError("rate limit window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._requests: dict[int, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, team_id: int) -> RateLimitDecision:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests[team_id]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                retry_after = max(1, math.ceil(requests[0] + self.window_seconds - now))
                return RateLimitDecision(False, retry_after)
            requests.append(now)
            return RateLimitDecision(True)

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()


def is_valid_flag_format(flag: object) -> bool:
    return isinstance(flag, str) and FLAG_PATTERN.fullmatch(flag) is not None


def authenticate_team(team_id: object, token: str, db_path: str | None = None) -> bool:
    if not isinstance(team_id, int) or isinstance(team_id, bool) or team_id <= 0:
        return False
    conn = db.get_db_connection(db_path)
    try:
        row = conn.execute("SELECT token FROM teams WHERE id = ?", (team_id,)).fetchone()
        return row is not None and verify_team_token(token, row[0])
    finally:
        conn.close()


def record_submission(
    attacker_id: int,
    flag: object,
    db_path: str | None = None,
) -> SubmissionResult:
    if not is_valid_flag_format(flag):
        return SubmissionResult(SubmissionCode.MALFORMED)

    conn = db.get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        flag_row = conn.execute(
            """
            SELECT id, match_id, team_id, service_id, round_number,
                   status, expires_after_round
            FROM flags WHERE flag = ?
            """,
            (flag,),
        ).fetchone()
        if flag_row is None:
            conn.rollback()
            return SubmissionResult(SubmissionCode.UNKNOWN)

        _, match_id, owner_id, service_id, flag_round, status, expires_after = flag_row
        if owner_id == attacker_id:
            conn.rollback()
            return SubmissionResult(SubmissionCode.SELF_OWNED)

        duplicate = conn.execute(
            "SELECT id FROM submissions WHERE flag = ? AND attacker_id = ?",
            (flag, attacker_id),
        ).fetchone()
        if duplicate is not None:
            conn.rollback()
            return SubmissionResult(SubmissionCode.DUPLICATE, submission_id=duplicate[0])

        current_round = conn.execute(
            "SELECT COALESCE(MAX(round_number), 0) FROM rounds WHERE match_id = ?",
            (match_id,),
        ).fetchone()[0]
        if status == FlagState.EXPIRED.value or current_round >= expires_after:
            conn.rollback()
            return SubmissionResult(SubmissionCode.EXPIRED)

        submission_cursor = conn.execute(
            """
            INSERT INTO submissions (flag, attacker_id, status)
            VALUES (?, ?, ?)
            """,
            (flag, attacker_id, SubmissionCode.ACCEPTED.value),
        )
        submission_id = int(submission_cursor.lastrowid)
        policy_row = conn.execute(
            "SELECT attack_points FROM matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        if policy_row is None:
            raise RuntimeError(f"match {match_id} does not exist")
        details = json.dumps(
            {
                "submission_id": submission_id,
                "victim_team_id": owner_id,
                "service_id": service_id,
                "flag_round": flag_round,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        score_cursor = conn.execute(
            """
            INSERT INTO score_events (
                match_id, team_id, round_number, event_type, points,
                details, submission_id
            ) VALUES (?, ?, ?, 'ATTACK', ?, ?, ?)
            """,
            (
                match_id,
                attacker_id,
                flag_round,
                float(policy_row[0]),
                details,
                submission_id,
            ),
        )
        score_event_id = int(score_cursor.lastrowid)
        conn.commit()
        return SubmissionResult(
            SubmissionCode.ACCEPTED,
            submission_id=submission_id,
            score_event_id=score_event_id,
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "submissions.flag, submissions.attacker_id" in str(exc):
            row = conn.execute(
                "SELECT id FROM submissions WHERE flag = ? AND attacker_id = ?",
                (flag, attacker_id),
            ).fetchone()
            return SubmissionResult(
                SubmissionCode.DUPLICATE,
                submission_id=row[0] if row else None,
            )
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
