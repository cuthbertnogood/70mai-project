"""Dashboard local file path helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autopilot_dashboard import (
    TripRow,
    _resolve_local_path,
    _row_show_local_path,
    format_local_files_block,
)


class AutopilotDashboardPathsTests(unittest.TestCase):
    def test_resolve_local_path_keeps_file_after_upload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            temp_dir = root / "video/Output/.publish_tmp"
            chunk = temp_dir / "chunk_01"
            chunk.mkdir(parents=True)
            mp4 = chunk / "trip_01.mp4"
            mp4.write_bytes(b"x" * 1000)
            video_dir = root / "video/Output"
            p = _resolve_local_path(
                temp_dir=temp_dir,
                video_dir=video_dir,
                record_type="Normal",
                chunk_index=1,
                trip_index=1,
                status="done",
                composed_bytes=0,
                merged_bytes=0,
                base=root,
            )
            self.assertTrue(p.endswith("chunk_01/trip_01.mp4"))

    def test_format_local_files_block_skips_uploaded(self):
        rows = [
            TripRow(
                key="n:1:1",
                record_type="Normal",
                chunk_index=1,
                trip_index=1,
                label="trip 1 07-01 10:00",
                duration_sec=3600,
                status="done",
                youtube_url="https://youtu.be/abc",
                local_path="video/Output/.publish_tmp/chunk_01/trip_01.mp4",
                overall_index=1,
            ),
            TripRow(
                key="n:2:1",
                record_type="Normal",
                chunk_index=2,
                trip_index=1,
                label="trip 2 07-01 12:00",
                duration_sec=3600,
                status="pending",
                local_path="video/Output/.publish_tmp/chunk_02/trip_01.mp4",
                overall_index=2,
            ),
        ]
        self.assertFalse(_row_show_local_path(rows[0]))
        self.assertTrue(_row_show_local_path(rows[1]))
        lines = format_local_files_block(rows, term_cols=120)
        text = "\n".join(lines)
        self.assertIn("Локальные файлы", text)
        self.assertIn("chunk_02/trip_01.mp4", text)
        self.assertNotIn("chunk_01/trip_01.mp4", text)

    def test_format_local_files_block_dedupes_same_path(self):
        same = "video/Output/.publish_tmp/chunk_01/trip_01.mp4"
        rows = [
            TripRow(
                key=f"n:{i}:1",
                record_type="Normal",
                chunk_index=i,
                trip_index=1,
                label=f"trip {i}",
                duration_sec=600,
                status="pending",
                local_path=same,
                overall_index=i,
            )
            for i in range(1, 4)
        ]
        lines = format_local_files_block(rows, term_cols=120)
        text = "\n".join(lines)
        self.assertEqual(text.count("chunk_01/trip_01.mp4"), 1)


if __name__ == "__main__":
    unittest.main()
