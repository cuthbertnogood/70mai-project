#!/usr/bin/env bash
# Full autopilot: insert SD card → import → compose → YouTube → delete.
#
#   ./scripts/publish_all_70mai.sh --wait          # wait for SD, then run
#   ./scripts/publish_all_70mai.sh                 # run now if SD mounted
#   ./scripts/publish_all_70mai.sh --wait --loop   # daemon: re-run after each card session
#
# Log: video/Output/.publish_tmp/publish_all.log

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/run" publish_all_70mai.py "$@"
