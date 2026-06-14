#!/usr/bin/env python3
"""Tests for AI-012: defensive_tools and AI-013: defensive_patch."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.defensive_tools import (
    DefensiveToolError,
    TransactionConflictError,
    _safe_path,
    create_snapshot,
    list_allowed_files,
    read_file_range,
    restore_snapshot,
    search_source,
    validate_diff,
)
from bot_lib.defensive_patch import (
    FixtureDefensivePatchWorkflow,
    TransactionRegistry,
)


def _make_service(root: Path) -> None:
    """Create a minimal fake service tree."""
    (root / "app").mkdir(parents=True)
    (root / "app" / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n"
        "@app.get('/')\ndef index(): return 'hello'\n"
        "# VULN: open(request.args.get('file'))\n"
    )
    (root / "requirements.txt").write_text("flask>=3.0\n")
    (root / "checker.py").write_text("# checker\n")


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "service"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_safe_path_inside_root(self):
        p = _safe_path(self.root, "app/app.py")
        self.assertTrue(str(p).startswith(str(self.root.resolve())))

    def test_path_traversal_rejected(self):
        with self.assertRaises(DefensiveToolError):
            _safe_path(self.root, "../etc/passwd")

    def test_double_dot_in_middle_rejected(self):
        with self.assertRaises(DefensiveToolError):
            _safe_path(self.root, "app/../../etc/passwd")

    def test_forbidden_name_rejected(self):
        with self.assertRaises(DefensiveToolError):
            _safe_path(self.root, ".env")

    def test_pyc_pattern_rejected(self):
        with self.assertRaises(DefensiveToolError):
            _safe_path(self.root, "app/__pycache__/app.cpython-312.pyc")


class ListFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "service"
        self.root.mkdir()
        _make_service(self.root)
        # Add a forbidden file
        (self.root / ".env").write_text("SECRET=x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_allowed_files(self):
        files = list_allowed_files(self.root)
        self.assertIn("app/app.py", files)
        self.assertIn("requirements.txt", files)

    def test_excludes_forbidden(self):
        files = list_allowed_files(self.root)
        self.assertNotIn(".env", files)


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "service"
        self.root.mkdir()
        _make_service(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_allowed_file(self):
        content = read_file_range(self.root, "app/app.py")
        self.assertIn("Flask", content)

    def test_traversal_rejected_in_read(self):
        with self.assertRaises(DefensiveToolError):
            read_file_range(self.root, "../etc/passwd")

    def test_missing_file_raises(self):
        with self.assertRaises(DefensiveToolError):
            read_file_range(self.root, "app/missing.py")

    def test_line_range_respected(self):
        content = read_file_range(self.root, "app/app.py", start_line=1, end_line=1)
        self.assertIn("flask", content.lower())
        self.assertNotIn("index", content.lower())


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "service"
        self.root.mkdir()
        _make_service(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_literal_search_finds_match(self):
        results = search_source(self.root, "VULN")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["file"], "app/app.py")

    def test_literal_search_no_match(self):
        results = search_source(self.root, "NOTHING_HERE_XYZ")
        self.assertEqual(results, [])

    def test_regex_search(self):
        results = search_source(self.root, r"def \w+", literal=False)
        self.assertGreaterEqual(len(results), 1)

    def test_invalid_regex_raises(self):
        with self.assertRaises(DefensiveToolError):
            search_source(self.root, "[unclosed", literal=False)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "service"
        self.root.mkdir()
        self.snaps = Path(self._tmp.name) / "snapshots"
        _make_service(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_snapshot(self):
        snap = create_snapshot(self.root, self.snaps)
        self.assertTrue(snap.snapshot_dir.exists())
        self.assertGreater(snap.file_count, 0)

    def test_snapshot_is_deterministic(self):
        snap1 = create_snapshot(self.root, self.snaps)
        snap2 = create_snapshot(self.root, self.snaps)
        self.assertEqual(snap1.snapshot_id, snap2.snapshot_id)

    def test_restore_recovers_content(self):
        snap = create_snapshot(self.root, self.snaps)
        # Modify the file
        (self.root / "app" / "app.py").write_text("# modified\n")
        restore_snapshot(snap)
        content = (self.root / "app" / "app.py").read_text()
        self.assertIn("Flask", content)


class ValidateDiffTests(unittest.TestCase):
    def test_valid_diff_passes(self):
        diff = "--- a/app/app.py\n+++ b/app/app.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        validate_diff(diff)  # no exception

    def test_empty_diff_raises(self):
        with self.assertRaises(DefensiveToolError):
            validate_diff("")

    def test_oversized_diff_raises(self):
        big = "--- a/x\n+++ b/x\n" + "+" * 70000
        with self.assertRaises(DefensiveToolError):
            validate_diff(big)

    def test_too_many_files_raises(self):
        headers = "\n".join(f"--- a/f{i}\n+++ b/f{i}" for i in range(12))
        with self.assertRaises(DefensiveToolError):
            validate_diff(headers)


class FixturePatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "service"
        self.root.mkdir()
        self.snaps = Path(self._tmp.name) / "snapshots"
        _make_service(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _diff(self) -> str:
        return (
            "--- a/app/app.py\n+++ b/app/app.py\n"
            "@@ -5,1 +5,2 @@\n"
            "-# VULN: open(request.args.get('file'))\n"
            "+# PATCHED: use safe path\n"
        )

    def test_successful_patch_commits(self):
        wf = FixtureDefensivePatchWorkflow(
            self.root,
            self.snaps,
            team_id=1,
            checker_passes=True,
            exploit_blocked=True,
            patch_commits=True,
        )
        tx = wf.run("corr-001", self._diff())
        self.assertEqual(tx.status, "committed")
        self.assertTrue(tx.checker_passed_before)
        self.assertTrue(tx.checker_passed_after)
        self.assertTrue(tx.exploit_blocked)

    def test_checker_failure_before_patch_aborts(self):
        wf = FixtureDefensivePatchWorkflow(self.root, self.snaps, team_id=1, checker_passes=False)
        tx = wf.run("corr-002", self._diff())
        self.assertEqual(tx.status, "failed")
        self.assertFalse(tx.patch_applied)

    def test_rebuild_failure_rolls_back(self):
        wf = FixtureDefensivePatchWorkflow(
            self.root, self.snaps, team_id=1, checker_passes=True, rebuild_passes=False
        )
        tx = wf.run("corr-003", self._diff())
        self.assertEqual(tx.status, "failed")

    def test_exploit_not_blocked_rolls_back(self):
        wf = FixtureDefensivePatchWorkflow(
            self.root, self.snaps, team_id=1, checker_passes=True, exploit_blocked=False
        )
        tx = wf.run("corr-004", self._diff())
        self.assertEqual(tx.status, "failed")
        self.assertFalse(tx.exploit_blocked)

    def test_idempotent_for_same_correlation_id(self):
        wf = FixtureDefensivePatchWorkflow(
            self.root, self.snaps, team_id=1, checker_passes=True, exploit_blocked=True
        )
        tx1 = wf.run("corr-005", self._diff())
        tx2 = wf.run("corr-005", self._diff())
        self.assertEqual(tx1.transaction_id, tx2.transaction_id)
        self.assertEqual(tx1.status, tx2.status)

    def test_transaction_conflict_raises(self):
        """Two concurrent transactions for the same team should conflict."""
        registry = TransactionRegistry()
        # Instantiate workflows (unused directly — we test registry state)
        _wf1 = FixtureDefensivePatchWorkflow(self.root, self.snaps, team_id=2, registry=registry)  # noqa: F841
        _wf2 = FixtureDefensivePatchWorkflow(self.root, self.snaps, team_id=2, registry=registry)  # noqa: F841
        # Manually occupy the registry
        from bot_lib.defensive_patch import PatchTransaction
        from bot_lib.defensive_tools import create_snapshot

        snap = create_snapshot(self.root, self.snaps)
        tx = PatchTransaction(transaction_id="X", team_id=2, snapshot=snap, diff_text="x")
        registry.start(2, "X", tx)
        with self.assertRaises(TransactionConflictError):
            registry.start(2, "Y", tx)

    def test_empty_diff_fails(self):
        wf = FixtureDefensivePatchWorkflow(self.root, self.snaps, team_id=1)
        tx = wf.run("corr-006", "")
        self.assertEqual(tx.status, "failed")
        self.assertIn("empty", tx.error)

    def test_tx_as_dict_is_serializable(self):
        import json

        wf = FixtureDefensivePatchWorkflow(
            self.root, self.snaps, team_id=1, checker_passes=True, exploit_blocked=True
        )
        tx = wf.run("corr-007", self._diff())
        json.dumps(tx.as_dict())


if __name__ == "__main__":
    unittest.main()
