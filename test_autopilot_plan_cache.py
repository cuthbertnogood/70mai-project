#!/usr/bin/env python3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from plan_estimate import ChunkPlan, Trip, load_autopilot_plan, save_autopilot_plan


class AutopilotPlanCacheTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        trip = Trip(
            record_type="Normal",
            index=1,
            start=datetime(2026, 7, 6, 10, 36, 0),
            end=datetime(2026, 7, 6, 10, 38, 0),
            clip_count=3,
            duration_sec=177.0,
        )
        chunks = [ChunkPlan(record_type="Normal", index=1, trips=(trip,))]
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            path = save_autopilot_plan(
                temp,
                source=Path("/Volumes/Untitled"),
                types=["Normal"],
                chunks=chunks,
                chunk_minutes=120,
                session_gap=120.0,
            )
            self.assertTrue(path.is_file())
            loaded = load_autopilot_plan(temp)
            assert loaded is not None
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].index, 1)
            self.assertEqual(loaded[0].trips[0].clip_count, 3)
            self.assertEqual(loaded[0].trips[0].start, trip.start)


if __name__ == "__main__":
    unittest.main()
