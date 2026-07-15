#!/usr/bin/env bash
# Full autopilot: insert SD card → import → compose → YouTube → delete.
#
# Default: Normal (trips) + Event + Parking (each → one 2-cam YouTube video when merged).
#
#   ./scripts/publish_all_70mai.sh --wait          # wait for SD, then run
#   ./scripts/publish_all_70mai.sh --force-restart --wait  # kill previous run, restart
#   ./scripts/publish_all_70mai.sh                 # run now if SD mounted
#   ./scripts/publish_all_70mai.sh --wait --loop   # daemon: re-run after each card session
#   ./scripts/watch_publish_all_70mai.sh --skip-import  # restart on crash (see script header)
#
# SD card: .70mai/import/CARD_SUMMARY.txt (trips + merge status),
#          .70mai/import/CARD_STORAGE.txt (sizes by type + disk free)
# Log: video/Output/.publish_tmp/publish_all.log

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/run" publish_all_70mai.py "$@"
