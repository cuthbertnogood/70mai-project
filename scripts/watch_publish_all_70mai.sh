#!/usr/bin/env bash
# Watchdog: restart autopilot on crash; stop when it exits cleanly (default).
#
#   ./scripts/watch_publish_all_70mai.sh --skip-import
#   WATCH_RESTART_SEC=120 ./scripts/watch_publish_all_70mai.sh --wait
#
# Env:
#   WATCH_RESTART_SEC=60       sleep before restart after failure
#   WATCH_STOP_ON_SUCCESS=1    exit watchdog when autopilot returns 0 (default)
#   WATCH_ONCE=1               single run, no restart loop
#
# Logs:
#   video/Output/.publish_tmp/publish_all.log          — autopilot
#   video/Output/.publish_tmp/publish_all_watchdog.log — watchdog events

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG_DIR="video/Output/.publish_tmp"
AUTOPILOT_LOG="$LOG_DIR/publish_all.log"
WATCH_LOG="$LOG_DIR/publish_all_watchdog.log"
AUTOPILOT_LOCK="$LOG_DIR/.publish_all.lock"
WATCH_LOCK="$LOG_DIR/.publish_all_watchdog.lock"
AUTOPILOT="$ROOT/scripts/publish_all_70mai.sh"

RESTART_SEC="${WATCH_RESTART_SEC:-60}"
STOP_ON_SUCCESS="${WATCH_STOP_ON_SUCCESS:-1}"
WATCH_ONCE="${WATCH_ONCE:-0}"

mkdir -p "$LOG_DIR"

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*"
  echo "$line" | tee -a "$WATCH_LOG"
}

pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

clear_stale_autopilot_lock() {
  [[ -f "$AUTOPILOT_LOCK" ]] || return 0
  local pid
  pid="$(tr -d '[:space:]' <"$AUTOPILOT_LOCK" 2>/dev/null || true)"
  if pid_alive "$pid"; then
    log "Autopilot lock held by live pid $pid — waiting..."
    return 1
  fi
  log "Removing stale autopilot lock (pid ${pid:-?} not running)"
  rm -f "$AUTOPILOT_LOCK"
}

acquire_watch_lock() {
  if [[ -f "$WATCH_LOCK" ]]; then
    local pid
    pid="$(tr -d '[:space:]' <"$WATCH_LOCK" 2>/dev/null || true)"
    if pid_alive "$pid"; then
      log "ERROR: another watchdog running (pid $pid, lock $WATCH_LOCK)"
      exit 1
    fi
    log "Removing stale watchdog lock (pid ${pid:-?} not running)"
    rm -f "$WATCH_LOCK"
  fi
  echo "$$" >"$WATCH_LOCK"
}

release_watch_lock() {
  rm -f "$WATCH_LOCK"
}

on_signal() {
  log "Signal received — stopping watchdog (autopilot child not killed)"
  release_watch_lock
  exit 130
}

trap on_signal INT TERM

run_autopilot() {
  clear_stale_autopilot_lock || {
    sleep "$RESTART_SEC"
    return 2
  }
  log "Starting: $AUTOPILOT $*"
  set +e
  "$AUTOPILOT" "$@" >>"$AUTOPILOT_LOG" 2>&1
  local ec=$?
  set -e
  log "Autopilot exited with code $ec"
  return "$ec"
}

main() {
  acquire_watch_lock
  log "Watchdog started (restart=${RESTART_SEC}s, stop_on_success=${STOP_ON_SUCCESS}, once=${WATCH_ONCE})"
  log "Args: $*"

  local attempt=0
  while true; do
    attempt=$(( attempt + 1 ))
    log "=== Attempt $attempt ==="
    set +e
    run_autopilot "$@"
    local ec=$?
    set -e

    if [[ "$ec" -eq 0 ]]; then
      log "Autopilot finished OK"
      if [[ "$STOP_ON_SUCCESS" == "1" || "$WATCH_ONCE" == "1" ]]; then
        log "Watchdog exiting (success)"
        release_watch_lock
        exit 0
      fi
      log "Restarting after success in ${RESTART_SEC}s (WATCH_STOP_ON_SUCCESS=0)"
    elif [[ "$ec" -eq 2 ]]; then
      log "Autopilot still running — retry in ${RESTART_SEC}s"
    else
      log "Autopilot failed (exit $ec) — restart in ${RESTART_SEC}s"
    fi

    if [[ "$WATCH_ONCE" == "1" ]]; then
      log "Watchdog exiting (WATCH_ONCE=1, exit $ec)"
      release_watch_lock
      exit "$ec"
    fi

    sleep "$RESTART_SEC"
  done
}

main "$@"
