#!/usr/bin/env bash
# Compatibility wrapper: reset app data, preserve source, and restart.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
exec "${ROOT}/scripts/arena.sh" reset "$@"
