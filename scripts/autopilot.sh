#!/usr/bin/env bash
# Autopilot one-shot: web Dashboard + publish_all conveyor.
#
#   ./scripts/autopilot.sh
#   ./scripts/autopilot.sh --no-browser
#   ./scripts/autopilot.sh -- --skip-import

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/run" autopilot.py "$@"
