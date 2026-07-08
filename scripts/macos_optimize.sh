#!/usr/bin/env bash
# macOS tuning for MacBook Air 2018 (8 GB RAM, Intel i5).
# Safe to re-run. Does not touch 70mai pipeline data except optional cleanup.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIDEO_DIR="$ROOT/video"
UID_NUM="$(id -u)"

log() { printf '→ %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }

log "Spotlight: exclude video/ from indexing"
touch "$VIDEO_DIR/.metadata_never_index"
ok "Created $VIDEO_DIR/.metadata_never_index"

log "Removing Google Keystone / Updater launch agents"
for label in com.google.keystone.agent com.google.keystone.xpcservice com.google.GoogleUpdater.wake; do
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
done
rm -f "$HOME/Library/LaunchAgents/com.google.keystone.agent.plist" \
      "$HOME/Library/LaunchAgents/com.google.keystone.xpcservice.plist" \
      "$HOME/Library/LaunchAgents/com.google.GoogleUpdater.wake.plist" 2>/dev/null || true
if launchctl list 2>/dev/null | grep -qi keystone; then
  echo "⚠ Some Google updater services still registered (Chrome may recreate on update)"
else
  ok "Google updater agents removed"
fi

log "Accessibility (Reduce Motion / Transparency)"
if defaults -currentHost write com.apple.universalaccess reduceMotion -bool true 2>/dev/null \
   && defaults -currentHost write com.apple.universalaccess reduceTransparency -bool true 2>/dev/null; then
  ok "Reduce Motion + Transparency enabled (log out/in or reboot to apply UI)"
else
  echo "⚠ Set manually: System Settings → Accessibility → Display"
  echo "  • Reduce motion"
  echo "  • Reduce transparency"
fi

echo ""
echo "Manual (recommended on 8 GB RAM):"
echo "  • Chrome → chrome://settings/performance → Memory Saver ON"
echo "  • During compose: close extra Chrome tabs; restart Cursor if Renderer >1 GB"
echo "  • Free disk: ./run scripts/cleanup_uploaded_sources.py  (dry-run)"
echo "                ./run scripts/cleanup_uploaded_sources.py --apply"
echo ""
echo "70mai compose on this Mac:"
echo "  • Default profile balanced = VideoToolbox HW encode (T2 chip)"
echo "  • Autopilot: ./scripts/publish_all_70mai.sh --profile balanced --prune-merged after-upload"
echo ""
df -h /System/Volumes/Data | tail -1
