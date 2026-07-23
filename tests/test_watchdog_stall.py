#!/usr/bin/env python3
"""Watchdog stall detection helpers (compose progress + log activity)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WATCH_SCRIPT = ROOT / "scripts" / "watch_publish_all_70mai.sh"

# Minimal bash harness: source helper functions from the watchdog script.
_BASH_HELPERS = r"""
set -euo pipefail
LOG_DIR="$1"
AUTOPILOT_LOG="$LOG_DIR/publish_all.log"
LOG_ACTIVE_SEC=600

compose_progress_bytes() {
  local total=0 sz f
  for f in \
    "$LOG_DIR"/chunk_*/trip_*.mp4 \
    "$LOG_DIR"/*/chunk_*/trip_*.mp4 \
    "$LOG_DIR"/*/part_*.mp4
  do
    [[ -f "$f" ]] || continue
    sz=$(stat -f%z "$f" 2>/dev/null || echo 0)
    total=$(( total + sz ))
  done
  echo "$total"
}

compose_progress_bytes
"""


class WatchdogStallTests(unittest.TestCase):
    def test_compose_progress_sums_typed_and_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            typed = log_dir / "Normal" / "chunk_02" / "trip_01.mp4"
            typed.parent.mkdir(parents=True)
            typed.write_bytes(b"a" * 500)
            legacy = log_dir / "chunk_01" / "trip_01.mp4"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"b" * 300)
            part = log_dir / "Event" / "part_01.mp4"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"c" * 200)

            proc = subprocess.run(
                ["bash", "-c", _BASH_HELPERS, "bash", str(log_dir)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(int(proc.stdout.strip()), 1000)

    def test_watch_script_has_activity_helpers(self) -> None:
        text = WATCH_SCRIPT.read_text(encoding="utf-8")
        for name in (
            "compose_progress_bytes",
            "autopilot_log_recent",
            "status_file_recent",
            "pipeline_children_active",
            "autopilot_has_recent_activity",
        ):
            self.assertIn(f"{name}()", text, msg=f"missing {name}")


if __name__ == "__main__":
    unittest.main()
