#!/usr/bin/env bash
# Smoke tests after code changes — run before/after fixes; fix scripts if this fails.
#
#   ./scripts/smoke-test.sh
#   ./scripts/smoke-test.sh tests.test_smoke   # only smoke module
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== smoke: venv =="
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  "$ROOT/scripts/setup-venv.sh"
fi

echo "== smoke: bash syntax (entry scripts) =="
for sh in \
  scripts/publish_all_70mai.sh \
  scripts/watch_publish_all_70mai.sh \
  scripts/autopilot_dashboard.sh \
  scripts/run-tests.sh \
  scripts/smoke-test.sh \
  run
do
  bash -n "$sh"
done

echo "== smoke: unit + smoke tests =="
if [[ $# -gt 0 ]]; then
  "$ROOT/scripts/run-tests.sh" "$@"
else
  "$ROOT/scripts/run-tests.sh"
fi

echo ""
echo "Smoke OK — safe to run autopilot / dashboard."
