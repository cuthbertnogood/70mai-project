"""Compose/upload dashboard detail lines (speed / ETA)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autopilot_dashboard import (
    format_compose_detail,
    format_upload_detail,
    parse_compose_log_detail,
    parse_upload_log_detail,
)


class ComposeUploadDetailTests(unittest.TestCase):
    def test_format_compose_from_status(self) -> None:
        short, detail = format_compose_detail(
            {
                "record_type": "Parking",
                "chunk_index": 1,
                "trip_index": 2,
                "percent": 42.5,
                "output_bytes": 850 * 1024 * 1024,
                "speed": 1.85,
                "speed_unit": "x",
                "elapsed": "2m",
                "eta": "5m",
            },
            trip_label="trip_02",
        )
        self.assertIn("42%", short)
        self.assertIn("trip_02", short)
        assert detail is not None
        self.assertIn("1.85x", detail)
        self.assertIn("ETA 5m", detail)
        self.assertIn("850 MB", detail)

    def test_format_upload_from_status(self) -> None:
        short, detail = format_upload_detail(
            {
                "record_type": "Parking",
                "percent": 8.0,
                "detail": "100.0 MB/1.2 GB · 2.5 MB/s",
                "speed": 2.5,
                "speed_unit": "MB/s",
                "elapsed": "40s",
                "eta": "7m",
            },
            trip_label="trip_01",
        )
        self.assertIn("8%", short)
        assert detail is not None
        self.assertIn("2.5 MB/s", detail)
        self.assertIn("ETA 7m", detail)

    def test_parse_compose_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "publish_all.log").write_text(
                "2026-07-15 12:00:00 Encode: [####----] 01:00/10:00 (10.0%) "
                "| 45s elapsed | ETA 5m | speed 1.85x\n",
                encoding="utf-8",
            )
            d = parse_compose_log_detail(root)
            assert d is not None
            self.assertAlmostEqual(d["percent"], 10.0)
            self.assertEqual(d["speed"], 1.85)
            self.assertEqual(d["eta"], "5m")

    def test_parse_upload_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "publish_all.log").write_text(
                "2026-07-15 12:00:00   Upload trip_01.mp4: [##------] "
                "100.0 MB/1.2 GB (8%) | 2.5 MB/s | 40s elapsed | ETA 7m\n",
                encoding="utf-8",
            )
            d = parse_upload_log_detail(root)
            assert d is not None
            self.assertEqual(d["file"], "trip_01.mp4")
            self.assertEqual(d["speed"], 2.5)
            self.assertEqual(d["eta"], "7m")


if __name__ == "__main__":
    unittest.main()
