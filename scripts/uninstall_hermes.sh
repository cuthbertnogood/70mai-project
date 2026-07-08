#!/usr/bin/env bash
# Fully remove Hermes Agent (NousResearch) from macOS.
# Uses official `hermes uninstall --full --yes` when available, then cleans
# artifacts the official uninstaller misses (uv Python, macOS prefs, DMG).
set -euo pipefail

DRY_RUN=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: uninstall_hermes.sh [--dry-run] [--yes|-y]

  --dry-run   Show what would be removed without changing anything
  --yes, -y   Skip confirmation prompt

Removes Hermes Agent from all typical install locations on macOS.
Does NOT remove Homebrew ripgrep or other shared tools.
EOF
}

log() { printf '→ %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

HOME_DIR="${HOME:?}"
HERMES_HOME="${HERMES_HOME:-$HOME_DIR/.hermes}"
LOCAL_BIN="$HOME_DIR/.local/bin"
LAUNCH_AGENT="$HOME_DIR/Library/LaunchAgents/ai.hermes.gateway.plist"
GATEWAY_LABEL="ai.hermes.gateway"

REMOVED=()
SKIPPED=()
REMAINING=()

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

remove_path() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] rm -rf %q\n' "$path"
    else
      rm -rf "$path"
    fi
    REMOVED+=("$path")
    return 0
  fi
  return 1
}

