#!/usr/bin/env python3
"""Regression: merge names that cross midnight must parse with end > start."""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from compose_70mai import parse_event_export_file, parse_merged_file
from import_70mai import Clip, output_name


class MidnightMergedParseTests(unittest.TestCase):
    def test_parse_merged_cross_midnight_end_after_start(self) -> None:
        # output_name stores end as %H%M%S only; chunk 23:59 → 00:19 next day.
        path = Path("NO_20260425-235900_001900_F.mp4")
        clip = parse_merged_file(path)
        self.assertIsNotNone(clip)
        assert clip is not None
        self.assertEqual(clip.start, datetime(2026, 4, 25, 23, 59, 0))
        self.assertEqual(clip.end, datetime(2026, 4, 26, 0, 19, 0))
        self.assertGreater(clip.end, clip.start)

    def test_parse_parking_cross_midnight(self) -> None:
        path = Path("PA_20260425-235500_000500_B.mp4")
        clip = parse_merged_file(path)
        self.assertIsNotNone(clip)
        assert clip is not None
        self.assertEqual(clip.end, datetime(2026, 4, 26, 0, 5, 0))
        self.assertGreater(clip.end, clip.start)

    def test_parse_event_merged_cross_midnight(self) -> None:
        path = Path("EV_20260425-235900_001000_F.mp4")
        clip = parse_event_export_file(path)
        self.assertIsNotNone(clip)
        assert clip is not None
        self.assertEqual(clip.end, datetime(2026, 4, 26, 0, 10, 0))
        self.assertGreater(clip.end, clip.start)

    def test_same_day_unchanged(self) -> None:
        path = Path("NO_20260425-130119_131019_F.mp4")
        clip = parse_merged_file(path)
        self.assertIsNotNone(clip)
        assert clip is not None
        self.assertEqual(clip.start, datetime(2026, 4, 25, 13, 1, 19))
        self.assertEqual(clip.end, datetime(2026, 4, 25, 13, 10, 19))

    def test_output_name_roundtrip_cross_midnight(self) -> None:
        first = Clip(
            path=Path("a.MP4"),
            record_type="Normal",
            camera="Front",
            timestamp=datetime(2026, 4, 25, 23, 59, 0),
            sequence=1,
        )
        last = Clip(
            path=Path("b.MP4"),
            record_type="Normal",
            camera="Front",
            timestamp=datetime(2026, 4, 26, 0, 19, 0),
            sequence=2,
        )
        name = output_name([first, last])
        self.assertEqual(name, "NO_20260425-235900_001900_F.mp4")
        parsed = parse_merged_file(Path(name))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.start, first.timestamp)
        self.assertEqual(parsed.end, last.timestamp)


if __name__ == "__main__":
    unittest.main()
