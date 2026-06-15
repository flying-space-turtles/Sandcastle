#!/usr/bin/env python3
"""Tests for AI-009: validator and AI-010: registry."""

from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import ChallengeSpec
from bot_lib.challenge_renderer import render
from challenge.validator import ChallengeValidator, ComposeSafetyError, check_compose_safety
from challenge.registry import ChallengeRegistry, PublicationError


def _render(seed: int = 42, vuln: str = "path_traversal") -> tuple[Path, str, str]:
    """Render a spec to a temp dir, return (staging_root, render_id, candidate_dir)."""
    tmp = tempfile.mkdtemp()
    staging = Path(tmp)
    spec = ChallengeSpec(seed=seed, vulnerability=vuln)
    c = render(spec, staging_root=staging)
    return staging, c.render_id, str(c.staging_dir)


class ComposeSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_compose(self, content: str) -> Path:
        d = self.staging / "cand"
        d.mkdir(exist_ok=True)
        (d / "docker-compose.yml").write_text(content)
        return d

    def test_clean_compose_passes(self):
        spec = ChallengeSpec(seed=1, vulnerability="path_traversal")
        c = render(spec, staging_root=self.staging)
        check_compose_safety(c.staging_dir)  # should not raise

    def test_privileged_rejected(self):
        d = self._write_compose("services:\n  app:\n    privileged: true\n")
        with self.assertRaises(ComposeSafetyError):
            check_compose_safety(d)

    def test_host_network_rejected(self):
        d = self._write_compose("services:\n  app:\n    network_mode: host\n")
        with self.assertRaises(ComposeSafetyError):
            check_compose_safety(d)

    def test_docker_socket_rejected(self):
        d = self._write_compose("volumes:\n  - /var/run/docker.sock:/var/run/docker.sock\n")
        with self.assertRaises(ComposeSafetyError):
            check_compose_safety(d)

    def _write_dockerfile(self, content: str) -> Path:
        d = self.staging / "cand2"
        d.mkdir(exist_ok=True)
        (d / "Dockerfile").write_text(content)
        return d

    def test_unapproved_image_rejected(self):
        d = self._write_dockerfile("FROM ubuntu:22.04\n")
        with self.assertRaises(ComposeSafetyError):
            check_compose_safety(d)

    def test_approved_image_passes(self):
        d = self._write_dockerfile("FROM python:3.12-slim\n")
        check_compose_safety(d)  # should not raise


class ValidatorFixtureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fixture_mode_returns_passed_report(self):
        spec = ChallengeSpec(seed=10, vulnerability="path_traversal")
        c = render(spec, staging_root=self.staging)
        validator = ChallengeValidator(docker=False)
        report = validator.validate(c.staging_dir, c.render_id, "test-digest")
        self.assertEqual(report.status, "passed")

    def test_report_as_dict_is_json_serializable(self):
        spec = ChallengeSpec(seed=11, vulnerability="sql_injection")
        c = render(spec, staging_root=self.staging)
        validator = ChallengeValidator(docker=False)
        report = validator.validate(c.staging_dir, c.render_id, "d")
        d = report.as_dict()
        json.dumps(d)  # must not raise

    def test_report_has_required_fields(self):
        spec = ChallengeSpec(seed=12, vulnerability="command_injection")
        c = render(spec, staging_root=self.staging)
        validator = ChallengeValidator(docker=False)
        report = validator.validate(c.staging_dir, c.render_id, "d")
        d = report.as_dict()
        for key in (
            "render_id",
            "status",
            "steps",
            "vulnerable_exploit_succeeded",
            "patched_exploit_failed",
            "checker_passed_before_patch",
            "checker_passed_after_patch",
            "artifact_digest",
            "created_at",
        ):
            self.assertIn(key, d, f"missing key: {key}")

    def test_fixture_sets_flags_true(self):
        spec = ChallengeSpec(seed=13, vulnerability="path_traversal")
        c = render(spec, staging_root=self.staging)
        validator = ChallengeValidator(docker=False)
        report = validator.validate(c.staging_dir, c.render_id, "d")
        self.assertTrue(report.vulnerable_exploit_succeeded)
        self.assertTrue(report.patched_exploit_failed)
        self.assertTrue(report.checker_passed_before_patch)
        self.assertTrue(report.checker_passed_after_patch)

    def test_missing_manifest_fails_validation(self):
        import os

        spec = ChallengeSpec(seed=14, vulnerability="path_traversal")
        c = render(spec, staging_root=self.staging)
        os.remove(c.staging_dir / "manifest.json")
        validator = ChallengeValidator(docker=False)
        report = validator.validate(c.staging_dir, c.render_id, "d")
        self.assertNotEqual(report.status, "passed")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name) / "staging"
        self.reg_root = Path(self._tmp.name) / "published"
        self.staging.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _render_and_validate(self, seed: int = 50, vuln: str = "path_traversal"):
        spec = ChallengeSpec(seed=seed, vulnerability=vuln)
        c = render(spec, staging_root=self.staging)
        v = ChallengeValidator(docker=False)
        report = v.validate(c.staging_dir, c.render_id, "d")
        return c, report

    def test_publish_passed_report_succeeds(self):
        c, report = self._render_and_validate(50)
        self.assertEqual(report.status, "passed")
        reg = ChallengeRegistry(self.reg_root)
        challenge_id = reg.publish(c.staging_dir, report.as_dict(), "run-001")
        self.assertIsNotNone(challenge_id)
        self.assertTrue((self.reg_root / challenge_id).exists())

    def test_publish_failed_report_raises(self):
        reg = ChallengeRegistry(self.reg_root)
        with self.assertRaises(PublicationError):
            reg.publish(self.staging, {"status": "failed", "render_id": "x"}, "run-x")

    def test_idempotent_publish(self):
        c, report = self._render_and_validate(51)
        reg = ChallengeRegistry(self.reg_root)
        id1 = reg.publish(c.staging_dir, report.as_dict(), "run-001")
        id2 = reg.publish(c.staging_dir, report.as_dict(), "run-001")
        self.assertEqual(id1, id2)

    def test_list_returns_published(self):
        c, report = self._render_and_validate(52, "sql_injection")
        reg = ChallengeRegistry(self.reg_root)
        challenge_id = reg.publish(c.staging_dir, report.as_dict(), "run-002")
        items = reg.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["challenge_id"], challenge_id)

    def test_inspect_returns_metadata(self):
        c, report = self._render_and_validate(53, "command_injection")
        reg = ChallengeRegistry(self.reg_root)
        cid = reg.publish(c.staging_dir, report.as_dict(), "run-003")
        info = reg.inspect(cid)
        self.assertEqual(info["challenge_id"], cid)
        self.assertIn("published_at", info)
        self.assertIn("source_digest", info)

    def test_inspect_missing_raises(self):
        reg = ChallengeRegistry(self.reg_root)
        with self.assertRaises(KeyError):
            reg.inspect("nonexistent-id")

    def test_source_path_exists(self):
        c, report = self._render_and_validate(54)
        reg = ChallengeRegistry(self.reg_root)
        cid = reg.publish(c.staging_dir, report.as_dict(), "run-004")
        src = reg.get_source_path(cid)
        self.assertTrue(src.exists())
        self.assertTrue((src / "app" / "app.py").exists())

    def test_delete_unreferenced_skips_in_use(self):
        c, report = self._render_and_validate(55)
        reg = ChallengeRegistry(self.reg_root)
        cid = reg.publish(c.staging_dir, report.as_dict(), "run-005")
        deleted = reg.delete_unreferenced({cid})
        self.assertNotIn(cid, deleted)
        self.assertTrue((self.reg_root / cid).exists())


if __name__ == "__main__":
    unittest.main()