remove_file() {
  local path="$1"
  if [[ -f "$path" || -L "$path" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] rm -f %q\n' "$path"
    else
      rm -f "$path"
    fi
    REMOVED+=("$path")
    return 0
  fi
  return 1
}

symlink_target() {
  local link="$1"
  [[ -L "$link" ]] || return 1
  local target
  target="$(readlink "$link")"
  if [[ "$target" != /* ]]; then
    target="$(cd "$(dirname "$link")" && pwd)/$target"
  fi
  printf '%s\n' "$target"
}

is_hermes_managed_symlink() {
  local link="$1"
  local target
  target="$(symlink_target "$link" 2>/dev/null)" || return 1
  case "$target" in
    "$HERMES_HOME"/*|"$HOME_DIR/.hermes"/*) return 0 ;;
    "$HOME_DIR/.local/share/uv/python/cpython-3.11"*) return 0 ;;
  esac
  return 1
}

remove_hermes_symlink() {
  local link="$1"
  if [[ -L "$link" ]] && is_hermes_managed_symlink "$link"; then
    remove_file "$link"
    return 0
  fi
  if [[ -e "$link" ]]; then
    SKIPPED+=("$link (not a Hermes-managed symlink)")
  fi
  return 1
}

remove_hermes_wrapper() {
  local wrapper="$LOCAL_BIN/hermes"
  [[ -e "$wrapper" ]] || return 1
  if [[ -L "$wrapper" ]]; then
    remove_hermes_symlink "$wrapper"
    return $?
  fi
  if [[ -f "$wrapper" ]] && grep -q 'hermes_cli\|hermes-agent' "$wrapper" 2>/dev/null; then
    remove_file "$wrapper"
    return 0
  fi
  SKIPPED+=("$wrapper (not a Hermes wrapper)")
  return 1
}

clean_shell_configs() {
  local configs=(
    "$HOME_DIR/.zshrc"
    "$HOME_DIR/.zprofile"
    "$HOME_DIR/.bashrc"
    "$HOME_DIR/.bash_profile"
    "$HOME_DIR/.profile"
  )
  local config
  for config in "${configs[@]}"; do
    [[ -f "$config" ]] || continue
    if ! grep -qi 'hermes' "$config" 2>/dev/null; then
      continue
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] strip Hermes PATH lines from %q\n' "$config"
      REMOVED+=("$config (hermes lines)")
      continue
    fi
    local tmp skip_next=0 changed=0
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == *'# Hermes Agent'* || "$line" == *'# hermes-agent'* ]]; then
        skip_next=1
        changed=1
        continue
      fi
      if [[ "$skip_next" -eq 1 && "$line" == *hermes* && "$line" == *PATH* ]]; then
        skip_next=0
        changed=1
        continue
      fi
      skip_next=0
      if [[ "$line" == *hermes* && "$line" == *PATH* ]]; then
        changed=1
        continue
      fi
      printf '%s\n' "$line"
    done < "$config" > "$tmp"
    if [[ "$changed" -eq 1 ]]; then
      mv "$tmp" "$config"
      REMOVED+=("$config (hermes lines)")
      ok "Updated $config"
    else
      rm -f "$tmp"
    fi
  done
}

stop_processes() {
  log "Stopping Hermes processes..."

  if pgrep -xq Hermes 2>/dev/null || pgrep -f '/Applications/Hermes.app' >/dev/null 2>&1; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] osascript quit Hermes.app\n'
    else
      osascript -e 'tell application "Hermes" to quit' 2>/dev/null || true
      sleep 1
    fi
  fi

  if [[ -f "$LAUNCH_AGENT" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] launchctl bootout gui/%s/%s\n' "$UID" "$GATEWAY_LABEL"
    else
      launchctl bootout "gui/$UID/$GATEWAY_LABEL" 2>/dev/null \
        || launchctl unload "$LAUNCH_AGENT" 2>/dev/null \
        || true
    fi
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] pkill hermes / hermes_cli / gateway processes\n'
    return 0
  fi

  pkill -f 'hermes_cli\.main gateway' 2>/dev/null || true
  pkill -f '/\.hermes/hermes-agent/venv/bin/hermes' 2>/dev/null || true
  pkill -x hermes 2>/dev/null || true
  sleep 1
}

find_hermes_cli() {
  local candidate
  for candidate in \
    "$LOCAL_BIN/hermes" \
    "$HERMES_HOME/hermes-agent/venv/bin/hermes"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

run_official_uninstall() {
  local cli
  cli="$(find_hermes_cli 2>/dev/null || true)"
  [[ -n "$cli" ]] || return 0

  log "Running official uninstall via $cli ..."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] %q uninstall --full --yes\n' "$cli"
    return 0
  fi

  if "$cli" uninstall --full --yes; then
    ok "Official uninstall completed"
  else
    warn "Official uninstall failed or was incomplete — continuing with manual cleanup"
  fi
}

manual_cleanup() {
  log "Manual cleanup of Hermes artifacts..."

  remove_path "$HERMES_HOME" || true

  remove_hermes_wrapper || true
  local link
  for link in node npm npx python3.11; do
    remove_hermes_symlink "$LOCAL_BIN/$link" || true
  done

  remove_file "$LAUNCH_AGENT" || true

  remove_path /Applications/Hermes.app || true
  remove_path "$HOME_DIR/Applications/Hermes.app" || true
  remove_path "$HOME_DIR/Library/Application Support/Hermes" || true
  remove_file "$HOME_DIR/Library/Preferences/com.nousresearch.hermes.plist" || true
  remove_path "$HOME_DIR/Library/Saved Application State/com.nousresearch.hermes.savedState" || true

  local crash
  shopt -s nullglob
  for crash in "$HOME_DIR/Library/Application Support/CrashReporter"/Hermes_*.plist; do
    remove_file "$crash" || true
  done
  shopt -u nullglob

  remove_file "$HOME_DIR/Downloads/Hermes-Setup.dmg" || true

  clean_shell_configs
}

cleanup_extra_artifacts() {
  log "Removing extra install.sh artifacts (state, caches)..."

  remove_path "$HOME_DIR/.local/state/hermes" || true
  remove_path "$HOME_DIR/Library/Caches/ms-playwright" || true

  local npx_dir
  shopt -s nullglob
  for npx_dir in "$HOME_DIR/.npm/_npx"/*/; do
    if [[ -f "${npx_dir}package.json" ]] && grep -q '"playwright"' "${npx_dir}package.json" 2>/dev/null; then
      remove_path "$npx_dir" || true
    fi
  done
  shopt -u nullglob

  local cache_dir
  shopt -s nullglob
  for cache_dir in /var/folders/*/*/*/C/com.nousresearch.hermes /var/folders/*/*/*/C/com.nousresearch.hermes.helper; do
    remove_path "$cache_dir" || true
  done
  shopt -u nullglob
}

cleanup_uv_python() {
  log "Removing Hermes-managed uv Python (if present)..."

  remove_hermes_symlink "$LOCAL_BIN/python3.11" || true

  local uv_python_dir="$HOME_DIR/.local/share/uv/python"
  if [[ -d "$uv_python_dir" ]]; then
    local entry
    shopt -s nullglob
    for entry in "$uv_python_dir"/cpython-3.11*; do
      remove_path "$entry" || true
    done
    shopt -u nullglob
    if [[ -d "$uv_python_dir" ]] && [[ -z "$(find "$uv_python_dir" -mindepth 1 -maxdepth 1 ! -name '.gitignore' ! -name '.lock' ! -name '.temp' -print -quit 2>/dev/null)" ]]; then
      remove_path "$uv_python_dir" || true
    fi
  fi
  if [[ -d "$HOME_DIR/.local/share/uv" ]] && [[ -z "$(find "$HOME_DIR/.local/share/uv" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    remove_path "$HOME_DIR/.local/share/uv" || true
  fi
}

collect_remaining() {
  local checks=(
    "$HERMES_HOME"
    "$LOCAL_BIN/hermes"
    "$LAUNCH_AGENT"
    /Applications/Hermes.app
    "$HOME_DIR/Applications/Hermes.app"
    "$HOME_DIR/Library/Application Support/Hermes"
    "$HOME_DIR/Library/Preferences/com.nousresearch.hermes.plist"
    "$HOME_DIR/Library/Saved Application State/com.nousresearch.hermes.savedState"
    "$HOME_DIR/Downloads/Hermes-Setup.dmg"
    "$HOME_DIR/.local/state/hermes"
    "$HOME_DIR/Library/Caches/ms-playwright"
  )
  local path
  for path in "${checks[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
      REMAINING+=("$path")
    fi
  done

  if command -v hermes >/dev/null 2>&1; then
    REMAINING+=("hermes on PATH: $(command -v hermes)")
  fi
  if launchctl list 2>/dev/null | grep -qi hermes; then
    REMAINING+=("launchctl service still registered (grep hermes)")
  fi
}

verify_and_report() {
  collect_remaining

  echo
  if [[ ${#REMOVED[@]} -gt 0 ]]; then
    echo "Removed (${#REMOVED[@]} items):"
    local item
    for item in "${REMOVED[@]}"; do
      echo "  • $item"
    done
  fi

  if [[ ${#SKIPPED[@]} -gt 0 ]]; then
    echo
    echo "Skipped:"
    for item in "${SKIPPED[@]}"; do
      echo "  • $item"
    done
  fi

  echo
  if [[ "$DRY_RUN" -eq 1 ]]; then
    ok "Dry run complete — no changes made"
    return 0
  fi
  if [[ ${#REMAINING[@]} -eq 0 ]]; then
    ok "Hermes Agent fully removed"
    echo
    echo "Reload your shell: source ~/.zshrc"
    return 0
  fi

  warn "Some artifacts may still remain:"
  for item in "${REMAINING[@]}"; do
    echo "  • $item"
  done
  return 1
}

main() {
  echo "Hermes Agent — full uninstall (macOS)"
  echo

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "Dry run — no files will be changed."
    echo
  elif [[ "$ASSUME_YES" -ne 1 ]]; then
    echo "This permanently deletes Hermes Agent, config, gateway, and desktop app."
    echo "Homebrew ripgrep and other shared tools are NOT removed."
    echo
    printf "Type 'yes' to continue: "
    local confirm
    read -r confirm
    if [[ "$confirm" != yes ]]; then
      echo "Cancelled."
      exit 0
    fi
    echo
  fi

  stop_processes
  run_official_uninstall
  manual_cleanup
  cleanup_extra_artifacts
  cleanup_uv_python
  verify_and_report
}

main "$@"
