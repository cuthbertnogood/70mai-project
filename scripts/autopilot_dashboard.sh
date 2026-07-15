#!/usr/bin/env bash
# Standalone live dashboard — independent of publish_all / publish_70mai / ffmpeg.
#
# Reads only files on disk (does not re-scan a busy SD by default):
#   video/Output/.publish_tmp/autopilot_plan.json   (trip table cache)
#   video/Output/.publish_tmp/autopilot_status.json
#   video/Output/.publish_tmp/autopilot_trip_reasons.json
#   video/Output/.publish_tmp/publish_*.state.json
#   chunk_*/trip_*.mp4 sizes
#
# Auto-reload: parent process stays up; edits to
#   lib/autopilot_dashboard_view.py  (screen layout — edit this for display tweaks)
#   lib/autopilot_dashboard.py       (data / refresh)
# are picked up without Ctrl+C (worker restarts; view also hot-reloads in-process).
#
# Safe to start, stop, or restart anytime (e.g. second terminal while encode runs).
# To avoid two tables in one terminal, run autopilot with --no-dashboard:
#
#   ./scripts/watch_publish_all_70mai.sh --skip-import --no-dashboard
#   ./scripts/autopilot_dashboard.sh
#
#   ./scripts/autopilot_dashboard.sh --wait
#   ./scripts/autopilot_dashboard.sh --source /Volumes/Untitled --interval 2
#   ./scripts/autopilot_dashboard.sh --scan-sd   # rebuild plan from SD (slow)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/run" autopilot_dashboard.py "$@"
