#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[v2] Python 3 is required: $PYTHON" >&2
  exit 1
fi

# V2 requires an explicit --model or --all; all arguments pass through intact.
exec "$PYTHON" "$ROOT_DIR/runner/generate.py" "$@"
