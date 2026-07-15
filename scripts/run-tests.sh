#!/usr/bin/env bash
# Run unit tests with lib/ on PYTHONPATH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  exec "$ROOT/scripts/setup-venv.sh"
fi
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m unittest discover -s tests -v "$@"
