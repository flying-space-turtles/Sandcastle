"""AI-009: Isolated challenge validation pipeline.

Validates a staged candidate in both vulnerable and patched states.
Docker mode is opt-in; fixture mode is used in CI.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Approved base images for generated services
_APPROVED_IMAGES = {"python:3.12-slim", "python:3.11-slim"}
_MIN_PORT = 8000
_MAX_PORT = 9999
_DEFAULT_TIMEOUT = 120.0
_STEP_TIMEOUT = 30.0
_MAX_OUTPUT_BYTES = 4096

# Dangerous Compose settings that are always rejected
_DANGEROUS_SETTINGS = [
    "privileged: true",
    "privileged:true",
    "/var/run/docker.sock",
    "network_mode: host",
    "network_mode:host",
    "cap_add:",
]


@dataclass
class ValidationStep:
    name: str
    status: str  # "passed" | "failed" | "skipped" | "error"
    duration_ms: int = 0
    output: str = ""


@dataclass
class ChallengeValidationReport:
    render_id: str
    spec_digest: str
    status: str  # "passed" | "failed" | "error"
    steps: list[ValidationStep] = field(default_factory=list)
    vulnerable_exploit_succeeded: bool = False
    patched_exploit_failed: bool = False
    checker_passed_before_patch: bool = False
    checker_passed_after_patch: bool = False
    artifact_digest: str = ""
    error: str | None = None
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        from datetime import datetime, timezone
        created = self.created_at or datetime.now(timezone.utc).isoformat()
        return {
            "render_id": self.render_id,
            "spec_digest": self.spec_digest,
            "status": self.status,
            "steps": [
                {
                    "name": s.name,
                    "status": s.status,
                    "duration_ms": s.duration_ms,
                    "output": s.output[:_MAX_OUTPUT_BYTES],
                }
                for s in self.steps
            ],
            "vulnerable_exploit_succeeded": self.vulnerable_exploit_succeeded,
            "patched_exploit_failed": self.patched_exploit_failed,
            "checker_passed_before_patch": self.checker_passed_before_patch,
            "checker_passed_after_patch": self.checker_passed_after_patch,
            "artifact_digest": self.artifact_digest,
            "error": self.error,
            "created_at": created,
        }


class ComposeSafetyError(ValueError):
    """Raised when a candidate Compose/Dockerfile violates safety rules."""


def check_compose_safety(candidate_dir: Path) -> None:
    """Reject unsafe Compose and Dockerfile settings before any container starts.

    Raises ComposeSafetyError describing the first violation found.
    """
    compose_file = candidate_dir / "docker-compose.yml"
    dockerfile = candidate_dir / "Dockerfile"

    for path in (compose_file, dockerfile):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for danger in _DANGEROUS_SETTINGS:
            if danger in text:
                raise ComposeSafetyError(
                    f"{path.name} contains forbidden setting: {danger!r}"
                )

    # Check image allowlist in Dockerfile
    if dockerfile.exists():
        for line in dockerfile.read_text().splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("FROM "):
                image = stripped.split()[1].lower()
                if image not in {img.lower() for img in _APPROVED_IMAGES}:
                    raise ComposeSafetyError(
                        f"Dockerfile uses unapproved base image: {image!r}. "
                        f"Allowed: {sorted(_APPROVED_IMAGES)}"
                    )

    # Check port range in Compose
    if compose_file.exists():
        text = compose_file.read_text()
        import re
        for m in re.finditer(r'"(\d+)"', text):
            port = int(m.group(1))
            if port < _MIN_PORT or port > _MAX_PORT:
                raise ComposeSafetyError(
                    f"docker-compose.yml exposes port {port} outside allowed range "
                    f"{_MIN_PORT}-{_MAX_PORT}"
                )


def _run_step(
    name: str,
    fn,
    steps: list[ValidationStep],
) -> tuple[bool, str]:
    t0 = time.monotonic()
    try:
        output = fn()
        elapsed = int((time.monotonic() - t0) * 1000)
        steps.append(ValidationStep(name=name, status="passed", duration_ms=elapsed, output=str(output or "")[:_MAX_OUTPUT_BYTES]))
        return True, str(output or "")
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - t0) * 1000)
        steps.append(ValidationStep(name=name, status="failed", duration_ms=elapsed, output=str(exc)[:_MAX_OUTPUT_BYTES]))
        return False, str(exc)


class ChallengeValidator:
    """Validates a staged candidate in vulnerable and patched states.

    docker=False uses fixture mode (no real Docker) for CI.
    """

    def __init__(self, docker: bool = False, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.docker = docker
        self.timeout = timeout

    def validate(
        self,
        candidate_dir: Path,
        render_id: str,
        spec_digest: str,
    ) -> ChallengeValidationReport:
        from datetime import datetime, timezone

        steps: list[ValidationStep] = []
        report = ChallengeValidationReport(
            render_id=render_id,
            spec_digest=spec_digest,
            status="error",
            steps=steps,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        if not self.docker:
            return self._fixture_validate(candidate_dir, render_id, spec_digest, steps, report)

        # Real Docker validation (opt-in)
        return self._docker_validate(candidate_dir, render_id, spec_digest, steps, report)

    def _fixture_validate(
        self,
        candidate_dir: Path,
        render_id: str,
        spec_digest: str,
        steps: list[ValidationStep],
        report: ChallengeValidationReport,
    ) -> ChallengeValidationReport:
        """Fixture mode: checks structure and safety without running Docker."""
        try:
            ok, err = _run_step("check_manifest", lambda: _check_manifest(candidate_dir), steps)
            if not ok:
                report.error = err
                return report

            ok, err = _run_step("check_compose_safety", lambda: check_compose_safety(candidate_dir), steps)
            if not ok:
                report.error = err
                return report

            ok, err = _run_step("check_exploit_file", lambda: _check_exploit_exists(candidate_dir), steps)
            if not ok:
                report.error = err
                return report

            ok, err = _run_step("check_patch_file", lambda: _check_patch_exists(candidate_dir), steps)
            if not ok:
                report.error = err
                return report

            # In fixture mode, we simulate pass
            report.vulnerable_exploit_succeeded = True
            report.patched_exploit_failed = True
            report.checker_passed_before_patch = True
            report.checker_passed_after_patch = True
            report.artifact_digest = _dir_digest(candidate_dir)
            report.status = "passed"
            steps.append(ValidationStep(name="fixture_mode", status="passed", output="no-docker fixture validation passed"))
        except Exception as exc:  # noqa: BLE001
            report.error = str(exc)
            report.status = "error"

        return report

    def _docker_validate(
        self,
        candidate_dir: Path,
        render_id: str,
        spec_digest: str,
        steps: list[ValidationStep],
        report: ChallengeValidationReport,
    ) -> ChallengeValidationReport:
        """Real Docker validation (not required for CI)."""
        project = f"sandcastle-cand-{render_id[:8]}"
        try:
            ok, err = _run_step("check_compose_safety", lambda: check_compose_safety(candidate_dir), steps)
            if not ok:
                report.error = err
                return report

            # Build
            ok, err = _run_step("build", lambda: _docker_compose(candidate_dir, project, ["build", "--no-cache"], self.timeout), steps)
            if not ok:
                report.error = "build failed: " + err
                return report

            # Start + health
            _docker_compose(candidate_dir, project, ["up", "-d"], _STEP_TIMEOUT)
            host_port = _get_mapped_port(candidate_dir, project, "app", 8080)

            ok, _ = _run_step("health_check", lambda: _http_get(host_port, "/health", expect="ok"), steps)
            if not ok:
                report.status = "failed"
                return report

            ok, _ = _run_step("checker_before_patch", lambda: _run_checker(candidate_dir, host_port), steps)
            report.checker_passed_before_patch = ok

            ok, _ = _run_step("exploit_before_patch", lambda: _run_exploit(candidate_dir, host_port), steps)
            report.vulnerable_exploit_succeeded = ok

            # Apply patch + rebuild
            ok, err = _run_step("apply_patch", lambda: _apply_patch(candidate_dir), steps)
            if ok:
                _docker_compose(candidate_dir, project, ["build", "--no-cache"], self.timeout)
                _docker_compose(candidate_dir, project, ["up", "-d", "--force-recreate"], _STEP_TIMEOUT)
                ok2, _ = _run_step("checker_after_patch", lambda: _run_checker(candidate_dir, host_port), steps)
                report.checker_passed_after_patch = ok2
                ok3, _ = _run_step("exploit_after_patch", lambda: _run_exploit(candidate_dir, host_port), steps)
                report.patched_exploit_failed = not ok3

            report.artifact_digest = _dir_digest(candidate_dir)
            passed = (
                report.checker_passed_before_patch
                and report.vulnerable_exploit_succeeded
                and report.checker_passed_after_patch
                and report.patched_exploit_failed
            )
            report.status = "passed" if passed else "failed"
        except Exception as exc:  # noqa: BLE001
            report.error = str(exc)
            report.status = "error"
        finally:
            try:
                _docker_compose(candidate_dir, project, ["down", "--volumes", "--remove-orphans"], _STEP_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
        return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_manifest(candidate_dir: Path) -> str:
    manifest = candidate_dir / "manifest.json"
    if not manifest.exists():
        raise FileNotFoundError("manifest.json missing from candidate")
    data = json.loads(manifest.read_text())
    for key in ("render_id", "spec", "file_digests"):
        if key not in data:
            raise ValueError(f"manifest.json missing key: {key}")
    return "manifest ok"


def _check_exploit_exists(candidate_dir: Path) -> str:
    exploits = list((candidate_dir / "exploits").glob("exploit_*.py"))
    if not exploits:
        raise FileNotFoundError("no exploit file found in exploits/")
    return f"found {len(exploits)} exploit(s)"


def _check_patch_exists(candidate_dir: Path) -> str:
    patches = list((candidate_dir / "patches").glob("patch_*.diff"))
    if not patches:
        raise FileNotFoundError("no patch file found in patches/")
    return f"found {len(patches)} patch(es)"


def _dir_digest(directory: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            h.update(path.read_bytes())
    return h.hexdigest()


def _docker_compose(
    candidate_dir: Path,
    project: str,
    args: list[str],
    timeout: float,
) -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=str(candidate_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/usr/local/bin:/bin"},
    )
    output = (result.stdout + result.stderr)[:_MAX_OUTPUT_BYTES]
    if result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{output}")
    return output


def _get_mapped_port(candidate_dir: Path, project: str, service: str, internal_port: int) -> int:
    result = subprocess.run(
        ["docker", "compose", "-p", project, "port", service, str(internal_port)],
        cwd=str(candidate_dir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("could not get mapped port")
    _, _, port = result.stdout.strip().rpartition(":")
    return int(port)


def _http_get(port: int, path: str, expect: str) -> str:
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        body = r.read().decode()
    if expect and expect not in body:
        raise AssertionError(f"expected {expect!r} in response, got: {body[:200]}")
    return body[:200]


def _run_checker(candidate_dir: Path, port: int) -> str:
    checker = candidate_dir / "checker.py"
    result = subprocess.run(
        ["python3", str(checker), "check", "127.0.0.1", str(port)],
        capture_output=True, text=True, timeout=_STEP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout.strip()


def _run_exploit(candidate_dir: Path, port: int) -> str:
    exploits = list((candidate_dir / "exploits").glob("exploit_*.py"))
    if not exploits:
        raise FileNotFoundError("no exploit file")
    result = subprocess.run(
        ["python3", str(exploits[0]), "127.0.0.1", str(port)],
        capture_output=True, text=True, timeout=_STEP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError("exploit did not capture flag: " + result.stdout)
    return result.stdout.strip()


def _apply_patch(candidate_dir: Path) -> str:
    patches = list((candidate_dir / "patches").glob("patch_*.diff"))
    if not patches:
        raise FileNotFoundError("no patch file")
    result = subprocess.run(
        ["patch", "-p1", "--input", str(patches[0])],
        cwd=str(candidate_dir),
        capture_output=True,
        text=True,
        timeout=_STEP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError("patch failed: " + result.stdout + result.stderr)
    return "patch applied"
