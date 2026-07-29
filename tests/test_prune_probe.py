#!/usr/bin/env python3
"""Prune must use probed merge duration, not filename end-time."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from publish_70mai import prune_merged_for_trip


class PruneProbeTests(unittest.TestCase):
    def test_prune_keeps_merge_when_probed_end_past_trip(self) -> None:
        """Filename end is inside the trip window; real duration extends past hi."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            merge_dir = root / "Normal" / "Front"
            merge_dir.mkdir(parents=True)
            # last-clip start 13:10:19 → filename end; start 13:01:19
            name = "NO_20260425-130119_131019_F.mp4"
            path = merge_dir / name
            path.write_bytes(b"fake")

            trip_start = datetime(2026, 4, 25, 13, 0, 0)
            trip_end = datetime(2026, 4, 25, 13, 10, 19)
            # With probe=False, filename end == trip_end ≤ hi → would delete.
            # With probe=True and duration=900s, end=13:16:19 > hi (trip_end+120s).
            with mock.patch(
                "compose_70mai.probe_duration", return_value=900.0
            ):
                freed = prune_merged_for_trip(
                    root, "Normal", trip_start, trip_end
                )
            self.assertEqual(freed, 0)
            self.assertTrue(path.exists())

    def test_prune_deletes_when_probed_end_inside_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            merge_dir = root / "Normal" / "Front"
            merge_dir.mkdir(parents=True)
            name = "NO_20260425-130119_131019_F.mp4"
            path = merge_dir / name
            path.write_bytes(b"x" * 100)

            trip_start = datetime(2026, 4, 25, 13, 0, 0)
            trip_end = datetime(2026, 4, 25, 14, 0, 0)
            with mock.patch(
                "compose_70mai.probe_duration", return_value=600.0
            ):
                freed = prune_merged_for_trip(
                    root, "Normal", trip_start, trip_end
                )
            self.assertEqual(freed, 100)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
