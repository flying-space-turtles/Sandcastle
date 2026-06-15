"""AI-012: Authenticated team-local defensive source and service tools.

Provides bounded, path-safe operations for AttackDefenseAgent:
  - source file listing / reading / searching
  - snapshot creation
  - diff apply / validate
  - rebuild / checker / exploit-regression
  - snapshot restore and patch transaction management

No arbitrary shell execution. All paths normalized and constrained.
Authenticated by a per-deployment token in addition to source-IP checks.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("defensive_tools")

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

# File extensions that may be read or patched
_ALLOWED_EXTENSIONS = {
    ".py",
    ".txt",
    ".md",
    ".cfg",
    ".ini",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".html",
    ".css",
    ".js",
    ".sh",
    ".env.example",
    ".requirements",
}

# Paths that must never be accessed even inside the service root
_FORBIDDEN_NAMES = {
    ".env",
    ".git",
    "__pycache__",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    "*.key",
    "*.pem",
    "*.crt",
}

_MAX_FILE_BYTES = 32_768  # 32 KB per file read
_MAX_SEARCH_RESULTS = 50  # lines returned from search
_MAX_PATCH_FILES = 10  # changed files per patch
_MAX_PATCH_BYTES = 65_536  # 64 KB unified diff size limit
_MAX_OUTPUT_BYTES = 8_192  # per subprocess call
_STEP_TIMEOUT = 30  # seconds per build/checker step
_MAX_TRANSACTIONS = 1  # one active patch transaction per team


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DefensiveToolError(ValueError):
    """Raised when a tool call is rejected for safety or policy reasons."""


class TransactionConflictError(DefensiveToolError):
    """Raised when a second patch transaction is attempted while one is active."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _safe_path(service_root: Path, relative: str) -> Path:
    """Return an absolute path inside service_root or raise DefensiveToolError."""
    # Normalize: remove leading /, ./, and resolve any .. sequences
    clean = relative.lstrip("/").replace("\\", "/")
    candidate = (service_root / clean).resolve()
    if not str(candidate).startswith(str(service_root.resolve())):
        raise DefensiveToolError(f"path traversal rejected: {relative!r} escapes service root")
    # Check forbidden name patterns
    for part in candidate.parts:
        for pattern in _FORBIDDEN_NAMES:
            if fnmatch.fnmatch(part, pattern):
                raise DefensiveToolError(f"access to {part!r} is forbidden")
    return candidate


def _allowed_extension(path: Path) -> bool:
    return path.suffix.lower() in _ALLOWED_EXTENSIONS or path.suffix == ""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class SourceSnapshot:
    snapshot_id: str
    service_root: Path
    snapshot_dir: Path
    file_count: int
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "service_root": str(self.service_root),
            "file_count": self.file_count,
            "digest": self.digest,
        }


def create_snapshot(service_root: Path, snapshots_root: Path) -> SourceSnapshot:
    """Copy the service source tree to a versioned snapshot directory."""
    service_root = service_root.resolve()
    snapshots_root.mkdir(parents=True, exist_ok=True)

    # Derive snapshot_id from directory digest (deterministic for same content)
    h = hashlib.sha256()
    file_count = 0
    for path in sorted(service_root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(service_root))
            if any(fnmatch.fnmatch(p, pat) for p in rel.split("/") for pat in _FORBIDDEN_NAMES):
                continue
            h.update(rel.encode())
            h.update(path.read_bytes())
            file_count += 1

    snapshot_id = h.hexdigest()[:16]
    snapshot_dir = snapshots_root / snapshot_id
    if not snapshot_dir.exists():
        shutil.copytree(
            str(service_root),
            str(snapshot_dir),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".git"),
        )

    return SourceSnapshot(
        snapshot_id=snapshot_id,
        service_root=service_root,
        snapshot_dir=snapshot_dir,
        file_count=file_count,
        digest=h.hexdigest(),
    )


