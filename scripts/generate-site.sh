#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_CMD="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD=python
else
  echo "[site] Python 3 is required" >&2
  exit 1
fi

exec "$PYTHON_CMD" "$ROOT_DIR/scripts/generate_site.py" "$@"
