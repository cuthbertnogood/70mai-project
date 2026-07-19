#!/usr/bin/env python3
"""Tests for aligned Front/Back timeline (slot pairing + black fill)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from clip_timeline import (
    ClipEntry,
    build_camera_lane,
    build_manifest,
    build_slots,
    clip_key,
    filter_entries_to_window,
    lane_black_seconds,
    lane_duration,
    load_manifest,
    manifest_is_fresh,
    pair_drift_report,
    timeline_duration,
    write_manifest_atomic,
)

BASE = datetime(2025, 8, 10, 8, 50, 33)


def _entry(
    seconds_from_base: int, seq: int, dur: float, merge: str, offset: float = 0.0
) -> ClipEntry:
    wall = BASE + timedelta(seconds=seconds_from_base)
    return ClipEntry(
        key=clip_key(wall, seq),
        wall=wall,
        duration=dur,
        offset=offset,
        source=f"PA{wall:%Y%m%d-%H%M%S}-{seq:06d}.MP4",
        merge=merge,
    )


class _FakeClip:
    def __init__(self, wall: datetime, seq: int, dur: float, name: str) -> None:
        self.timestamp = wall
        self.sequence = seq
        self.duration = dur
        self.path = Path(name)


class SlotAlignmentTests(unittest.TestCase):
    def _pair(self, keys_durs):
        """Build front/back entry lists sharing keys where both present.

        Offsets are cumulative per camera (as real manifests are), so
        contiguous clips from one merge coalesce into a single video span.
        """
        front, back = [], []
        foff = boff = 0.0
        for i, (fdur, bdur) in enumerate(keys_durs):
            if fdur is not None:
                front.append(_entry(i * 60, i + 1, fdur, "F.mp4", offset=foff))
                foff += fdur
            if bdur is not None:
                back.append(_entry(i * 60, i + 1, bdur, "B.mp4", offset=boff))
                boff += bdur
        return front, back

    def test_full_coverage_single_video_span(self) -> None:
        front, back = self._pair([(30.0, 30.0), (30.0, 30.0)])
        slots = build_slots(front, back, mode="slot")
        self.assertEqual(len(slots), 2)
        self.assertAlmostEqual(timeline_duration(slots), 60.0)
        lane = build_camera_lane(slots, "Front")
        # Contiguous same-merge video coalesces to one span.
        self.assertEqual([s.kind for s in lane], ["video"])
        self.assertAlmostEqual(lane_black_seconds(lane), 0.0)

    def test_missing_front_middle_becomes_black(self) -> None:
        front, back = self._pair([(30.0, 30.0), (None, 30.0), (30.0, 30.0)])
        slots = build_slots(front, back, mode="slot")
        self.assertAlmostEqual(timeline_duration(slots), 90.0)
        front_lane = build_camera_lane(slots, "Front")
        back_lane = build_camera_lane(slots, "Back")
        self.assertEqual([s.kind for s in front_lane], ["video", "black", "video"])
        self.assertAlmostEqual(lane_black_seconds(front_lane), 30.0)
        # Back is fully covered.
        self.assertEqual([s.kind for s in back_lane], ["video"])
        self.assertAlmostEqual(lane_duration(front_lane), lane_duration(back_lane))

    def test_missing_front_leading_and_trailing(self) -> None:
        front, back = self._pair([(None, 30.0), (30.0, 30.0), (None, 30.0)])
        slots = build_slots(front, back, mode="slot")
        front_lane = build_camera_lane(slots, "Front")
        self.assertEqual([s.kind for s in front_lane], ["black", "video", "black"])
        self.assertAlmostEqual(lane_black_seconds(front_lane), 60.0)
        self.assertAlmostEqual(lane_duration(front_lane), 90.0)

    def test_short_pair_tail_padded_black(self) -> None:
        # Front clip shorter than Back in same slot → Front tail is black.
        front, back = self._pair([(20.0, 30.0)])
        slots = build_slots(front, back, mode="slot")
        self.assertAlmostEqual(timeline_duration(slots), 30.0)
        front_lane = build_camera_lane(slots, "Front")
        self.assertEqual([s.kind for s in front_lane], ["video", "black"])
        self.assertAlmostEqual(front_lane[0].duration, 20.0)
        self.assertAlmostEqual(front_lane[1].duration, 10.0)

    def test_equal_lane_lengths_prevent_drift(self) -> None:
        front, back = self._pair(
            [(30.0, 30.0), (None, 30.0), (30.0, None), (30.0, 30.0)]
        )
        slots = build_slots(front, back, mode="slot")
        f = build_camera_lane(slots, "Front")
        b = build_camera_lane(slots, "Back")
        self.assertAlmostEqual(lane_duration(f), lane_duration(b))
        self.assertAlmostEqual(lane_duration(f), timeline_duration(slots))

    def test_slot_mode_ignores_calendar_gaps(self) -> None:
        # Two parking events months apart pack back-to-back (no black between).
        front = [
            _entry(0, 1, 30.0, "F.mp4"),
            _entry(90 * 86400, 2, 30.0, "F.mp4"),
        ]
        back = [
            _entry(0, 1, 30.0, "B.mp4"),
            _entry(90 * 86400, 2, 30.0, "B.mp4"),
        ]
        slots = build_slots(front, back, mode="slot")
        self.assertAlmostEqual(timeline_duration(slots), 60.0)
        lane = build_camera_lane(slots, "Front")
        self.assertAlmostEqual(lane_black_seconds(lane), 0.0)

    def test_wall_mode_inserts_gap_black(self) -> None:
        # Normal: a 60s hole in wall-clock becomes black in both lanes.
        front = [_entry(0, 1, 30.0, "F.mp4"), _entry(90, 2, 30.0, "F.mp4")]
        back = [_entry(0, 1, 30.0, "B.mp4"), _entry(90, 2, 30.0, "B.mp4")]
        slots = build_slots(front, back, mode="wall", timeline_start=BASE)
        # 0..30 clip, gap 30..90, 90..120 clip => 120 total
        self.assertAlmostEqual(timeline_duration(slots), 120.0)
        lane = build_camera_lane(slots, "Front")
        self.assertEqual([s.kind for s in lane], ["video", "black", "video"])
        self.assertAlmostEqual(lane_black_seconds(lane), 60.0)

    def test_filter_excludes_prefetch_outside_trip_window(self) -> None:
        trip_start = BASE
        trip_end = BASE + timedelta(hours=2)
        front = [
            _entry(0, 1, 30.0, "F.mp4"),
            _entry(90 * 86400, 2, 30.0, "F.mp4"),
        ]
        back = [
            _entry(0, 1, 30.0, "B.mp4"),
            _entry(90 * 86400, 2, 30.0, "B.mp4"),
        ]
        front_f = filter_entries_to_window(front, trip_start, trip_end)
        back_f = filter_entries_to_window(back, trip_start, trip_end)
        self.assertEqual(len(front_f), 1)
        self.assertEqual(len(back_f), 1)
        slots = build_slots(front_f, back_f, mode="wall", timeline_start=trip_start)
        self.assertAlmostEqual(timeline_duration(slots), 30.0)
        self.assertLess(timeline_duration(slots), trip_end.timestamp() - trip_start.timestamp())

    def test_drift_report(self) -> None:
        front, back = self._pair([(30.0, 30.0), (None, 30.0), (20.0, 30.0)])
        slots = build_slots(front, back, mode="slot")
        report = pair_drift_report(slots)
        self.assertEqual(report["missing_front"], 1)
        self.assertEqual(report["missing_back"], 0)
        self.assertAlmostEqual(report["max_pair_spread"], 10.0)


class ManifestTests(unittest.TestCase):
    def test_roundtrip_and_offsets(self) -> None:
        clips = [
            _FakeClip(BASE, 1, 30.0, "PA...-000001F.MP4"),
            _FakeClip(BASE + timedelta(seconds=60), 2, 25.0, "PA...-000002F.MP4"),
        ]
        manifest = build_manifest(
            record_type="Parking", camera="Front", merge_name="PA_F.mp4", clips=clips
        )
        self.assertEqual(manifest["clips"][0]["offset"], 0.0)
        self.assertEqual(manifest["clips"][1]["offset"], 30.0)
        with tempfile.TemporaryDirectory() as tmp:
            merge = Path(tmp) / "PA_F.mp4"
            merge.write_bytes(b"x")
            write_manifest_atomic(merge, manifest)
            loaded = load_manifest(merge)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(len(loaded.clips), 2)
            self.assertEqual(loaded.clips[1].offset, 30.0)
            self.assertTrue(
                manifest_is_fresh(
                    loaded,
                    expected_clip_count=2,
                    expected_last_clip="PA...-000002F.MP4",
                )
            )
            self.assertFalse(
                manifest_is_fresh(loaded, expected_clip_count=3)
            )

    def test_load_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_manifest(Path(tmp) / "nope.mp4"))


if __name__ == "__main__":
    unittest.main()
