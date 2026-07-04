#!/bin/bash
# Auto-commit uncommitted changes when a Cursor agent session ends.
# Respects .gitignore; skips if there is nothing to commit.

set -euo pipefail

cat >/dev/null

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT"

if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  exit 0
fi

timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
summary="$(git status --porcelain | head -20 | sed 's/^...//' | paste -sd ', ' -)"

git add -A
git commit -m "$(cat <<EOF
Auto-commit: ${timestamp}

${summary}
EOF
)" || exit 0

exit 0
