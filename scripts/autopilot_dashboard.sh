#!/usr/bin/env bash
# Standalone live dashboard — independent of publish_all / publish_70mai / ffmpeg.
#
# Reads only files on disk:
#   video/Output/.publish_tmp/autopilot_status.json
#   video/Output/.publish_tmp/autopilot_trip_reasons.json
#   SD .70mai/publish/*.state.json
#   chunk_*/trip_*.mp4 sizes
#
# Safe to start, stop, or restart anytime (e.g. second terminal while encode runs).
# To avoid two tables in one terminal, run autopilot with --no-dashboard:
#
#   ./scripts/watch_publish_all_70mai.sh --skip-import --no-dashboard
#   ./scripts/autopilot_dashboard.sh
#
#   ./scripts/autopilot_dashboard.sh --wait
#   ./scripts/autopilot_dashboard.sh --source /Volumes/Untitled --interval 2

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/run" autopilot_dashboard.py "$@"
