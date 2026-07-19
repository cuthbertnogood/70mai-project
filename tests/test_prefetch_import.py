#!/usr/bin/env python3
"""Prefetch import helpers in publish_all_70mai."""

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
    def test_normal_window_and_live_status(self, _imp) -> None:
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
            live_status=True,
        )
        self.assertIn("--from", cmd)
        self.assertIn("2025-07-01 10:00:00", cmd)
        self.assertIn("--status-dir", cmd)
        self.assertIn("--state-on-sd", cmd)

    @patch("runtime_config.import_settings", return_value={})
    def test_prefetch_omits_status_dir(self, _imp) -> None:
        from publish_all_70mai import build_import_cmd

        chunk = _chunk("Normal", 2, 10)
        cmd = build_import_cmd(
            "python3",
            Path("/Volumes/Untitled"),
            "Normal",
            Path("video/Output"),
            chunk,
            session_gap=120.0,
            state_on_sd=False,
            temp_dir=Path("video/Output/.publish_tmp"),
            live_status=False,
        )
        self.assertNotIn("--status-dir", cmd)

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
            live_status=False,
        )
        self.assertNotIn("--from", cmd)
        self.assertIn("--types", cmd)
        idx = cmd.index("--types")
        self.assertEqual(cmd[idx + 1], "Event")


class NextChunkNeedingImportTests(unittest.TestCase):
    def test_skips_done_and_ready_chunks(self) -> None:
        from publish_all_70mai import next_chunk_needing_import

        chunks = [_chunk("Normal", i, 8 + i) for i in (1, 2, 3)]
        state = {"trip_parts": []}

        def is_done(_state, chunk) -> bool:
            return chunk.index == 2

        def merges_ready(_video_dir, chunk) -> bool:
            return chunk.index == 2

        with patch("publish_all_70mai.chunk_is_done", side_effect=is_done):
            with patch("publish_all_70mai.chunk_merges_ready", side_effect=merges_ready):
                nxt = next_chunk_needing_import(chunks, chunks[0], state, Path("."))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.index, 3)

    def test_returns_none_when_no_pending(self) -> None:
        from publish_all_70mai import next_chunk_needing_import

        chunks = [_chunk("Normal", 1, 8)]
        with patch("publish_all_70mai.chunk_is_done", return_value=True):
            nxt = next_chunk_needing_import(chunks, chunks[0], {}, Path("."))
        self.assertIsNone(nxt)


class TryStartPrefetchTests(unittest.TestCase):
    @patch("publish_all_70mai.start_step_background")
    @patch("publish_70mai.free_disk_gb", return_value=50.0)
    @patch("publish_all_70mai.next_chunk_needing_import")
    def test_starts_when_disk_ok(self, mock_next, _free, mock_start) -> None:
        from publish_all_70mai import BackgroundStep, try_start_prefetch_import

        chunk = _chunk("Normal", 1, 8)
        next_chunk = _chunk("Normal", 2, 10)
        mock_next.return_value = next_chunk
        mock_start.return_value = BackgroundStep(
            proc=object(),  # type: ignore[arg-type]
            log_handle=object(),  # type: ignore[arg-type]
            chunk_index=2,
            record_type="Normal",
        )

        bg = try_start_prefetch_import(
            chunk,
            type_chunks=[chunk, next_chunk],
            type_state={},
            python="python3",
            source=Path("/Volumes/Untitled"),
            record_type="Normal",
            video_dir=Path("video/Output"),
            session_gap=120.0,
            state_on_sd=False,
            temp_dir=Path("video/Output/.publish_tmp"),
            log_path=Path("video/Output/.publish_tmp/publish_all.log"),
            min_free_gb=20.0,
            check_disk=Path("."),
        )
        self.assertIsNotNone(bg)
        mock_start.assert_called_once()
        cmd = mock_start.call_args[0][0]
        self.assertNotIn("--status-dir", cmd)

    @patch("publish_all_70mai.start_step_background")
    @patch("publish_70mai.free_disk_gb", return_value=25.0)
    @patch("publish_all_70mai.next_chunk_needing_import")
    def test_skips_when_disk_low(self, mock_next, _free, mock_start) -> None:
        from publish_all_70mai import try_start_prefetch_import

        chunk = _chunk("Normal", 1, 8)
        mock_next.return_value = _chunk("Normal", 2, 10)

        bg = try_start_prefetch_import(
            chunk,
            type_chunks=[chunk],
            type_state={},
            python="python3",
            source=Path("/Volumes/Untitled"),
            record_type="Normal",
            video_dir=Path("video/Output"),
            session_gap=120.0,
            state_on_sd=False,
            temp_dir=Path("video/Output/.publish_tmp"),
            log_path=Path("video/Output/.publish_tmp/publish_all.log"),
            min_free_gb=20.0,
            check_disk=Path("."),
        )
        self.assertIsNone(bg)
        mock_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
