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
#   WATCH_STALL_SEC=1800       kill autopilot if publish_all.log unchanged (default 30 min)
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
STALL_SEC="${WATCH_STALL_SEC:-1800}"

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

# publish_70mai.py only — not publish_all_70mai.py or shell wrappers mentioning the name.
is_publish_70mai_cmd() {
  local cmd="$1"
  local lower
  [[ "$cmd" != *publish_all_70mai* ]] || return 1
  [[ "$cmd" =~ (^|[[:space:]/])publish_70mai\.py([[:space:]]|$) ]] || return 1
  lower="$(printf '%s' "$cmd" | tr '[:upper:]' '[:lower:]')"
  [[ "$lower" == *python* ]]
}

kill_stale_publish_70mai() {
  local pid cmd killed=0
  while read -r pid cmd; do
    [[ -z "$pid" ]] && continue
    if is_publish_70mai_cmd "$cmd"; then
      log "Killing stale publish_70mai.py pid $pid"
      kill -TERM "$pid" 2>/dev/null || true
      killed=1
    fi
  done < <(ps ax -o pid=,command= 2>/dev/null || true)
  if [[ "$killed" == "1" ]]; then
    sleep 3
    while read -r pid cmd; do
      [[ -z "$pid" ]] && continue
      if is_publish_70mai_cmd "$cmd" && pid_alive "$pid"; then
        log "SIGKILL publish_70mai.py pid $pid"
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done < <(ps ax -o pid=,command= 2>/dev/null || true)
  fi
}

kill_stale_autopilot_holder() {
  [[ -f "$AUTOPILOT_LOCK" ]] || return 0
  local pid
  pid="$(tr -d '[:space:]' <"$AUTOPILOT_LOCK" 2>/dev/null || true)"
  if pid_alive "$pid"; then
    log "Killing previous autopilot pid $pid (watchdog takeover)"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 3
    pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
  elif [[ -n "$pid" ]]; then
    log "Removing stale autopilot lock (pid $pid not running)"
  fi
  rm -f "$AUTOPILOT_LOCK"
}

cleanup_before_autopilot() {
  kill_stale_publish_70mai
  kill_stale_autopilot_holder
}

clear_stale_autopilot_lock() {
  cleanup_before_autopilot
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
  cleanup_before_autopilot
  log "Starting: $AUTOPILOT --force-restart $*"
  set +e
  "$AUTOPILOT" --force-restart "$@" >>"$AUTOPILOT_LOG" 2>&1 &
  local child=$!
  local last_size last_change now sz
  last_size="$(stat -f%z "$AUTOPILOT_LOG" 2>/dev/null || echo 0)"
  last_change="$(date +%s)"

  while pid_alive "$child"; do
    sleep 30
    sz="$(stat -f%z "$AUTOPILOT_LOG" 2>/dev/null || echo 0)"
    if [[ "$sz" != "$last_size" ]]; then
      last_size="$sz"
      last_change="$(date +%s)"
    fi
    now="$(date +%s)"
    if (( now - last_change > STALL_SEC )); then
      log "Autopilot stalled (no log progress for ${STALL_SEC}s) — killing pid $child"
      kill -TERM "$child" 2>/dev/null || true
      sleep 5
      pid_alive "$child" && kill -KILL "$child" 2>/dev/null || true
      cleanup_before_autopilot
      wait "$child" 2>/dev/null || true
      return 3
    fi
  done

  wait "$child"
  local ec=$?
  set -e
  log "Autopilot exited with code $ec"
  return "$ec"
}

main() {
  acquire_watch_lock
  log "Watchdog started (restart=${RESTART_SEC}s, stop_on_success=${STOP_ON_SUCCESS}, once=${WATCH_ONCE}, stall=${STALL_SEC}s)"
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
    elif [[ "$ec" -eq 3 ]]; then
      log "Autopilot killed (stalled) — restart in ${RESTART_SEC}s"
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
