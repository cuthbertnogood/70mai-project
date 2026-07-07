#!/bin/bash
# Auto-commit and push when a Cursor agent session ends.
# Respects .gitignore; skips commit if there is nothing to commit;
# pushes when the branch is ahead of its upstream.

set -euo pipefail

cat >/dev/null

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT"

if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
  summary="$(git status --porcelain | head -20 | sed 's/^...//' | paste -sd ', ' -)"

  git add -A
  git commit -m "$(cat <<EOF
Auto-commit: ${timestamp}

${summary}
EOF
)" || true
fi

remote="$(git remote 2>/dev/null | head -1 || true)"
if [ -n "$remote" ]; then
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    ahead="$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
    if [ "$ahead" -gt 0 ]; then
      git push 2>/dev/null || true
    fi
  else
    git push -u "$remote" "$branch" 2>/dev/null || true
  fi
fi

exit 0
