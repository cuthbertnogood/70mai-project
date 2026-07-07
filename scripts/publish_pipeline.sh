#!/usr/bin/env bash
# Wait for chunk 1 upload, then compose → YouTube → delete for chunks 2, 5, 4, 3.
#
# Usage:
#   WAIT_PID=23448 ./scripts/publish_pipeline.sh
#   ./scripts/publish_pipeline.sh   # waits for any chunk-1 upload-only process
#
# Logs:
#   video/Output/.publish_tmp/publish_pipeline.log  — orchestrator
#   video/Output/.publish_tmp/chunkNN_publish.log   — per-chunk publish_70mai.py

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=70mai-env.sh
source "$ROOT/scripts/70mai-env.sh"

SOURCE="${PUBLISH_SOURCE:-/Volumes/Untitled}"
TITLE="${PUBLISH_TITLE:-70mai 2026-04-25}"
LOG_DIR="video/Output/.publish_tmp"
PIPELINE_LOG="$LOG_DIR/publish_pipeline.log"
# Short chunks first; chunk 3 (trip 8, ~7.7 GB) last.
REMAINING_CHUNKS=(2 5 4 3)

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  echo "$line" >>"$PIPELINE_LOG"
  echo "$line"
}

wait_for_chunk1() {
  local pid="${WAIT_PID:-}"
  if [[ -n "$pid" ]]; then
    log "Waiting for chunk 1 upload (PID $pid)..."
    while kill -0 "$pid" 2>/dev/null; do
      sleep 30
    done
    log "PID $pid exited"
  else
    log "Waiting for chunk 1 upload-only process..."
    while pgrep -f "publish_70mai\\.py.*--chunk 1.*upload-only" >/dev/null 2>&1; do
      sleep 30
    done
    log "No chunk 1 upload process running"
  fi

  local chunk1_log="$LOG_DIR/chunk01_upload.log"
  if [[ ! -f "$chunk1_log" ]]; then
    log "ERROR: missing $chunk1_log"
    exit 1
  fi
  if grep -q "Failed:   [1-9]" "$chunk1_log"; then
    log "ERROR: chunk 1 upload reported failures — fix before continuing"
    exit 1
  fi
  if ! grep -q "Done\." "$chunk1_log"; then
    log "WARNING: chunk 1 log has no 'Done.' — check $chunk1_log before trusting pipeline"
  fi
  log "Chunk 1 upload phase complete"
}

run_chunk() {
  local chunk="$1"
  local logfile="$LOG_DIR/chunk$(printf '%02d' "$chunk")_publish.log"

  if [[ ! -d "$SOURCE" ]]; then
    log "ERROR: SD source not mounted: $SOURCE"
    return 1
  fi

  log "=== Chunk $chunk: compose → upload → delete → $logfile ==="
  set +e
  "$_70MAI_PY" publish_70mai.py \
    --source "$SOURCE" \
    --types Normal \
    --chunk "$chunk" \
    --per-trip-upload \
    --resume-upload \
    --resume \
    --state-on-sd \
    --continue-on-error \
    --title "$TITLE" \
    >>"$logfile" 2>&1
  local ec=$?
  set -e
  if [[ "$ec" -eq 0 ]]; then
    log "Chunk $chunk finished OK"
  else
    log "Chunk $chunk finished with exit code $ec (see $logfile)"
  fi
  return "$ec"
}

main() {
  mkdir -p "$LOG_DIR"
  log "Publish pipeline started (remaining chunks: ${REMAINING_CHUNKS[*]})"
  wait_for_chunk1
  local failed=0
  for chunk in "${REMAINING_CHUNKS[@]}"; do
    run_chunk "$chunk" || failed=$((failed + 1))
  done
  if [[ "$failed" -gt 0 ]]; then
    log "Pipeline done with $failed chunk(s) reporting errors"
    exit 1
  fi
  log "Pipeline complete — all remaining chunks processed"
}

main "$@"
