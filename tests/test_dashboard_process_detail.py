#!/usr/bin/env python3
"""Dashboard process detail block."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from autopilot_dashboard import (  # noqa: E402
    PipelineProc,
    PrefetchImportState,
    format_process_detail_block,
    log_last_activity,
    resolve_watchdog_snapshot,
)


class DashboardProcessDetailTests(unittest.TestCase):
    def test_watchdog_snapshot_from_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            wd_log = temp / "publish_all_watchdog.log"
            wd_log.write_text(
                "2026-07-25 12:49:16 [watchdog] Watchdog started "
                "(restart=60s, stop_on_success=1, once=0, stall=7200s, awake=1)\n"
                "2026-07-25 12:49:17 [watchdog] === Attempt 1 ===\n"
                "2026-07-25 12:49:18 [watchdog] Starting: publish_all.sh\n",
                encoding="utf-8",
            )
            procs = [PipelineProc(99, 120, "watchdog", "watch_publish_all_70mai.sh")]
            snap = resolve_watchdog_snapshot(temp, procs)
            self.assertTrue(snap.alive)
            self.assertEqual(snap.pid, 99)
            self.assertEqual(snap.attempt, 1)
            self.assertEqual(snap.restart_sec, 60)
            self.assertTrue(snap.stop_on_success)
            self.assertTrue(snap.awake)

    def test_format_process_detail_shows_import_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            (temp / "publish_all.log").write_text(
                f"{now}   [copy] Front 12/254: PA20250104.MP4 (126 MB) SD→SSD\n",
                encoding="utf-8",
            )
            (temp / "publish_all_watchdog.log").write_text(
                f"{now} [watchdog] Watchdog started (restart=60s, stop_on_success=1)\n"
                f"{now} [watchdog] === Attempt 1 ===\n",
                encoding="utf-8",
            )
            procs = [
                PipelineProc(1, 60, "watchdog", "watch_publish_all_70mai.sh"),
                PipelineProc(2, 30, "autopilot", "publish_all_70mai.py --wait"),
                PipelineProc(
                    3,
                    20,
                    "import",
                    "import_70mai.py --types Parking --output video/Output",
                ),
            ]
            lines = format_process_detail_block(
                temp_dir=temp,
                video_dir=None,
                procs=procs,
                log_fallback={"copy": "Front 12/254 PA20250104.MP4"},
            )
            text = "\n".join(lines)
            self.assertIn("процессы:", text)
            self.assertIn("watchdog", text)
            self.assertIn("import", text)
            self.assertIn("Parking", text)
            self.assertIn("12/254", text)
            self.assertIn("log", text)

    def test_log_last_activity_parses_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            ts = datetime.now() - timedelta(seconds=30)
            (temp / "publish_all.log").write_text(
                f"{ts:%Y-%m-%d %H:%M:%S}   [copy] Front 1/254: clip.MP4\n",
                encoding="utf-8",
            )
            age, snip = log_last_activity(temp)
            self.assertIsNotNone(age)
            assert age is not None
            self.assertGreaterEqual(age, 25)
            self.assertLess(age, 120)
            self.assertIn("[copy]", snip)

    def test_stopped_autopilot_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            old = datetime.now() - timedelta(hours=4)
            (temp / "publish_all_watchdog.log").write_text(
                f"{old:%Y-%m-%d %H:%M:%S} [watchdog] === Attempt 1 ===\n",
                encoding="utf-8",
            )
            lines = format_process_detail_block(
                temp_dir=temp,
                video_dir=None,
                procs=[],
            )
            text = "\n".join(lines)
            self.assertIn("✗ watchdog", text)
            self.assertIn("автопилот остановлен", text)


if __name__ == "__main__":
    unittest.main()
