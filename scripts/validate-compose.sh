#!/usr/bin/env bash
# Validate every committed and generated Docker Compose variant.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v docker >/dev/null 2>&1 || {
    echo "validate-compose.sh: Docker CLI is required" >&2
    exit 1
}

mapfile -d '' compose_files < <(
    find "${ROOT}" \
        -path "${ROOT}/.git" -prune -o \
        -path "${ROOT}/visualizer/node_modules" -prune -o \
        -type f \
        \( \
            -name 'docker-compose*.yml' -o \
            -name 'docker-compose*.yaml' -o \
            -name 'compose*.yml' -o \
            -name 'compose*.yaml' \
        \) \
        -print0 | sort -z
)

if ((${#compose_files[@]} == 0)); then
    echo "validate-compose.sh: no Compose files found" >&2
    exit 1
fi

for compose_file in "${compose_files[@]}"; do
    relative_path="${compose_file#"${ROOT}"/}"
    echo "Validating ${relative_path}"
    docker compose -f "${compose_file}" config --quiet
done

echo "Validated ${#compose_files[@]} Compose file(s)."
