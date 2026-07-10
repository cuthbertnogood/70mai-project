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
#   WATCH_STALL_SEC=7200       kill if trip_*.mp4 total size unchanged (default 2h; log heartbeats ignored)
#   WATCH_AWAKE=1              lid-close awake via pmset+caffeinate (default on)
#
# Logs:
#   video/Output/.publish_tmp/publish_all.log          — autopilot
#   video/Output/.publish_tmp/publish_all_watchdog.log — watchdog events
#
# Lid-close: needs passwordless sudo for /usr/bin/pmset (see scripts/70mai-awake.sh).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=70mai-awake.sh
source "$ROOT/scripts/70mai-awake.sh"

WATCH_AWAKE="${WATCH_AWAKE:-1}"

LOG_DIR="video/Output/.publish_tmp"
AUTOPILOT_LOG="$LOG_DIR/publish_all.log"
WATCH_LOG="$LOG_DIR/publish_all_watchdog.log"
AUTOPILOT_LOCK="$LOG_DIR/.publish_all.lock"
WATCH_LOCK="$LOG_DIR/.publish_all_watchdog.lock"
AUTOPILOT="$ROOT/scripts/publish_all_70mai.sh"

RESTART_SEC="${WATCH_RESTART_SEC:-60}"
STOP_ON_SUCCESS="${WATCH_STOP_ON_SUCCESS:-1}"
WATCH_ONCE="${WATCH_ONCE:-0}"
STALL_SEC="${WATCH_STALL_SEC:-7200}"

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

kill_stale_ffmpeg() {
  local pid cmd killed=0
  while read -r pid cmd; do
    [[ -z "$pid" ]] && continue
    if [[ "$cmd" == *ffmpeg* ]] && [[ "$cmd" == *".publish_tmp"* || "$cmd" == *"/chunk_"*"/trip_"* ]]; then
      log "Killing stale ffmpeg pid $pid"
      kill -TERM "$pid" 2>/dev/null || true
      killed=1
    fi
  done < <(ps ax -o pid=,command= 2>/dev/null || true)
  if [[ "$killed" == "1" ]]; then
    sleep 2
    while read -r pid cmd; do
      [[ -z "$pid" ]] && continue
      if [[ "$cmd" == *ffmpeg* ]] && [[ "$cmd" == *".publish_tmp"* || "$cmd" == *"/chunk_"*"/trip_"* ]]; then
        pid_alive "$pid" && log "SIGKILL stale ffmpeg pid $pid" && kill -KILL "$pid" 2>/dev/null || true
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
  kill_stale_ffmpeg
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

CHILD_PID=""

on_signal() {
  log "Signal received — stopping watchdog and autopilot"
  if [[ -n "${CHILD_PID}" ]] && pid_alive "$CHILD_PID"; then
    log "Sending SIGTERM to autopilot pid $CHILD_PID"
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    sleep 3
    if pid_alive "$CHILD_PID"; then
      log "SIGKILL autopilot pid $CHILD_PID"
      kill -KILL "$CHILD_PID" 2>/dev/null || true
    fi
  fi
  cleanup_before_autopilot
  exit 130
}

on_exit() {
  if [[ "$WATCH_AWAKE" == "1" ]]; then
    70mai_awake_restore
  fi
  release_watch_lock
}

trap on_exit EXIT
trap on_signal INT TERM

run_autopilot() {
  cleanup_before_autopilot
  log "Starting: $AUTOPILOT --force-restart $*"
  set +e
  "$AUTOPILOT" --force-restart "$@" >>"$AUTOPILOT_LOG" 2>&1 &
  CHILD_PID=$!
  local child=$CHILD_PID
  local last_trip_sz last_change now trip_sz
  last_trip_sz=0
  for f in "$LOG_DIR"/chunk_*/trip_*.mp4; do
    [[ -f "$f" ]] || continue
    last_trip_sz=$(( last_trip_sz + $(stat -f%z "$f" 2>/dev/null || echo 0) ))
  done
  last_change="$(date +%s)"

  while pid_alive "$child"; do
    sleep 30
    # Progress = trip_*.mp4 growth only (encode heartbeats must not reset stall)
    trip_sz=0
    for f in "$LOG_DIR"/chunk_*/trip_*.mp4; do
      [[ -f "$f" ]] || continue
      trip_sz=$(( trip_sz + $(stat -f%z "$f" 2>/dev/null || echo 0) ))
    done
    if [[ "$trip_sz" != "$last_trip_sz" ]]; then
      last_trip_sz="$trip_sz"
      last_change="$(date +%s)"
    fi
    now="$(date +%s)"
    if (( now - last_change > STALL_SEC )); then
      log "Autopilot stalled (trip_*.mp4 unchanged ${STALL_SEC}s, total ${trip_sz} bytes) — killing pid $child"
      kill -TERM "$child" 2>/dev/null || true
      sleep 5
      pid_alive "$child" && kill -KILL "$child" 2>/dev/null || true
      cleanup_before_autopilot
      wait "$child" 2>/dev/null || true
      CHILD_PID=""
      return 3
    fi
  done

  wait "$child"
  local ec=$?
  CHILD_PID=""
  set -e
  log "Autopilot exited with code $ec"
  return "$ec"
}

main() {
  acquire_watch_lock
  log "Watchdog started (restart=${RESTART_SEC}s, stop_on_success=${STOP_ON_SUCCESS}, once=${WATCH_ONCE}, stall=${STALL_SEC}s, awake=${WATCH_AWAKE})"
  log "Args: $*"

  if [[ "$WATCH_AWAKE" == "1" ]]; then
    if 70mai_awake_enable 0; then
      70mai_awake_caffeinate_self
      log "Awake on (disablesleep + caffeinate) — lid may stay closed on AC"
    else
      70mai_awake_caffeinate_self
      log "Awake partial (caffeinate only) — close lid only if SleepDisabled=1 already"
    fi
  fi

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
      exit "$ec"
    fi

    sleep "$RESTART_SEC"
  done
}

main "$@"