def restore_snapshot(snapshot: SourceSnapshot) -> None:
    """Restore service_root to the snapshot state (destructive)."""
    service_root = snapshot.service_root.resolve()
    if not snapshot.snapshot_dir.exists():
        raise DefensiveToolError(f"snapshot directory missing: {snapshot.snapshot_dir}")
    # Clear existing source (keep the directory)
    for item in service_root.iterdir():
        if item.is_dir():
            shutil.rmtree(str(item))
        else:
            item.unlink()
    # Copy snapshot back
    for item in snapshot.snapshot_dir.iterdir():
        dest = service_root / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(dest))
        else:
            shutil.copy2(str(item), str(dest))


# ---------------------------------------------------------------------------
# Source operations
# ---------------------------------------------------------------------------


def list_allowed_files(service_root: Path) -> list[str]:
    """Return relative paths of all readable, non-forbidden source files."""
    service_root = service_root.resolve()
    result = []
    for path in sorted(service_root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(service_root))
        parts = rel.split("/")
        if any(fnmatch.fnmatch(p, pat) for p in parts for pat in _FORBIDDEN_NAMES):
            continue
        if not _allowed_extension(path):
            continue
        result.append(rel)
    return result


def read_file_range(
    service_root: Path,
    relative: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> str:
    """Return bounded text from a source file (max _MAX_FILE_BYTES)."""
    target = _safe_path(service_root, relative)
    if not target.exists():
        raise DefensiveToolError(f"file not found: {relative!r}")
    if not _allowed_extension(target):
        raise DefensiveToolError(f"file extension not allowed: {target.suffix}")
    raw = target.read_bytes()[:_MAX_FILE_BYTES]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    s = max(0, start_line - 1)
    e = end_line if end_line is not None else len(lines)
    return "".join(lines[s:e])


def search_source(
    service_root: Path,
    pattern: str,
    *,
    literal: bool = True,
    file_glob: str = "*.py",
) -> list[dict[str, Any]]:
    """Search source files for a pattern (literal or regex).

    Returns at most _MAX_SEARCH_RESULTS matching line records.
    """
    service_root = service_root.resolve()
    if not literal:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise DefensiveToolError(f"invalid search pattern: {exc}") from exc
    else:
        compiled = None

    results: list[dict[str, Any]] = []
    for path in sorted(service_root.rglob(file_glob)):
        if not path.is_file():
            continue
        rel = str(path.relative_to(service_root))
        if any(fnmatch.fnmatch(p, pat) for p in rel.split("/") for pat in _FORBIDDEN_NAMES):
            continue
        try:
            text = path.read_bytes()[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            matched = (compiled.search(line) is not None) if compiled else (pattern in line)
            if matched:
                results.append({"file": rel, "line": i, "content": line[:500]})
                if len(results) >= _MAX_SEARCH_RESULTS:
                    return results
    return results


# ---------------------------------------------------------------------------
# Diff / patch operations
# ---------------------------------------------------------------------------


def validate_diff(diff_text: str) -> None:
    """Basic structural validation of a unified diff before apply.

    Raises DefensiveToolError if the diff is oversized, empty, or
    touches too many files.
    """
    if not diff_text or not diff_text.strip():
        raise DefensiveToolError("diff is empty")
    if len(diff_text.encode()) > _MAX_PATCH_BYTES:
        raise DefensiveToolError(f"diff exceeds max size ({_MAX_PATCH_BYTES} bytes)")
    file_headers = [line for line in diff_text.splitlines() if line.startswith("--- ")]
    if len(file_headers) > _MAX_PATCH_FILES:
        raise DefensiveToolError(
            f"diff touches {len(file_headers)} files, max is {_MAX_PATCH_FILES}"
        )


def apply_diff(service_root: Path, diff_text: str) -> str:
    """Apply a unified diff to the service root.

    Returns a bounded output string from the patch tool.
    Raises DefensiveToolError on failure.
    """
    validate_diff(diff_text)
    service_root = service_root.resolve()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(diff_text)
        patch_file = f.name

    try:
        result = subprocess.run(
            ["patch", "-p1", "--input", patch_file, "--forward"],
            cwd=str(service_root),
            capture_output=True,
            text=True,
            timeout=_STEP_TIMEOUT,
        )
        output = (result.stdout + result.stderr)[:_MAX_OUTPUT_BYTES]
        if result.returncode != 0:
            raise DefensiveToolError(f"patch failed:\n{output}")
        return output
    finally:
        os.unlink(patch_file)


def show_diff(service_root: Path, snapshot: SourceSnapshot) -> str:
    """Return a unified diff between the snapshot and the current service root."""
    service_root = service_root.resolve()
    lines: list[str] = []
    snapshot_files = set(
        str(p.relative_to(snapshot.snapshot_dir))
        for p in snapshot.snapshot_dir.rglob("*")
        if p.is_file()
    )
    current_files = set(
        str(p.relative_to(service_root)) for p in service_root.rglob("*") if p.is_file()
    )
    for rel in sorted(snapshot_files | current_files):
        a_path = snapshot.snapshot_dir / rel
        b_path = service_root / rel
        a_lines = (
            a_path.read_text(errors="replace").splitlines(keepends=True) if a_path.exists() else []
        )
        b_lines = (
            b_path.read_text(errors="replace").splitlines(keepends=True) if b_path.exists() else []
        )
        lines.extend(
            difflib.unified_diff(
                a_lines,
                b_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
    result = "".join(lines)[:_MAX_OUTPUT_BYTES]
    return result


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------


def _run_subprocess(args: list[str], cwd: str, timeout: int = _STEP_TIMEOUT) -> tuple[int, str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout + result.stderr)[:_MAX_OUTPUT_BYTES]
    return result.returncode, output


def rebuild_service(service_root: Path, compose_project: str) -> tuple[bool, str]:
    """Rebuild the own-team app container. Returns (success, output)."""
    code, out = _run_subprocess(
        ["docker", "compose", "-p", compose_project, "build", "--no-cache"],
        cwd=str(service_root),
        timeout=120,
    )
    return code == 0, out


def restart_service(service_root: Path, compose_project: str) -> tuple[bool, str]:
    """Restart the own-team service. Returns (success, output)."""
    code, out = _run_subprocess(
        ["docker", "compose", "-p", compose_project, "up", "-d", "--force-recreate"],
        cwd=str(service_root),
    )
    return code == 0, out


def run_checker(checker_path: Path, host: str, port: int) -> tuple[bool, str]:
    """Run the own-team service checker. Returns (success, output)."""
    if not checker_path.exists():
        return False, "checker not found"
    code, out = _run_subprocess(
        ["python3", str(checker_path), "check", host, str(port)],
        cwd=str(checker_path.parent),
    )
    return code == 0, out


def run_own_exploit(exploit_path: Path, host: str, port: int) -> tuple[bool, str]:
    """Run a registered reference exploit against the own-team service.

    Returns (exploit_succeeded, output). The agent should use this to verify
    that a patch *blocks* exploitation — success here means the patch failed.
    """
    if not exploit_path.exists():
        return False, "exploit not found"
    code, out = _run_subprocess(
        ["python3", str(exploit_path), host, str(port)],
        cwd=str(exploit_path.parent),
    )
    return code == 0, out


# ---------------------------------------------------------------------------
# Patch transaction
# ---------------------------------------------------------------------------


@dataclass
class PatchTransaction:
    """A bounded patch transaction for the own-team service.

    Lifecycle: open → apply → commit or rollback.
    Only one transaction may be active at a time per team.
    """

    transaction_id: str
    team_id: int
    snapshot: SourceSnapshot
    diff_text: str
    status: str = "open"  # open | committed | rolled_back | failed
    patch_output: str = ""
    checker_output_before: str = ""
    checker_output_after: str = ""
    exploit_output_after: str = ""
    error: str = ""
    patch_applied: bool = False
    checker_passed_before: bool = False
    checker_passed_after: bool = False
    exploit_blocked: bool = False
    changed_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "team_id": self.team_id,
            "snapshot_id": self.snapshot.snapshot_id,
            "status": self.status,
            "patch_applied": self.patch_applied,
            "checker_passed_before": self.checker_passed_before,
            "checker_passed_after": self.checker_passed_after,
            "exploit_blocked": self.exploit_blocked,
            "changed_files": self.changed_files,
            "error": self.error,
        }
