#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../../.." && pwd)
# Always use this checkout's environment, quietly, even from a shell with another venv active
unset VIRTUAL_ENV
exec uv run --quiet --project "$REPO_ROOT" python "$SCRIPT_DIR/instance.py" "$@"
