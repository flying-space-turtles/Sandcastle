#!/usr/bin/env bash
# Compatibility wrapper for the unified arena lifecycle command.

set -euo pipefail

ROOT="${SANDCASTLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
exec "${ROOT}/scripts/arena.sh" up "$@"
