# Contributing to Sandcastle

Read [`VISION.md`](VISION.md) and
[`docs/PROJECT_AUDIT_AND_BACKLOG.md`](docs/PROJECT_AUDIT_AND_BACKLOG.md)
before starting work. Every task in the backlog includes an agent prompt,
acceptance criteria, and validation commands.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Branch naming](#branch-naming)
3. [Running tests locally](#running-tests-locally)
4. [Full Docker integration test](#full-docker-integration-test)
5. [CI pipeline summary](#ci-pipeline-summary)
6. [Adding new tests](#adding-new-tests)
7. [Commit and PR conventions](#commit-and-pr-conventions)
8. [Engineering invariants](#engineering-invariants)

---

## Prerequisites

```bash
# Required for any work
docker             # Docker Engine (not Docker Desktop)
docker compose     # Compose plugin (v2)
bash               # 4.x or later
python3            # 3.10+

# Required for visualizer work
node               # 22+
npm

# Required for the full Docker integration test on a native Linux host
sudo               # to apply the firewall bridge-netfilter preflight
```

macOS and Windows with Docker Desktop are **not supported** for firewall
enforcement or the full integration test. Use a native Linux host or VM.

---

## Branch naming

Use the Linear task ID and a short slug:

```
fly-<N>-<short-description>
```

Examples:
- `fly-45-add-a-full-competition-lifecycle-integration-test`
- `fly-11-docker-in-docker-isolation`

Work on SC-series tasks from the backlog should use the corresponding
Linear ticket. If you don't have a ticket yet, open one before starting.

---

## Running tests locally

Install the local quality tools first (`shellcheck` from your package manager,
plus Ruff from the pinned development requirements):

```bash
python3 -m pip install -r requirements-dev.txt
```

**One command runs all checks and fixture-driven tests:**

```bash
./scripts/run-tests.sh
```

This runs in order:

| Step | What it checks |
|---|---|
| `bash -n`, ShellCheck | Syntax and static analysis for every tracked shell script |
| `py_compile`, Ruff | Syntax, formatting, and lint for every tracked Python module |
| bot component tests | Config, planners, model adapter, actions, API validation, runtime, and submissions |
| `firewall_test.py` | Firewall parsing, classification, and event unit tests |
| `gameserver_test.py` | Gameserver schema, registry, state, and API tests |
| `checker_test.py` | Checker statuses, persistence, scoping, and TurtleNotes workflows |
| `round_engine_test.py` | Fake-clock scheduling, retries, expiry, concurrency, and recovery |
| `scoring_test.py`, `telemetry_test.py` | Scoring replay plus telemetry storage and redaction |
| `firewall_preflight_test.sh` | Host preflight flag handling |
| `network_smoke_test.sh` | Smoke-network script fixture tests |
| `doctor_test.sh` | Doctor script fixture tests |
| `setup_test.sh` | Setup/generation fixture tests |
| `arena_test.sh` | Arena lifecycle fixture tests |
| `integration_test.sh --local` | SC-005 integration test (fixture mode) |
| `validate-compose.sh` | Every committed and generated Compose variant is valid |
| visualizer `npm ci && npm run build` | Visualizer builds |

Skip the visualizer build when iterating quickly:

```bash
./scripts/run-tests.sh --fast
```

---

## Full Docker integration test

The full integration test runs a real two-team arena end-to-end: setup,
startup, SSH reachability, flag plant, cross-team exploit, stale-namespace
regression, and cleanup verification.

**Requirements**: native Linux host, Docker Engine, and `net.bridge.bridge-nf-call-iptables`.

```bash
# One-time: apply the bridge-netfilter sysctl (survives until next reboot)
sudo ./scripts/firewall-preflight.sh --apply

# Run the full test (bounded to SC005_TIMEOUT seconds, default 180)
./tests/integration_test.sh

# Optional: change the timeout
SC005_TIMEOUT=300 ./tests/integration_test.sh
```

On failure, logs are written to `./sc005-logs/<timestamp>/`:

```
sc005-logs/
└── 20260612-203000/
    ├── compose.log
    ├── team1-vuln-app.log
    ├── team2-vuln-app.log
    └── firewall.log
```

Run the fixture-mode version during development (no Docker required):

```bash
./tests/integration_test.sh --local
```

---

## CI pipeline summary

Every push and pull request against `main` runs these jobs:

| Job | Trigger | Requires Docker |
|---|---|---|
| `python-lint` | push + PR | no |
| `visualizer-build` | push + PR | no |
| `gen-compose` | push + PR | yes (compose CLI) |
| `docker-lint` | push + PR | no |
| `compose-config` | push + PR | yes (compose CLI) |
| `scripts-executable` | push + PR | no |
| `doctor-tests` | push + PR | no |
| `setup-tests` | push + PR | no |
| `arena-lifecycle-tests` | push + PR | no |
| `firewall-tests` | push + PR | no |
| `integration-test-local` | push + PR | no |
| `integration-test-docker` | push to `main` only | yes (Linux) |

The `integration-test-docker` job applies the firewall preflight and runs
the full two-team smoke test. It silently skips if the runner cannot
apply `bridge-nf-call-iptables` (some hosted runners don't expose the
sysctl). The `integration-test-local` job always covers the control-flow
invariants.

Failure logs from `integration-test-docker` are uploaded as the
`sc005-failure-logs` artifact.

---

## Adding new tests

### Shell fixture tests

Follow the pattern in `tests/arena_test.sh` and `tests/network_smoke_test.sh`:

1. Create a fixture directory under `$(mktemp -d)`.
2. Stub binaries (`docker`, `sleep`, etc.) in a `bin/` subdirectory and
   prepend it to `PATH`.
3. Copy only the library files you need from `scripts/lib/`.
4. Run the script under test with `SANDCASTLE_ROOT` pointing at the fixture.
5. Assert log contents with `grep -Fq`.
6. Exit `0` on success, `1` on any assertion failure.

The script must be executable, start with `#!/usr/bin/env bash`, and use
`set -euo pipefail`.

### Python unit tests

Add `.py` files under `tests/`. They should be runnable with:

```bash
python3 -B tests/your_test.py
```

No test framework is required – plain `assert` and `sys.exit(1)` are fine.

### CI registration

Add a new job to `.github/workflows/ci.yml` following the existing
`doctor-tests` or `firewall-tests` pattern. Keep CI jobs focused on a
single layer.

---

## Commit and PR conventions

- **Commit messages**: `<type>: <short description>` where type is one of
  `feat`, `fix`, `test`, `docs`, `refactor`, `ci`, `chore`.
- **PR title**: same convention, reference the Linear task: `feat(SC-005): …`
- **One PR per backlog task** where practical. Stack PRs if a task depends on
  another that isn't merged yet.
- **No generated files in commits** unless they are the canonical output of a
  deterministic generation step (e.g. `docker-compose.yml` from `setup.sh`).
  Do not commit files under `teams/generated/`.
- **Every PR must pass all CI jobs** before merge. The `integration-test-local`
  job is a required check.

---

## Engineering invariants

These are from [`VISION.md`](VISION.md) and must not be violated:

- A clean checkout has one documented path to a working arena.
- Generated files are reproducible and never edited as canonical source.
- Startup is idempotent and removes or reports stale runtime resources.
- A reported healthy arena has every required service actually running.
- Checkers exercise legitimate behavior, not only `/health`.
- Flags are unique, authenticated, time-bounded, and never trusted from clients.
- Every P0 workflow has an automated end-to-end test.
- The platform fails loudly when required host networking features are absent.
- Security boundaries are documented honestly and verified by tests.

Before merging a task, re-read its acceptance criteria and confirm every
point is satisfied by a test or by a documented manual verification step.
