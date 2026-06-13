# Round Engine

The gameserver starts a persisted scheduler alongside its HTTP API. Automatic
rounds run only while match `1` is `RUNNING`; a `PAUSED` match keeps its current
state and accepts explicit single-step requests.

## Round Lifecycle

Starting a round is one SQLite transaction:

1. Allocate the next monotonic round number for the match.
2. Snapshot every configured team/service pair.
3. Generate and persist one cryptographically random flag per target.
4. Mark flags whose persisted `expires_after_round` has been reached as
   `EXPIRED`.

Checker work runs after that transaction in two bounded phases. PUT completes
for every target before CHECK and GET are scheduled. Each result is persisted
as it completes, including DOWN, MUMBLE, and CORRUPT outcomes. Service failures
therefore complete the round with recorded SLA outcomes; only an internal
database or lifecycle invariant failure marks the round and match `FAILED`.

`ARENA_CHECKER_MAX_CONCURRENCY` bounds simultaneous checker jobs.
`ARENA_ROUND_DURATION_SECONDS` controls the interval between persisted round
start deadlines, and `ARENA_FLAG_EXPIRY_ROUNDS` controls flag validity.

## Recovery And Retries

A `RUNNING` round is its recovery journal. On restart the scheduler:

- reuses the existing target snapshot and flags;
- skips checker operations already present in `checker_results`;
- retries only missing operations;
- completes the round after exactly one PUT, CHECK, and GET result exists per
  target.

The `(match, round)` and `(match, team, service, round)` database constraints
prevent duplicate rounds and flags. Retried PUT operations use the same stored
flag, so a crash after the service side effect is deterministic.

## Operator Controls

All operator mutations require the Bearer token configured as
`ARENA_OPERATOR_TOKEN`. With the default host port `8000`:

```bash
OPERATOR_TOKEN="$(sed -n 's/^ARENA_OPERATOR_TOKEN=//p' config/arena.env)"

# Start a CREATED match. Round 1 is created by the scheduler within one poll.
curl -s -X POST http://localhost:8000/api/match/start \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

# Stop creation of future rounds. An operation already running may finish.
curl -s -X POST http://localhost:8000/api/match/pause \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

# Run or recover exactly one round while paused.
curl -s -X POST http://localhost:8000/api/rounds/step \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

# Resume automatic scheduling.
curl -s -X POST http://localhost:8000/api/match/resume \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

# Finish a running or paused match permanently.
curl -s -X POST http://localhost:8000/api/match/finish \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}"

# Read the latest persisted round.
curl -s http://localhost:8000/api/rounds/current
```

Single-step returns `409` unless the match is `PAUSED`. It never changes the
match back to `RUNNING`.
