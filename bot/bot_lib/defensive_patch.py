"""AI-013: Transactional defensive patch validation with automatic rollback.

Orchestrates: snapshot → validate diff → apply → rebuild → health →
checker(before not needed here, snapshot proves it) →
checker(after) → exploit-regression → commit or rollback.

Idempotent for retried correlation IDs (same tx_id returns cached result).
Prevents concurrent patch transactions per team.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from .defensive_tools import (
    DefensiveToolError,
    PatchTransaction,
    SourceSnapshot,
    TransactionConflictError,
    apply_diff,
    create_snapshot,
    rebuild_service,
    restart_service,
    restore_snapshot,
    run_checker,
    run_own_exploit,
    show_diff,
    validate_diff,
)

log = logging.getLogger("patch_workflow")

_HEALTH_RETRY_INTERVAL = 2.0
_HEALTH_MAX_RETRIES = 15


def _tx_id(correlation_id: str) -> str:
    return hashlib.sha256(correlation_id.encode()).hexdigest()[:16]


class TransactionRegistry:
    """In-memory registry of active and completed patch transactions.

    One active transaction per team_id is enforced.
    Completed transactions are cached for idempotency.
    """

    def __init__(self) -> None:
        self._active: dict[int, str] = {}  # team_id -> tx_id
        self._completed: dict[str, PatchTransaction] = {}  # tx_id -> tx

    def start(self, team_id: int, tx_id: str, tx: PatchTransaction) -> None:
        if team_id in self._active:
            existing = self._active[team_id]
            if existing != tx_id:
                raise TransactionConflictError(
                    f"team {team_id} already has an active patch transaction: {existing}"
                )
        self._active[team_id] = tx_id
        self._completed[tx_id] = tx

    def finish(self, team_id: int, tx_id: str) -> None:
        self._active.pop(team_id, None)

    def get(self, tx_id: str) -> PatchTransaction | None:
        return self._completed.get(tx_id)

    def is_active(self, team_id: int) -> bool:
        return team_id in self._active


# Global registry (shared across requests in the same process)
_REGISTRY = TransactionRegistry()


class DefensivePatchWorkflow:
    """Orchestrates a bounded, transactional patch for the own-team service.

    Usage:
        workflow = DefensivePatchWorkflow(service_root, snapshots_root, ...)
        tx = workflow.run(correlation_id, diff_text)
        if tx.status == "committed":
            ...
    """

    def __init__(
        self,
        service_root: Path,
        snapshots_root: Path,
        checker_path: Path,
        exploit_paths: list[Path],
        compose_project: str,
        service_host: str,
        service_port: int,
        team_id: int,
        *,
        registry: TransactionRegistry | None = None,
    ) -> None:
        self.service_root = service_root.resolve()
        self.snapshots_root = snapshots_root
        self.checker_path = checker_path
        self.exploit_paths = exploit_paths
        self.compose_project = compose_project
        self.service_host = service_host
        self.service_port = service_port
        self.team_id = team_id
        self._registry = registry or _REGISTRY

    def run(self, correlation_id: str, diff_text: str) -> PatchTransaction:
        """Execute the full patch workflow.  Idempotent for the same correlation_id."""
        tx_id = _tx_id(correlation_id)

        # Idempotency: return cached result for the same correlation_id
        cached = self._registry.get(tx_id)
        if cached is not None and cached.status != "open":
            log.info("returning cached tx result for %s", tx_id)
            return cached

        # Build transaction object
        snapshot = create_snapshot(self.service_root, self.snapshots_root)
        tx = PatchTransaction(
            transaction_id=tx_id,
            team_id=self.team_id,
            snapshot=snapshot,
            diff_text=diff_text,
        )

        # Register (raises TransactionConflictError if another is active)
        self._registry.start(self.team_id, tx_id, tx)

        try:
            self._execute(tx)
        except Exception as exc:  # noqa: BLE001
            tx.error = str(exc)[:500]
            tx.status = "failed"
            self._rollback(tx)
        finally:
            self._registry.finish(self.team_id, tx_id)

        return tx

    def _execute(self, tx: PatchTransaction) -> None:
        # 1. Validate diff before touching any files
        try:
            validate_diff(tx.diff_text)
        except DefensiveToolError as exc:
            tx.error = str(exc)
            tx.status = "failed"
            return

        # 2. Checker before patch (prove service is healthy before we touch it)
        ok, out = run_checker(self.checker_path, self.service_host, self.service_port)
        tx.checker_output_before = out
        tx.checker_passed_before = ok
        if not ok:
            log.warning("checker failed before patch; aborting tx %s", tx.transaction_id)
            tx.error = "checker failed before patch — service may already be broken"
            tx.status = "failed"
            return

        # 3. Apply diff
        try:
            tx.patch_output = apply_diff(self.service_root, tx.diff_text)
            tx.patch_applied = True
            tx.changed_files = self._detect_changed_files(tx.snapshot)
        except DefensiveToolError as exc:
            tx.error = str(exc)
            tx.status = "failed"
            self._rollback(tx)
            return

        # 4. Rebuild and restart
        ok_build, build_out = rebuild_service(self.service_root, self.compose_project)
        if not ok_build:
            tx.error = f"rebuild failed: {build_out[:300]}"
            tx.status = "failed"
            self._rollback(tx)
            return

        ok_restart, _ = restart_service(self.service_root, self.compose_project)
        if not ok_restart:
            tx.error = "restart after rebuild failed"
            tx.status = "failed"
            self._rollback(tx)
            return

        self._wait_healthy()

        # 5. Checker after patch — must pass
        ok, out = run_checker(self.checker_path, self.service_host, self.service_port)
        tx.checker_output_after = out
        tx.checker_passed_after = ok
        if not ok:
            tx.error = "checker failed after patch — SLA regression"
            tx.status = "failed"
            self._rollback(tx)
            return

        # 6. Exploit regression — all registered exploits must FAIL
        exploit_succeeded = False
        for exploit_path in self.exploit_paths:
            success, out = run_own_exploit(exploit_path, self.service_host, self.service_port)
            tx.exploit_output_after += out[:500]
            if success:
                exploit_succeeded = True
                break

        tx.exploit_blocked = not exploit_succeeded
        if exploit_succeeded:
            tx.error = "patch did not block the reference exploit — not committed"
            tx.status = "failed"
            self._rollback(tx)
            return

        # All gates passed
        tx.status = "committed"
        log.info("patch tx %s committed for team %d", tx.transaction_id, self.team_id)

    def _rollback(self, tx: PatchTransaction) -> None:
        if not tx.patch_applied:
            return
        log.warning("rolling back patch tx %s", tx.transaction_id)
        try:
            restore_snapshot(tx.snapshot)
            rebuild_service(self.service_root, self.compose_project)
            restart_service(self.service_root, self.compose_project)
            self._wait_healthy()
            ok, _ = run_checker(self.checker_path, self.service_host, self.service_port)
            if not ok:
                log.error("service checker still failing after rollback (tx %s)", tx.transaction_id)
            tx.status = "rolled_back"
        except Exception as exc:  # noqa: BLE001
            log.error("rollback failed for tx %s: %s", tx.transaction_id, exc)
            tx.status = "failed"

    def _wait_healthy(self) -> None:
        for _ in range(_HEALTH_MAX_RETRIES):
            ok, _ = run_checker(self.checker_path, self.service_host, self.service_port)
            if ok:
                return
            time.sleep(_HEALTH_RETRY_INTERVAL)
        log.warning("service did not become healthy within timeout")

    def _detect_changed_files(self, snapshot: SourceSnapshot) -> list[str]:
        diff = show_diff(self.service_root, snapshot)
        changed = []
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                changed.append(line[6:])
        return changed


# ---------------------------------------------------------------------------
# Fixture/no-Docker workflow for testing
# ---------------------------------------------------------------------------


class FixtureDefensivePatchWorkflow(DefensivePatchWorkflow):
    """Scripted fixture workflow for unit tests — no Docker required."""

    def __init__(
        self,
        service_root: Path,
        snapshots_root: Path,
        *,
        checker_passes: bool = True,
        rebuild_passes: bool = True,
        exploit_blocked: bool = True,
        patch_commits: bool = True,  # alias: a patch "commits" when all gates pass
        team_id: int = 1,
        registry: TransactionRegistry | None = None,
    ) -> None:
        # Don't call super().__init__ — we override _execute entirely
        self.service_root = service_root.resolve()
        self.snapshots_root = snapshots_root
        self.team_id = team_id
        self._registry = registry or TransactionRegistry()
        self._checker_passes = checker_passes
        self._rebuild_passes = rebuild_passes and patch_commits
        self._exploit_blocked = exploit_blocked
        # Stubs required by base class attributes (not called in fixture mode)
        self.checker_path = service_root / "checker.py"
        self.exploit_paths = []
        self.compose_project = f"fixture-team{team_id}"
        self.service_host = "127.0.0.1"
        self.service_port = 8080

    def run(self, correlation_id: str, diff_text: str) -> PatchTransaction:
        tx_id = _tx_id(correlation_id)
        cached = self._registry.get(tx_id)
        if cached is not None and cached.status != "open":
            return cached

        snapshot = create_snapshot(self.service_root, self.snapshots_root)
        tx = PatchTransaction(
            transaction_id=tx_id,
            team_id=self.team_id,
            snapshot=snapshot,
            diff_text=diff_text,
        )
        self._registry.start(self.team_id, tx_id, tx)

        try:
            self._fixture_execute(tx)
        except Exception as exc:  # noqa: BLE001
            tx.error = str(exc)[:500]
            tx.status = "failed"
        finally:
            self._registry.finish(self.team_id, tx_id)

        return tx

    def _fixture_execute(self, tx: PatchTransaction) -> None:
        try:
            validate_diff(tx.diff_text)
        except DefensiveToolError as exc:
            tx.error = str(exc)
            tx.status = "failed"
            return

        tx.checker_passed_before = self._checker_passes
        if not self._checker_passes:
            tx.error = "fixture: checker failed before patch"
            tx.status = "failed"
            return

        tx.patch_applied = True
        tx.changed_files = ["app/app.py"]

        if not self._rebuild_passes:
            tx.error = "fixture: rebuild failed"
            tx.status = "failed"
            return

        tx.checker_passed_after = self._checker_passes
        if not self._checker_passes:
            tx.error = "fixture: SLA regression"
            tx.status = "failed"
            return

        tx.exploit_blocked = self._exploit_blocked
        if not self._exploit_blocked:
            tx.error = "fixture: exploit still succeeds after patch"
            tx.status = "failed"
            return

        tx.status = "committed"
