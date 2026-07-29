#!/usr/bin/env bash
# Cleanup SSD leftovers after YouTube upload is confirmed in publish_*.state.json.
#
# Deletes for fully uploaded types:
#   - composed: .publish_tmp/{Type}/part_*.mp4, full_long_*.mp4, …
#   - merges:   video/Output/{Type}/{Front,Back}/*.mp4 (+ timeline manifests)
#   - stages:   video/Output/{Type}/*/.merge_stage/
#
# Sources stay on the SD card (can re-import).
#
#   ./scripts/cleanup_uploaded_70mai.sh
#   ./scripts/cleanup_uploaded_70mai.sh --dry-run
#   ./scripts/cleanup_uploaded_70mai.sh --types Parking Event

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

exec "$ROOT/run" lib/cleanup_uploaded_70mai.py "$@"
