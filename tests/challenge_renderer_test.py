#!/usr/bin/env python3
"""Tests for AI-008: ChallengeSpec validation and renderer determinism."""
from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.agent_contracts import ChallengeSpec
from bot_lib.challenge_renderer import render, RenderedCandidate


def _spec(**kwargs) -> ChallengeSpec:
    defaults = {"seed": 42, "vulnerability": "path_traversal"}
    return ChallengeSpec(**{**defaults, **kwargs})


class ChallengeSpecValidationTests(unittest.TestCase):
    def test_valid_spec_creates_successfully(self):
        s = _spec(seed=0, vulnerability="path_traversal")
        self.assertEqual(s.vulnerability, "path_traversal")

    def test_all_three_vulnerabilities_accepted(self):
        for v in ("path_traversal", "command_injection", "sql_injection"):
            _spec(vulnerability=v)

    def test_unsupported_vulnerability_raises(self):
        with self.assertRaises(ValueError):
            _spec(vulnerability="buffer_overflow")

    def test_bad_seed_raises(self):
        with self.assertRaises(ValueError):
            _spec(seed=-1)

    def test_bad_service_name_raises(self):
        with self.assertRaises(ValueError):
            _spec(service_name="My Service!")

    def test_too_many_decoys_raises(self):
        with self.assertRaises(ValueError):
            _spec(decoy_endpoints=6)

    def test_bad_difficulty_raises(self):
        with self.assertRaises(ValueError):
            _spec(difficulty="impossible")

    def test_as_dict_round_trips_through_json(self):
        import json
        s = _spec()
        d = s.as_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_as_dict_deterministic(self):
        s1 = _spec()
        s2 = _spec()
        self.assertEqual(s1.as_dict(), s2.as_dict())


class RendererTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_renders_without_error(self):
        c = render(_spec(), staging_root=self.staging)
        self.assertIsInstance(c, RenderedCandidate)

    def test_render_id_is_deterministic(self):
        a = render(_spec(), staging_root=self.staging)
        b = render(_spec(), staging_root=self.staging)
        self.assertEqual(a.render_id, b.render_id)

    def test_different_seeds_produce_different_ids(self):
        a = render(_spec(seed=1), staging_root=self.staging)
        b = render(_spec(seed=2), staging_root=self.staging)
        self.assertNotEqual(a.render_id, b.render_id)

    def test_golden_files_byte_identical_same_spec(self):
        a = render(_spec(seed=99), staging_root=self.staging)
        b = render(_spec(seed=99), staging_root=self.staging)
        for rel in a.file_digests:
            if rel == "manifest.json":
                continue
            self.assertEqual(a.file_digests[rel], b.file_digests[rel], f"digest mismatch: {rel}")

    def test_different_seeds_differ_in_at_least_one_file(self):
        a = render(_spec(seed=7), staging_root=self.staging)
        b = render(_spec(seed=8), staging_root=self.staging)
        shared = set(a.file_digests) & set(b.file_digests) - {"manifest.json"}
        diffs = sum(1 for k in shared if a.file_digests[k] != b.file_digests[k])
        self.assertGreater(diffs, 0)

    def test_manifest_contains_digests(self):
        c = render(_spec(), staging_root=self.staging)
        import json
        manifest = json.loads((c.staging_dir / "manifest.json").read_text())
        self.assertIn("file_digests", manifest)
        self.assertGreater(len(manifest["file_digests"]), 0)

    def test_dockerfile_has_no_privileged(self):
        c = render(_spec(), staging_root=self.staging)
        dockerfile = (c.staging_dir / "Dockerfile").read_text()
        self.assertNotIn("privileged", dockerfile.lower())

    def test_compose_has_no_host_network(self):
        c = render(_spec(), staging_root=self.staging)
        compose = (c.staging_dir / "docker-compose.yml").read_text()
        self.assertNotIn("network_mode: host", compose)

    def test_compose_has_no_docker_socket(self):
        c = render(_spec(), staging_root=self.staging)
        compose = (c.staging_dir / "docker-compose.yml").read_text()
        self.assertNotIn("docker.sock", compose)

    def test_exploit_file_exists_for_each_vuln(self):
        for vuln in ("path_traversal", "command_injection", "sql_injection"):
            c = render(_spec(vulnerability=vuln), staging_root=self.staging)
            exploit = c.staging_dir / "exploits" / f"exploit_{vuln}.py"
            self.assertTrue(exploit.exists(), f"exploit missing for {vuln}")

    def test_patch_file_exists_for_each_vuln(self):
        for vuln in ("path_traversal", "command_injection", "sql_injection"):
            c = render(_spec(vulnerability=vuln), staging_root=self.staging)
            patch = c.staging_dir / "patches" / f"patch_{vuln}.diff"
            self.assertTrue(patch.exists(), f"patch missing for {vuln}")

    def test_checker_file_exists(self):
        c = render(_spec(), staging_root=self.staging)
        self.assertTrue((c.staging_dir / "checker.py").exists())

    def test_no_provider_keys_in_any_file(self):
        import re
        c = render(_spec(), staging_root=self.staging)
        # Real OpenAI keys look like sk-proj-... or sk-...(48+ chars)
        key_pattern = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
        gemini_pattern = re.compile(r"AIza[A-Za-z0-9_-]{30,}")
        for path in c.staging_dir.rglob("*.py"):
            text = path.read_text()
            for secret in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
                self.assertNotIn(secret, text, f"{secret} found in {path.name}")
            self.assertIsNone(key_pattern.search(text), f"OpenAI key pattern in {path.name}")
            self.assertIsNone(gemini_pattern.search(text), f"Gemini key pattern in {path.name}")

    def test_decoy_endpoints_rendered(self):
        c = render(_spec(decoy_endpoints=2), staging_root=self.staging)
        app = (c.staging_dir / "app" / "app.py").read_text()
        self.assertIn("decoy_", app)


if __name__ == "__main__":
    unittest.main()
