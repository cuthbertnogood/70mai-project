#!/usr/bin/env python3
"""Trip.end = end of footage; Merge readiness uses 98% coverage."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from plan_estimate import ChunkPlan, Trip, trips_from_clips


class _Clip:
    def __init__(self, ts: datetime, duration: float, sequence: int = 1) -> None:
        self.timestamp = ts
        self.duration = duration
        self.sequence = sequence
        self.camera = "Front"


class TripEndTests(unittest.TestCase):
    def test_single_clip_end_includes_duration(self) -> None:
        start = datetime(2026, 7, 29, 23, 10, 53)
        clips = [_Clip(start, 7.48)]
        trips = trips_from_clips("Normal", clips, session_gap=120.0)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].start, start)
        self.assertEqual(trips[0].end, start + timedelta(seconds=7.48))
        self.assertAlmostEqual(trips[0].duration_sec, 7.48)
        self.assertGreater(trips[0].end, trips[0].start)

    def test_multi_clip_end_is_last_clip_end(self) -> None:
        t0 = datetime(2026, 8, 1, 17, 20, 16)
        clips = [
            _Clip(t0, 60.0, 1),
            _Clip(t0 + timedelta(seconds=60), 60.0, 2),
            _Clip(t0 + timedelta(seconds=120), 60.0, 3),
            _Clip(t0 + timedelta(seconds=180), 60.0, 4),
            _Clip(t0 + timedelta(seconds=240), 20.0, 5),
        ]
        trips = trips_from_clips("Normal", clips, session_gap=120.0)
        self.assertEqual(len(trips), 1)
        # Old bug: end == last.timestamp (t0+240); correct: t0+260
        self.assertEqual(trips[0].end, t0 + timedelta(seconds=260))
        self.assertAlmostEqual(trips[0].duration_sec, 260.0)
        self.assertAlmostEqual(
            (trips[0].end - trips[0].start).total_seconds(),
            trips[0].duration_sec,
        )


class MergeReadinessCoverageTests(unittest.TestCase):
    def test_normal_timeline_partial_coverage_not_ready(self) -> None:
        from publish_all_70mai import chunk_merges_ready

        start = datetime(2026, 8, 1, 17, 20, 16)
        trip = Trip(
            record_type="Normal",
            index=12,
            start=start,
            end=start + timedelta(seconds=260),
            clip_count=5,
            duration_sec=260.0,
        )
        chunk = ChunkPlan(record_type="Normal", index=3, trips=(trip,))

        class _FakeMerged:
            def __init__(self, path: Path) -> None:
                self.path = path

        front = [_FakeMerged(Path("NO_front.mp4"))]
        back = [_FakeMerged(Path("NO_back.mp4"))]

        def fake_scan(video_dir, camera, *, record_type="Normal", probe=True):
            return front if camera == "Front" else back

        # Aligned total only 240s of 260s planned → below 98%.
        fake_aligned = (
            [],
            [],
            240.0,
            Path("f.mp4"),
            Path("b.mp4"),
            {},
        )

        with (
            mock.patch("compose_70mai.scan_merged_clips", side_effect=fake_scan),
            mock.patch(
                "clip_timeline.merges_timeline_ready",
                return_value=(True, ""),
            ),
            mock.patch(
                "clip_timeline.load_manifest",
                return_value=mock.Mock(clips=[object()]),
            ),
            mock.patch(
                "compose_2cam_70mai.build_aligned_lanes",
                return_value=fake_aligned,
            ),
        ):
            self.assertFalse(chunk_merges_ready(Path("video/Output"), chunk))

    def test_normal_timeline_full_coverage_ready(self) -> None:
        from publish_all_70mai import chunk_merges_ready

        start = datetime(2026, 8, 1, 17, 20, 16)
        trip = Trip(
            record_type="Normal",
            index=12,
            start=start,
            end=start + timedelta(seconds=260),
            clip_count=5,
            duration_sec=260.0,
        )
        chunk = ChunkPlan(record_type="Normal", index=3, trips=(trip,))

        class _FakeMerged:
            def __init__(self, path: Path) -> None:
                self.path = path

        front = [_FakeMerged(Path("NO_front.mp4"))]
        back = [_FakeMerged(Path("NO_back.mp4"))]

        def fake_scan(video_dir, camera, *, record_type="Normal", probe=True):
            return front if camera == "Front" else back

        fake_aligned = (
            [],
            [],
            260.0,
            Path("f.mp4"),
            Path("b.mp4"),
            {},
        )

        with (
            mock.patch("compose_70mai.scan_merged_clips", side_effect=fake_scan),
            mock.patch(
                "clip_timeline.merges_timeline_ready",
                return_value=(True, ""),
            ),
            mock.patch(
                "clip_timeline.load_manifest",
                return_value=mock.Mock(clips=[object()]),
            ),
            mock.patch(
                "compose_2cam_70mai.build_aligned_lanes",
                return_value=fake_aligned,
            ),
        ):
            self.assertTrue(chunk_merges_ready(Path("video/Output"), chunk))


if __name__ == "__main__":
    unittest.main()
