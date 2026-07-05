#!/usr/bin/env bash
# Watch publish_70mai compose; restart if process dies or output stalls.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CHUNK="${MONITOR_CHUNK:-1}"
STALL_SEC="${MONITOR_STALL_SEC:-900}"   # 15 min without progress => restart
CHECK_SEC="${MONITOR_CHECK_SEC:-60}"
LOG_DIR="video/Output/.publish_tmp"
MON_LOG="$LOG_DIR/monitor_chunk${CHUNK}.log"
COMPOSE_LOG="$LOG_DIR/chunk$(printf '%02d' "$CHUNK")_compose_active.log"
TEMP_DIR="$LOG_DIR"
PART="$TEMP_DIR/part_$(printf '%02d' "$CHUNK").mp4"
CHUNK_DIR="$TEMP_DIR/chunk_$(printf '%02d' "$CHUNK")"

PUBLISH_CMD=(
  python3 publish_70mai.py
  --source /Volumes/Untitled
  --types Normal
  --chunk "$CHUNK"
  --compose-only
  --keep
)

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$MON_LOG"
}

human_size() {
  local n="$1"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec "$n"
  else
    echo "${n}B"
  fi
}

progress_target() {
  if [[ -f "$PART" ]]; then
    echo "$PART"
    return
  fi
  if [[ -d "$CHUNK_DIR" ]]; then
    local latest
    latest="$(find "$CHUNK_DIR" -name 'trip_*.mp4' -type f 2>/dev/null | sort | tail -1 || true)"
    if [[ -n "$latest" ]]; then
      echo "$latest"
      return
    fi
  fi
  echo "$COMPOSE_LOG"
}

progress_signature() {
  local target size
  target="$(progress_target)"
  if [[ -f "$target" && "$target" == *.mp4 ]]; then
    size="$(stat -f%z "$target" 2>/dev/null || echo 0)"
    echo "${size}"
    return
  fi
  echo "0"
}

publish_running() {
  pgrep -f "publish_70mai\\.py.*--chunk ${CHUNK}.*compose-only" >/dev/null
}

ffmpeg_chunk_running() {
  pgrep -f "ffmpeg.*chunk_$(printf '%02d' "$CHUNK")/" >/dev/null 2>&1
}

start_compose() {
  log "Starting compose chunk $CHUNK -> $COMPOSE_LOG"
  "${PUBLISH_CMD[@]}" >>"$COMPOSE_LOG" 2>&1 &
  log "PID $!"
}

kill_stuck() {
  log "Killing stuck compose/ffmpeg for chunk $CHUNK"
  pkill -f "publish_70mai\\.py.*--chunk ${CHUNK}.*compose-only" 2>/dev/null || true
  sleep 2
  pkill -f "ffmpeg.*chunk_$(printf '%02d' "$CHUNK")/" 2>/dev/null || true
  sleep 1
}

log "Monitor chunk $CHUNK (stall=${STALL_SEC}s, check=${CHECK_SEC}s)"

if [[ -f "$PART" ]]; then
  log "Done: $PART exists — monitor exiting"
  exit 0
fi

last_sig=""
last_change="$(date +%s)"

while true; do
  if [[ -f "$PART" ]]; then
    log "Done: $PART ($(du -h "$PART" | cut -f1))"
    exit 0
  fi

  target="$(progress_target)"
  sig="$(progress_signature)"
  now="$(date +%s)"

  if publish_running || ffmpeg_chunk_running; then
    if [[ "$sig" != "$last_sig" ]]; then
      last_change="$now"
      if [[ -f "$target" && "$target" == *.mp4 ]]; then
        sz="$(stat -f%z "$target" 2>/dev/null || echo 0)"
        log "OK | $(basename "$target") $(human_size "$sz")"
      else
        log "OK | probing/planning (log active)"
      fi
      last_sig="$sig"
    else
      idle=$(( now - last_change ))
      log "OK | idle ${idle}s ($(basename "$target"))"
      if [[ "$idle" -ge "$STALL_SEC" ]]; then
        log "STALL ${idle}s >= ${STALL_SEC}s — restarting"
        kill_stuck
        start_compose
        last_sig=""
        last_change="$(date +%s)"
        sleep "$CHECK_SEC"
        continue
      fi
    fi
  else
    log "Process not running — restarting compose"
    start_compose
    last_sig=""
    last_change="$(date +%s)"
  fi

  sleep "$CHECK_SEC"
done
