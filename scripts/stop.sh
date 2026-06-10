#!/usr/bin/env bash
# Compatibility wrapper: stop containers, preserve source and data.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
exec "${ROOT}/scripts/arena.sh" down "$@"
