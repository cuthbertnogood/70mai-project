#!/usr/bin/env bash
# Run compose_70mai.py with lid-close sleep disabled for the duration.
#
#   ./scripts/compose_awake.sh "video/ScreenRecording_04-25-2026 13-01-19.mp4" -d 60
#
# Prefer the autopilot watchdog for long unattended runs — it enables the same
# awake helpers for the whole session (see watch_publish_all_70mai.sh).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=70mai-awake.sh
source "$ROOT/scripts/70mai-awake.sh"

trap 70mai_awake_restore EXIT INT TERM
70mai_awake_enable 1 || true

set +e
caffeinate -dims "$ROOT/run" compose_70mai.py "$@"
rc=$?
set -e
exit "$rc"
