"""Compose/upload dashboard detail lines (speed / ETA)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
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
        self.assertIn("1.85x", short)
        self.assertIn("42%", short)
        self.assertIn("trip_02", short)
        assert detail is not None
        self.assertIn("encode Front↑+Back↓", detail)
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
                "2026-07-15 12:00:00 Encode: [████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░] "
                "46m 07s/2h 01m 19s (38.0%) | 23m 58s elapsed | ETA 38m 46s | speed 1.94x\n"
                "2026-07-15 12:00:02        … encoding (24m 00s, 38%)\n",
                encoding="utf-8",
            )
            d = parse_compose_log_detail(root)
            assert d is not None
            self.assertAlmostEqual(d["percent"], 38.0)
            self.assertEqual(d["speed"], 1.94)
            self.assertEqual(d["eta"], "38m 46s")
            self.assertIn("46m 07s", d["position"])
            self.assertEqual(d["elapsed"], "24m 00s")

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

    def test_compose_prefers_log_speed_over_bare_status(self) -> None:
        short, detail = format_compose_detail(
            {
                "record_type": "Parking",
                "chunk_index": 1,
                "trip_index": 1,
                "percent": 38.0,
                "output_bytes": 1751646256,
                "detail": "38% (1670M)",
            },
            log_detail={
                "percent": 38.0,
                "position": "46m 07s/2h 01m 19s",
                "elapsed": "23m 58s",
                "eta": "38m 46s",
                "speed": 1.94,
                "speed_unit": "x",
                "action": "encode Front↑+Back↓",
                "log_ts": datetime(2026, 7, 15, 12, 0, 2),
            },
            trip_label="trip_01",
        )
        self.assertIn("1.94x", short)
        assert detail is not None
        self.assertIn("1.94x", detail)
        self.assertIn("46m 07s/2h 01m 19s", detail)
        self.assertIn("ETA 38m 46s", detail)

    def test_compose_live_status_beats_stale_log(self) -> None:
        short, detail = format_compose_detail(
            {
                "phase": "compose",
                "ts": "2026-07-19T12:43:26",
                "record_type": "Parking",
                "percent": 4.0,
                "output_bytes": 180 * 1024 * 1024,
                "speed": 1.67,
                "speed_unit": "x",
                "eta": "1h 10m",
                "elapsed": "3m",
            },
            log_detail={
                "percent": 100.0,
                "position": "2h 01m 19s/2h 01m 19s",
                "elapsed": "56m 28s",
                "eta": "0s",
                "speed": 2.15,
                "speed_unit": "x",
                "log_ts": datetime(2026, 7, 16, 0, 51, 50),
            },
            trip_label="все parking",
        )
        self.assertIn("4%", short)
        self.assertNotIn("100%", short)
        assert detail is not None
        self.assertIn("4%", detail)
        self.assertNotIn("100%", detail)

    def test_parse_compose_log_scans_extra_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "publish_all.log").write_text(
                "2026-07-16 00:51:50 Encode: [████] 2h 01m 19s/2h 01m 19s "
                "(100.0%) | 56m 28s elapsed | ETA 0s | speed 2.15x\n",
                encoding="utf-8",
            )
            (root / "parking_rebuild.log").write_text(
                "2026-07-19 12:43:26 Encode: [█░░░] 4m 52s/2h 01m 54s "
                "(4.0%) | 2m 59s elapsed | ETA 1h 10m | speed 1.67x\n",
                encoding="utf-8",
            )
            d = parse_compose_log_detail(root)
            assert d is not None
            self.assertAlmostEqual(d["percent"], 4.0)
            self.assertEqual(d["log_file"], "parking_rebuild.log")


if __name__ == "__main__":
    unittest.main()
