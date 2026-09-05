#!/usr/bin/env python3
"""Tests for post-upload SSD cleanup."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from plan_estimate import ChunkPlan, Trip  # noqa: E402
from publish_70mai import (  # noqa: E402
    cleanup_after_successful_uploads,
    type_fully_uploaded,
)
from datetime import datetime, timedelta  # noqa: E402


class CleanupUploadedTests(unittest.TestCase):
    def test_cleanup_removes_parking_compose_and_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video"
            temp = root / ".publish_tmp"
            part = temp / "Parking" / "part_01.mp4"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"x" * 1000)
            stage = (
                video
                / "Parking"
                / "Front"
                / ".merge_stage"
                / "PA_x"
                / "_part_0.mp4"
            )
            stage.parent.mkdir(parents=True)
            stage.write_bytes(b"y" * 2000)
            merge = video / "Parking" / "Front" / "PA_x.mp4"
            merge.write_bytes(b"z" * 3000)

            start = datetime(2025, 1, 2, 16, 33, 49)
            trip = Trip(
                record_type="Parking",
                index=1,
                start=start,
                end=start + timedelta(seconds=100),
                clip_count=2,
                duration_sec=100.0,
            )
            chunk = ChunkPlan(record_type="Parking", index=1, trips=(trip,))
            state = {
                "parts": [
                    {
                        "record_type": "Parking",
                        "index": 1,
                        "wall_start": start.isoformat(),
                        "trip_indices": [1],
                        "duration_sec": 100.0,
                        "uploaded": True,
                        "video_id": "abc",
                    }
                ],
                "trip_parts": [],
            }
            self.assertTrue(type_fully_uploaded(state, "Parking", [chunk]))
            freed = cleanup_after_successful_uploads(
                video_dir=video,
                temp_dir=temp,
                state=state,
                chunks=[chunk],
                types=["Parking"],
                prune_merged=True,
            )
            self.assertGreater(freed, 0)
            self.assertFalse(part.exists())
            self.assertFalse(merge.exists())
            self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
