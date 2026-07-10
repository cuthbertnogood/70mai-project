#!/usr/bin/env bash
# Shared lid-close awake helpers (source from other scripts).
#
#   source "$ROOT/scripts/70mai-awake.sh"
#   70mai_awake_enable 1    # 1 = allow interactive sudo prompt
#   70mai_awake_enable 0    # nopass only (watchdog / background)
#   70mai_awake_caffeinate_self   # hold idle assertions while this shell lives
#   70mai_awake_restore     # undo disablesleep if we toggled it
#
# Optional passwordless sudo (once):
#   echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/pmset" | sudo tee /etc/sudoers.d/70mai-pmset
#   sudo chmod 440 /etc/sudoers.d/70mai-pmset

_70MAI_PMSET=/usr/bin/pmset
_70MAI_AWAKE_DID_TOGGLE=0
_70MAI_AWAKE_PREV=0
_70MAI_CAFFEINE_PID=""

_70mai_awake_log() { printf '→ %s\n' "$*" >&2; }
_70mai_awake_warn() { printf '⚠ %s\n' "$*" >&2; }

70mai_awake_read_disabled() {
  local v
  v="$("$_70MAI_PMSET" -g 2>/dev/null | awk '/SleepDisabled/{print $2; exit}')"
  [[ -n "$v" ]] && echo "$v" || echo 0
}

70mai_awake_enable() {
  local allow_prompt="${1:-0}"
  _70MAI_AWAKE_PREV="$(70mai_awake_read_disabled)"

  if [[ "$_70MAI_AWAKE_PREV" == "1" ]]; then
    _70mai_awake_log "SleepDisabled already 1 — leaving as-is"
    _70MAI_AWAKE_DID_TOGGLE=0
    return 0
  fi

  _70mai_awake_log "Enabling lid-close awake (pmset disablesleep 1); was $_70MAI_AWAKE_PREV"
  if sudo -n "$_70MAI_PMSET" -a disablesleep 1 2>/dev/null; then
    :
  elif [[ "$allow_prompt" == "1" ]] && sudo "$_70MAI_PMSET" -a disablesleep 1; then
    :
  else
    _70mai_awake_warn "sudo pmset failed — lid close may still sleep (set NOPASSWD for /usr/bin/pmset)"
    return 1
  fi

  _70MAI_AWAKE_DID_TOGGLE=1
  if [[ "$(70mai_awake_read_disabled)" != "1" ]]; then
    _70mai_awake_warn "disablesleep did not stick"
    return 1
  fi
  _70mai_awake_log "SleepDisabled=1 (safe to close lid; keep on AC power)"
  return 0
}

70mai_awake_caffeinate_self() {
  # Hold idle/display/disk/system assertions until this shell exits.
  if [[ -n "$_70MAI_CAFFEINE_PID" ]] && kill -0 "$_70MAI_CAFFEINE_PID" 2>/dev/null; then
    return 0
  fi
  caffeinate -dims -w $$ >/dev/null 2>&1 &
  _70MAI_CAFFEINE_PID=$!
}

70mai_awake_restore() {
  if [[ -n "$_70MAI_CAFFEINE_PID" ]]; then
    kill "$_70MAI_CAFFEINE_PID" 2>/dev/null || true
    _70MAI_CAFFEINE_PID=""
  fi
  if [[ "$_70MAI_AWAKE_DID_TOGGLE" -eq 1 ]]; then
    _70mai_awake_log "Restoring SleepDisabled=$_70MAI_AWAKE_PREV"
    if ! sudo -n "$_70MAI_PMSET" -a disablesleep "$_70MAI_AWAKE_PREV" 2>/dev/null \
       && ! sudo "$_70MAI_PMSET" -a disablesleep "$_70MAI_AWAKE_PREV" 2>/dev/null; then
      _70mai_awake_warn "Could not restore disablesleep — run: sudo pmset -a disablesleep $_70MAI_AWAKE_PREV"
    fi
    _70MAI_AWAKE_DID_TOGGLE=0
  fi
}
