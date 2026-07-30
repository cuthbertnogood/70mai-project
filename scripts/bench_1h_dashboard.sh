#!/usr/bin/env bash
# Live dashboard for 1h SD bench (same paths as bench_1h_run.sh).
#
#   # terminal 1
#   ./scripts/bench_1h_dashboard.sh
#   # terminal 2
#   ./scripts/bench_1h_run.sh
#
# Safe to start/stop anytime; does not drive the pipeline.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SOURCE="${BENCH_SOURCE:-/Volumes/Untitled}"
VIDEO_DIR="${BENCH_VIDEO_DIR:-$ROOT/video/Output}"
TEMP_DIR="${BENCH_TEMP_DIR:-$VIDEO_DIR/.publish_tmp}"

exec "$ROOT/scripts/autopilot_dashboard.sh" \
  --source "$SOURCE" \
  --types Normal \
  --video-dir "$VIDEO_DIR" \
  --temp-dir "$TEMP_DIR" \
  --interval 1 \
  "$@"
