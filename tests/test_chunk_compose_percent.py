"""Chunk-level compose % (no reset when the next trip starts)."""

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

from autopilot_dashboard import TripRow, chunk_compose_percent  # noqa: E402


def _row(trip_index: int, dur: float) -> TripRow:
    base = datetime(2026, 7, 14, 21, 54)
    end = base + timedelta(seconds=dur)
    return TripRow(
        key=f"Normal:1:{trip_index}",
        record_type="Normal",
        chunk_index=1,
        trip_index=trip_index,
        label=f"trip {trip_index}",
        duration_sec=dur,
        trip_start=base,
        trip_end=end,
    )


class ChunkComposePercentTests(unittest.TestCase):
    def test_weighted_across_trips(self) -> None:
        rows = [_row(1, 600.0), _row(2, 600.0), _row(3, 600.0)]
        st = {
            "record_type": "Normal",
            "chunk_index": 1,
            "trip_index": 2,
            "phase": "compose",
            "percent": 50.0,
        }
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            done = temp_dir / "Normal" / "chunk_01" / "trip_01.mp4"
            done.parent.mkdir(parents=True)
            # Minimal file — fraction falls back to 0 without ffprobe; use size path
            done.write_bytes(b"x" * 2_000_000)
            pct = chunk_compose_percent(rows, temp_dir, st)
        self.assertIsNotNone(pct)
        assert pct is not None
        # trip 1 incomplete on disk → 0 + 50% of trip2 weight ≈ 16.7%
        self.assertGreater(pct, 10.0)
        self.assertLess(pct, 40.0)

    def test_single_trip_uses_raw_percent(self) -> None:
        rows = [_row(1, 600.0)]
        st = {
            "record_type": "Normal",
            "chunk_index": 1,
            "trip_index": 1,
            "percent": 42.0,
        }
        with tempfile.TemporaryDirectory() as td:
            pct = chunk_compose_percent(rows, Path(td), st)
        self.assertAlmostEqual(pct, 42.0)


if __name__ == "__main__":
    unittest.main()
