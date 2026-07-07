#!/usr/bin/env bash
# Create or refresh project .venv (Python 3.12 recommended).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pick_python() {
  for candidate in \
    /usr/local/bin/python3.12 \
    /opt/homebrew/bin/python3.12 \
    python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ver="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
      major="${ver%%.*}"
      minor="${ver#*.}"
      if [[ "$major" -gt 3 || ( "$major" -eq 3 && "$minor" -ge 10 ) ]]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  echo "Python 3.10+ not found. Install: brew install python@3.12" >&2
  exit 1
}

echo "Using $PY ($("$PY" --version))"
"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "Done. Run scripts via: python3 publish_70mai.py ... (auto-uses .venv)"
