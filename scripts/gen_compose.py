#!/usr/bin/env python3
"""Compatibility wrapper for the root setup script.

The full setup flow now creates team directories, per-team service copies,
per-team SSH Dockerfiles, and the root compose file. Keep this script for older
commands such as:

    python3 scripts/gen_compose.py 4
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
