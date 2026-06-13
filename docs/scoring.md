# Deterministic Scoring

Sandcastle scoring policy `sandcastle-v1` projects authoritative match records
into immutable `score_events`. Standings are always calculated by summing those
events; no aggregate total is stored as the source of truth.

## Components

The committed local defaults are configurable in `config/arena.env` and copied
onto the match while it is still `CREATED` and has no score events:

| Component | Source record | Award condition | Default |
|---|---|---|---:|
| Attack | Accepted submission | An attacker submits an active opponent flag | 10 |
| Defense | Checker `GET` | The planted flag remains retrievable and returns `UP` in a completed round | 2 |
| SLA | Checker `CHECK` | The benign service workflow returns `UP` in a completed round | 1 |

`PUT` establishes checker state and does not score. Failed, missing, or
non-`UP` `GET` and `CHECK` results award zero points. Malformed, unknown,
self-owned, expired, and duplicate submissions never create attack events.

The weights are stored as `attack_points`, `defense_points`, and `sla_points`
on the match. This prevents later arena configuration changes from rewriting a
match's history.

## Replay And Idempotency

The scoring projector reads accepted submissions, flags, completed rounds, and
checker results. It appends any missing events with source references:

- one attack event per accepted submission;
- one defense event per successful completed-round `GET` result;
- one SLA event per successful completed-round `CHECK` result.

Database unique indexes on submission and checker-result sources make replay
safe to run repeatedly. Concurrent duplicate submissions are already protected
by the submission uniqueness constraint, so they cannot produce duplicate
attack score.

## Standings

Current standings are available at `GET /api/standings`. A single round is
available at `GET /api/rounds/{round_number}/scores`. Both responses expose the
stored policy, component totals, event counts, and tie rules.

Ordering is deterministic:

1. total points descending;
2. attack points descending;
3. defense points descending;
4. SLA points descending;
5. team ID ascending.
