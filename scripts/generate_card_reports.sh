#!/usr/bin/env bash
# Generate MD/CSV card session reports → отчеты/ + SD .70mai/reports/
#
#   ./scripts/generate_card_reports.sh
#   ./scripts/generate_card_reports.sh --source /Volumes/Untitled
#   ./scripts/generate_card_reports.sh --no-sd   # project only

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/run" scripts/generate_card_reports.py "$@"
