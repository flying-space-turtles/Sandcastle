"""AI-010: Challenge publication registry and arena selection support."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_REGISTRY_ROOT = Path(__file__).resolve().parents[3] / "challenges" / "published"


@dataclass
class PublishedChallenge:
    challenge_id: str
    published_at: str
    vulnerability: str
    difficulty: str
    seed: int
    template_version: str
    agent_run_id: str
    source_digest: str
    validation_digest: str


class PublicationError(ValueError):
    """Raised when publication is rejected."""


class ChallengeRegistry:
    def __init__(self, registry_root: Path | None = None) -> None:
        self.root = (registry_root or _DEFAULT_REGISTRY_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def publish(
        self,
        candidate_dir: Path,
        validation_report: dict[str, Any],
        agent_run_id: str,
    ) -> str:
        """Atomically publish a validated candidate to the registry.

        Returns the stable challenge_id (idempotent for same render_id).
        Raises PublicationError if validation did not pass or digest mismatches.
        """
        if validation_report.get("status") != "passed":
            raise PublicationError(
                f"cannot publish: validation status is {validation_report.get('status')!r}"
            )

        render_id = validation_report.get("render_id", "")
        if not render_id:
            raise PublicationError("validation report missing render_id")

        # Verify source digest matches the candidate directory
        reported_digest = validation_report.get("artifact_digest", "")
        actual_digest = self._dir_digest(candidate_dir)
        if reported_digest and reported_digest != actual_digest:
            raise PublicationError(
                f"source digest mismatch: report has {reported_digest!r}, "
                f"candidate dir has {actual_digest!r}"
            )

        challenge_id = render_id
        dest = self.root / challenge_id

        # Idempotent: if already published, return existing ID
        if dest.exists() and (dest / "manifest.json").exists():
            return challenge_id

        # Atomic copy: write to temp dir, then rename
        tmp = self.root / f".tmp-{challenge_id}"
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(str(candidate_dir), str(tmp))

        # Read spec from manifest
        manifest_path = tmp / "manifest.json"
        spec_data = json.loads(manifest_path.read_text()).get("spec", {})

        # Write registry manifest (immutable metadata)
        registry_manifest = {
            "challenge_id": challenge_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "vulnerability": spec_data.get("vulnerability", ""),
            "difficulty": spec_data.get("difficulty", "easy"),
            "seed": spec_data.get("seed", 0),
            "template_version": spec_data.get("template_version", ""),
            "agent_run_id": agent_run_id,
            "source_digest": actual_digest,
            "validation_digest": hashlib.sha256(
                json.dumps(validation_report, sort_keys=True).encode()
            ).hexdigest(),
        }
        (tmp / "registry_manifest.json").write_text(
            json.dumps(registry_manifest, indent=2, sort_keys=True)
        )
        (tmp / "validation_report.json").write_text(
            json.dumps(validation_report, indent=2, sort_keys=True)
        )

        # Atomic rename
        tmp.rename(dest)
        return challenge_id

    # ------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        results = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            rm = entry / "registry_manifest.json"
            if rm.exists():
                data = json.loads(rm.read_text())
                results.append(
                    {
                        "challenge_id": data.get("challenge_id", entry.name),
                        "published_at": data.get("published_at", ""),
                        "vulnerability": data.get("vulnerability", ""),
                        "difficulty": data.get("difficulty", ""),
                    }
                )
        return results

    # ------------------------------------------------------------------
    def inspect(self, challenge_id: str) -> dict[str, Any]:
        dest = self.root / challenge_id
        rm = dest / "registry_manifest.json"
        if not rm.exists():
            raise KeyError(f"challenge not found: {challenge_id!r}")
        return json.loads(rm.read_text())

    # ------------------------------------------------------------------
    def get_source_path(self, challenge_id: str) -> Path:
        dest = self.root / challenge_id
        if not (dest / "registry_manifest.json").exists():
            raise KeyError(f"challenge not found: {challenge_id!r}")
        return dest

    # ------------------------------------------------------------------
    def delete_unreferenced(self, challenge_ids_in_use: set[str]) -> list[str]:
        """Delete published challenges not referenced by any arena config.

        Never deletes a challenge whose ID is in challenge_ids_in_use.
        """
        deleted = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in challenge_ids_in_use:
                continue
            rm = entry / "registry_manifest.json"
            if rm.exists():
                shutil.rmtree(str(entry))
                deleted.append(entry.name)
        return deleted

    # ------------------------------------------------------------------
    @staticmethod
    def _dir_digest(directory: Path) -> str:
        h = hashlib.sha256()
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.name not in (
                "manifest.json",
                "registry_manifest.json",
                "validation_report.json",
            ):
                h.update(path.read_bytes())
        return h.hexdigest()
