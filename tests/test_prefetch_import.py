#!/usr/bin/env python3
"""Import command builder for publish_all_70mai (sync import per chunk)."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from plan_estimate import ChunkPlan, Trip  # noqa: E402


def _trip(record_type: str, index: int, hour: int) -> Trip:
    start = datetime(2025, 7, 1, hour, 0, 0)
    end = start.replace(hour=hour, minute=59)
    return Trip(
        record_type=record_type,
        index=index,
        start=start,
        end=end,
        clip_count=6,
        duration_sec=3540.0,
    )


def _chunk(record_type: str, index: int, hour: int) -> ChunkPlan:
    return ChunkPlan(record_type=record_type, index=index, trips=(_trip(record_type, index, hour),))


class BuildImportCmdTests(unittest.TestCase):
    @patch("runtime_config.import_settings", return_value={})
    def test_normal_window_and_status_dir(self, _imp) -> None:
        from publish_all_70mai import build_import_cmd

        chunk = _chunk("Normal", 2, 10)
        cmd = build_import_cmd(
            "python3",
            Path("/Volumes/Untitled"),
            "Normal",
            Path("video/Output"),
            chunk,
            session_gap=120.0,
            state_on_sd=True,
            temp_dir=Path("video/Output/.publish_tmp"),
        )
        self.assertIn("--from", cmd)
        self.assertIn("2025-07-01 10:00:00", cmd)
        self.assertIn("--status-dir", cmd)
        self.assertIn("--state-on-sd", cmd)

    @patch("runtime_config.import_settings", return_value={})
    def test_event_has_no_time_window(self, _imp) -> None:
        from publish_all_70mai import build_import_cmd

        chunk = _chunk("Event", 1, 8)
        cmd = build_import_cmd(
            "python3",
            Path("/Volumes/Untitled"),
            "Event",
            Path("video/Output"),
            chunk,
            session_gap=120.0,
            state_on_sd=False,
            temp_dir=Path("video/Output/.publish_tmp"),
        )
        self.assertNotIn("--from", cmd)
        self.assertIn("--status-dir", cmd)
        self.assertIn("--types", cmd)
        idx = cmd.index("--types")
        self.assertEqual(cmd[idx + 1], "Event")


if __name__ == "__main__":
    unittest.main()
