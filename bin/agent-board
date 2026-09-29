#!/usr/bin/env bash
set -euo pipefail
# Public CLI; Python's standard library supplies flock and atomic snapshots.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$SCRIPT_DIR/board.py" "$@"
