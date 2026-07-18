#!/usr/bin/env python3
"""Tests for Parking/Event pipeline self-repair."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from import_70mai import MERGE_DURATION_TOLERANCE_EVENT, is_valid_merge_output
from pipeline_repair import (
    HealthIssue,
    capped_compose_duration,
    diagnose_chunk,
    remediate,
)
from plan_estimate import ChunkPlan, Trip


@dataclass
class _FakeClip:
    path: Path
    start: datetime
    end: datetime
    camera: str
    duration: float | None


class PipelineRepairTests(unittest.TestCase):
    def _parking_chunk(self, duration_sec: float = 7309.0) -> ChunkPlan:
        start = datetime(2025, 8, 10, 8, 50, 33)
        trip = Trip(
            record_type="Parking",
            index=1,
            start=start,
            end=start + timedelta(seconds=duration_sec),
            clip_count=496,
            duration_sec=duration_sec,
        )
        return ChunkPlan(record_type="Parking", index=1, trips=(trip,))

    def test_merge_short_detected(self) -> None:
        chunk = self._parking_chunk(7309.0)
        start = chunk.start
        front = [
            _FakeClip(
                path=Path("PA_front.mp4"),
                start=start,
                end=start + timedelta(seconds=6889.6),
                camera="Front",
                duration=6889.6,
            )
        ]
        back = [
            _FakeClip(
                path=Path("PA_back.mp4"),
                start=start,
                end=start + timedelta(seconds=7314.0),
                camera="Back",
                duration=7314.0,
            )
        ]

        def fake_scan(video_dir, camera, *, record_type="Normal", probe=True):
            return front if camera == "Front" else back

        def fake_plan(clips, wall_start, duration, sync_offset):
            from compose_70mai import Segment

            dur = clips[0].duration or 0.0
            if duration > dur + 0.01:
                raise ValueError(
                    f"No merged clip covers "
                    f"{(wall_start + timedelta(seconds=dur)):%Y-%m-%d %H:%M:%S}"
                )
            return [Segment(path=clips[0].path, ss=0.0, duration=min(duration, dur))]

        with (
            mock.patch(
                "compose_70mai.scan_merged_clips", side_effect=fake_scan
            ),
            mock.patch("compose_70mai.plan_segments", side_effect=fake_plan),
        ):
            issues = diagnose_chunk(None, Path("video/Output"), chunk)
        codes = {i.code for i in issues}
        self.assertIn("merge_short", codes)
        self.assertTrue(any(i.camera == "Front" for i in issues if i.code == "merge_short"))

    def test_merge_short_is_warn_when_aligned(self) -> None:
        chunk = self._parking_chunk(7309.0)
        start = chunk.start
        front = [
            _FakeClip(
                path=Path("PA_front.mp4"),
                start=start,
                end=start + timedelta(seconds=6889.6),
                camera="Front",
                duration=6889.6,
            )
        ]
        back = [
            _FakeClip(
                path=Path("PA_back.mp4"),
                start=start,
                end=start + timedelta(seconds=7314.0),
                camera="Back",
                duration=7314.0,
            )
        ]

        def fake_scan(video_dir, camera, *, record_type="Normal", probe=True):
            return front if camera == "Front" else back

        def fake_plan(clips, wall_start, duration, sync_offset):
            from compose_70mai import Segment

            dur = clips[0].duration or 0.0
            return [Segment(path=clips[0].path, ss=0.0, duration=min(duration, dur))]

        with (
            mock.patch("compose_70mai.scan_merged_clips", side_effect=fake_scan),
            mock.patch("compose_70mai.plan_segments", side_effect=fake_plan),
            mock.patch("pipeline_repair._aligned_ready", return_value=True),
            mock.patch("pipeline_repair._manifest_matches_file", return_value=True),
        ):
            issues = diagnose_chunk(None, Path("video/Output"), chunk)
        # With slot-aligned manifests, coverage shortfalls are warnings, not
        # blockers — nothing forces a rebuild.
        blockers = [i for i in issues if i.severity == "blocker"]
        self.assertEqual(blockers, [])
        self.assertTrue(any(i.code == "merge_short" for i in issues))

    def test_manifest_missing_blocks_when_not_aligned(self) -> None:
        chunk = self._parking_chunk(7309.0)
        start = chunk.start
        front = [
            _FakeClip(
                path=Path("PA_front.mp4"),
                start=start,
                end=start + timedelta(seconds=7309.6),
                camera="Front",
                duration=7309.6,
            )
        ]
        back = [
            _FakeClip(
                path=Path("PA_back.mp4"),
                start=start,
                end=start + timedelta(seconds=7309.6),
                camera="Back",
                duration=7309.6,
            )
        ]

        def fake_scan(video_dir, camera, *, record_type="Normal", probe=True):
            return front if camera == "Front" else back

        def fake_plan(clips, wall_start, duration, sync_offset):
            from compose_70mai import Segment

            return [Segment(path=clips[0].path, ss=0.0, duration=duration)]

        with (
            mock.patch("compose_70mai.scan_merged_clips", side_effect=fake_scan),
            mock.patch("compose_70mai.plan_segments", side_effect=fake_plan),
            mock.patch("pipeline_repair._manifest_matches_file", return_value=False),
        ):
            issues = diagnose_chunk(None, Path("video/Output"), chunk)
        codes = {i.code for i in issues if i.severity == "blocker"}
        self.assertIn("manifest_missing", codes)

    def test_remediate_deletes_and_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            merge = root / "Parking" / "Front" / "PA_20250810-085033_021325_F.mp4"
            merge.parent.mkdir(parents=True)
            merge.write_bytes(b"x" * 100)
            temp_dir = root / ".publish_tmp"
            store = mock.Mock()
            store.compact_event_state.return_value = 2
            issues = [
                HealthIssue(
                    code="merge_short",
                    record_type="Parking",
                    camera="Front",
                    severity="blocker",
                    message="short",
                    remediation="rebuild_merge",
                    path=merge,
                )
            ]
            actions = remediate(
                issues,
                video_dir=root,
                temp_dir=temp_dir,
                import_store=store,
                dry_run=False,
            )
            self.assertFalse(merge.exists())
            store.invalidate_merge.assert_called_once()
            self.assertTrue(any("deleted" in a or "rebuilt" in a for a in actions))

    def test_capped_compose_duration(self) -> None:
        self.assertEqual(
            capped_compose_duration(7309.0, 6889.6, 7314.0),
            6889.6,
        )
        self.assertEqual(capped_compose_duration(100.0, None, None), 100.0)

    def test_no_repair_when_uploaded(self) -> None:
        chunk = self._parking_chunk()
        issues = diagnose_chunk(
            None, Path("video/Output"), chunk, uploaded=True
        )
        self.assertEqual(issues, [])

    def test_event_tolerance_rejects_94_percent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PA_fake.mp4"
            path.write_bytes(b"0" * 20_000)
            with mock.patch(
                "import_70mai.probe_duration_safe", return_value=6889.6
            ):
                ok_normal = is_valid_merge_output(
                    path, "ffprobe", 7309.0, record_type="Normal"
                )
                ok_parking = is_valid_merge_output(
                    path, "ffprobe", 7309.0, record_type="Parking"
                )
            self.assertTrue(ok_normal)  # 94% >= 85%
            self.assertFalse(ok_parking)  # 94% < 98%
            self.assertGreaterEqual(MERGE_DURATION_TOLERANCE_EVENT, 0.98)

    def test_chunk_merges_ready_probe_catches_gap(self) -> None:
        from publish_all_70mai import chunk_merges_ready

        chunk = self._parking_chunk(7309.0)
        start = chunk.start
        front = [
            _FakeClip(
                path=Path("PA_front.mp4"),
                start=start,
                end=start + timedelta(seconds=6889.6),
                camera="Front",
                duration=6889.6,
            )
        ]
        back = [
            _FakeClip(
                path=Path("PA_back.mp4"),
                start=start,
                end=start + timedelta(seconds=7314.0),
                camera="Back",
                duration=7314.0,
            )
        ]

        def fake_scan(video_dir, camera, *, record_type="Normal", probe=True):
            self.assertTrue(probe)
            return front if camera == "Front" else back

        def fake_plan(clips, wall_start, duration, sync_offset):
            raise ValueError("No merged clip covers 2025-08-10 10:45:22")

        with (
            mock.patch(
                "compose_70mai.scan_merged_clips", side_effect=fake_scan
            ),
            mock.patch(
                "compose_70mai.plan_segments", side_effect=fake_plan
            ),
        ):
            self.assertFalse(chunk_merges_ready(Path("video/Output"), chunk))


if __name__ == "__main__":
    unittest.main()
