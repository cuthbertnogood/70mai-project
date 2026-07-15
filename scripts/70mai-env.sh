#!/usr/bin/env bash
# Source from shell scripts: resolve project root and venv python.
set -euo pipefail

_70MAI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_70MAI_PY="$_70MAI_ROOT/.venv/bin/python"
export PYTHONPATH="$_70MAI_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

if [[ ! -x "$_70MAI_PY" ]]; then
  echo "Missing .venv — run: $_70MAI_ROOT/scripts/setup-venv.sh" >&2
  exit 1
fi
