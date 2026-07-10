#!/usr/bin/env bash
# Run compose_70mai.py with lid-close sleep disabled for the duration.
#
# Idle keep-awake (caffeinate) alone does not survive closing the MacBook lid.
# This wrapper toggles `pmset disablesleep` around the encode, then restores
# the previous value — even on Ctrl-C / failure.
#
#   ./scripts/compose_awake.sh "video/ScreenRecording_....mp4" -d 60
#   ./scripts/compose_awake.sh "video/ScreenRecording_....mp4" --profile hevc
#
# Optional passwordless sudo (once):
#   echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/pmset" | sudo tee /etc/sudoers.d/70mai-pmset
#   sudo chmod 440 /etc/sudoers.d/70mai-pmset

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PMSET=/usr/bin/pmset
DID_TOGGLE=0
PREV_DISABLED=0

log() { printf '→ %s\n' "$*" >&2; }
warn() { printf '⚠ %s\n' "$*" >&2; }

read_sleep_disabled() {
  # pmset omits the key when 0; treat missing as 0.
  local v
  v="$("$PMSET" -g 2>/dev/null | awk '/SleepDisabled/{print $2; exit}')"
  [[ -n "$v" ]] && echo "$v" || echo 0
}

restore_sleep() {
  if [[ "$DID_TOGGLE" -eq 1 ]]; then
    log "Restoring SleepDisabled=$PREV_DISABLED"
    if ! sudo -n "$PMSET" -a disablesleep "$PREV_DISABLED" 2>/dev/null \
       && ! sudo "$PMSET" -a disablesleep "$PREV_DISABLED"; then
      warn "Could not restore disablesleep — run: sudo pmset -a disablesleep $PREV_DISABLED"
    fi
    DID_TOGGLE=0
  fi
}
trap restore_sleep EXIT INT TERM

PREV_DISABLED="$(read_sleep_disabled)"

if [[ "$PREV_DISABLED" != "1" ]]; then
  log "Enabling lid-close awake (pmset disablesleep 1); was $PREV_DISABLED"
  if sudo -n "$PMSET" -a disablesleep 1 2>/dev/null \
     || sudo "$PMSET" -a disablesleep 1; then
    DID_TOGGLE=1
    if [[ "$(read_sleep_disabled)" != "1" ]]; then
      warn "disablesleep did not stick — lid close may still sleep the Mac"
    else
      log "SleepDisabled=1 (safe to close lid; keep on AC power)"
    fi
  else
    warn "sudo pmset failed — continuing with caffeinate only (lid close will still sleep)"
  fi
else
  log "SleepDisabled already 1 — leaving as-is (will not change on exit)"
fi

# -d display, -i idle, -m disk, -s system assertions (belt + suspenders with disablesleep)
set +e
caffeinate -dims "$ROOT/run" compose_70mai.py "$@"
rc=$?
set -e
exit "$rc"
