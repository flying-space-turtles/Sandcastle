#!/usr/bin/env python3
"""Compatibility wrapper for the canonical setup script.

`scripts/setup.sh` owns team directory creation, per-team Dockerfiles, service
copies, and docker-compose.yml generation. This wrapper exists so older CI and
developer commands that call `python3 scripts/gen_compose.py N` still do the
right thing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    args = ["--teams", sys.argv[1]] if len(sys.argv) > 1 else []
    return subprocess.call(["bash", str(REPO_ROOT / "scripts" / "setup.sh"), *args])


if __name__ == "__main__":
    raise SystemExit(main())
