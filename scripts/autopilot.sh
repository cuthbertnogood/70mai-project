#!/usr/bin/env bash
# Autopilot one-shot: web Dashboard + publish_all conveyor.
#
#   ./scripts/autopilot.sh
#   ./scripts/autopilot.sh --no-browser
#   ./scripts/autopilot.sh -- --skip-import
#
# Lid-close awake (default on, same helpers as watchdog / compose_awake):
#   AUTOPILOT_AWAKE=0 ./scripts/autopilot.sh   # disable
# Needs passwordless sudo for /usr/bin/pmset for closed-lid on AC
# (see scripts/70mai-awake.sh). Skipped for --dashboard-only.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

AUTOPILOT_AWAKE="${AUTOPILOT_AWAKE:-1}"

_is_dashboard_only=0
for _arg in "$@"; do
  if [[ "$_arg" == "--dashboard-only" ]]; then
    _is_dashboard_only=1
    break
  fi
done

if [[ "$AUTOPILOT_AWAKE" == "1" && "$_is_dashboard_only" -eq 0 ]]; then
  # shellcheck source=70mai-awake.sh
  source "$ROOT/scripts/70mai-awake.sh"
  trap 70mai_awake_restore EXIT INT TERM
  if 70mai_awake_enable 1; then
    70mai_awake_caffeinate_self
    printf '→ Awake on (disablesleep + caffeinate) — lid may stay closed on AC\n' >&2
  else
    70mai_awake_caffeinate_self
    printf '⚠ Awake partial (caffeinate only) — close lid only if SleepDisabled=1 already\n' >&2
  fi
  set +e
  "$ROOT/run" autopilot.py "$@"
  rc=$?
  set -e
  exit "$rc"
fi

exec "$ROOT/run" autopilot.py "$@"
