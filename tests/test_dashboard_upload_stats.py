#!/usr/bin/env python3
"""YouTube upload rollup footer on the dashboard."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from autopilot_dashboard import (  # noqa: E402
    TripRow,
    collect_uploaded_rollups,
    format_upload_stats_block,
    parse_upload_log_stats,
    summarize_youtube_upload_stats,
)


class DashboardUploadStatsTests(unittest.TestCase):
    def test_collect_uploaded_rollups_dedupes_chunk(self) -> None:
        rows = [
            TripRow(
                key="Normal:1:1",
                record_type="Normal",
                chunk_index=1,
                trip_index=1,
                label="t1",
                duration_sec=100.0,
                status="done",
                youtube_url="https://youtu.be/abc123",
            ),
            TripRow(
                key="Normal:1:2",
                record_type="Normal",
                chunk_index=1,
                trip_index=2,
                label="t2",
                duration_sec=200.0,
                status="done",
                youtube_url="https://youtu.be/abc123",
            ),
        ]
        sec, n, vids = collect_uploaded_rollups(rows)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(sec, 300.0)
        self.assertEqual(vids, {"abc123"})

    def test_parse_upload_log_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "publish_all.log").write_text(
                "2026-08-08 06:36:31   Uploaded: https://youtu.be/V-3UB8tLlyk "
                "(2.65 GB, 25m 06s)\n"
                "2026-08-08 02:44:30   Uploaded: https://youtu.be/Q2lHxYksgSs "
                "(1.31 GB, 7m 15s)\n",
                encoding="utf-8",
            )
            stats = parse_upload_log_stats(temp)
            self.assertIn("V-3UB8tLlyk", stats)
            self.assertAlmostEqual(stats["V-3UB8tLlyk"]["elapsed_sec"], 25 * 60 + 6)
            self.assertGreater(stats["V-3UB8tLlyk"]["size_bytes"], 2_000_000_000)

    def test_summarize_matches_log_to_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "publish_all.log").write_text(
                "Uploaded: https://youtu.be/abc123 (100.00 MB, 1m 30s)\n",
                encoding="utf-8",
            )
            rows = [
                TripRow(
                    key="Parking:1:1",
                    record_type="Parking",
                    chunk_index=1,
                    trip_index=1,
                    label="all parking",
                    duration_sec=3600.0,
                    status="done",
                    youtube_url="https://youtu.be/abc123",
                )
            ]
            stats = summarize_youtube_upload_stats(rows, temp)
            assert stats is not None
            self.assertEqual(stats["n_videos"], 1)
            self.assertAlmostEqual(stats["footage_sec"], 3600.0)
            self.assertAlmostEqual(stats["upload_sec"], 90.0)

    def test_format_block_when_idle_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "publish_all.log").write_text(
                "Uploaded: https://youtu.be/x (1.00 GB, 2m 00s)\n",
                encoding="utf-8",
            )
            rows = [
                TripRow(
                    key="Event:1:1",
                    record_type="Event",
                    chunk_index=1,
                    trip_index=1,
                    label="all events",
                    duration_sec=7200.0,
                    status="done",
                    youtube_url="https://youtu.be/x",
                )
            ]
            lines = format_upload_stats_block(
                rows, temp, term_cols=100, idle_complete=True
            )
            text = " ".join(lines)
            self.assertIn("Итог YouTube", text)
            self.assertIn("2h 00m", text)
            self.assertIn("загрузка 2m", text)


if __name__ == "__main__":
    unittest.main()
