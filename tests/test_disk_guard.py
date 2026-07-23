#!/usr/bin/env python3
"""Disk guard auto-recovery (wait upload, prune merged/composed, retry)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from plan_estimate import ChunkPlan, Trip  # noqa: E402


def _trip(index: int) -> Trip:
    from datetime import datetime

    start = datetime(2025, 7, 1, 10, 0, 0)
    return Trip(
        record_type="Normal",
        index=index,
        start=start,
        end=start.replace(hour=10, minute=59),
        clip_count=6,
        duration_sec=3540.0,
    )


def _chunk(index: int = 1) -> ChunkPlan:
    return ChunkPlan(record_type="Normal", index=index, trips=(_trip(index),))


class DiskGuardTests(unittest.TestCase):
    def test_prune_uploaded_composed_deletes_trip_mp4(self) -> None:
        from publish_70mai import mark_trip_state, prune_uploaded_composed

        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            trip_path = temp / "Normal" / "chunk_01" / "trip_01.mp4"
            trip_path.parent.mkdir(parents=True)
            trip_path.write_bytes(b"x" * 1000)
            state: dict = {"trip_parts": []}
            mark_trip_state(
                state,
                record_type="Normal",
                chunk_index=1,
                trip_index=1,
                video_id="abc",
                uploaded=True,
                output_path=trip_path,
            )
            freed = prune_uploaded_composed(temp, state, [_chunk()])
            self.assertGreaterEqual(freed, 1000)
            self.assertFalse(trip_path.is_file())

    @patch("publish_70mai.time.sleep")
    @patch("publish_70mai.free_disk_gb")
    def test_guard_recovers_after_prune(self, mock_free, _sleep) -> None:
        from publish_70mai import guard_free_disk

        mock_free.side_effect = [5.0, 5.0, 25.0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_dir = root / "video"
            video_dir.mkdir()
            temp_dir = root / "tmp"
            temp_dir.mkdir()
            state: dict = {"trip_parts": []}
            guard_free_disk(
                root,
                20.0,
                None,
                state=state,
                chunks=[_chunk()],
                video_dir=video_dir,
                temp_dir=temp_dir,
                prune_merged="off",
                max_attempts=2,
                retry_sec=0.0,
            )
        self.assertGreaterEqual(mock_free.call_count, 2)

    @patch("publish_70mai.free_disk_gb", return_value=5.0)
    @patch("publish_70mai.time.sleep")
    def test_guard_raises_after_exhausted_retries(self, _sleep, _free) -> None:
        from publish_70mai import guard_free_disk

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(RuntimeError) as ctx:
                guard_free_disk(
                    root,
                    20.0,
                    None,
                    max_attempts=2,
                    retry_sec=0.0,
                )
        self.assertIn("cannot compose", str(ctx.exception))

    def test_upload_checkpoint_before_cleanup(self) -> None:
        from publish_70mai import upload_and_cleanup

        saved: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trip_01.mp4"
            path.write_bytes(b"video")
            session_dir = Path(tmp)
            with patch("publish_70mai.upload_video", return_value="vid123"):
                with patch("publish_70mai.post_video_comment"):
                    with patch("publish_70mai.cleanup_uploaded_file") as mock_clean:
                        upload_and_cleanup(
                            path,
                            "title",
                            privacy="private",
                            credentials=Path(tmp) / "c.json",
                            token=Path(tmp) / "t.json",
                            session_dir=session_dir,
                            resume_upload=False,
                            diag_log=None,
                            keep=False,
                            playlist_id=None,
                            playlist_title="",
                            on_video_id=saved.append,
                        )
                        self.assertEqual(saved, ["vid123"])
                        mock_clean.assert_called_once()


if __name__ == "__main__":
    unittest.main()
