#!/usr/bin/env python3
"""Dashboard shows upload when status.json stuck on compose."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from autopilot_dashboard import (  # noqa: E402
    PipelineProc,
    apply_live_upload_from_log,
    parse_upload_log_detail,
    resolve_live_status,
)


class DashboardUploadOverlayTests(unittest.TestCase):
    def test_parse_upload_log_has_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            now = datetime.now()
            (temp / "publish_all.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S}   Upload part_01.mp4: "
                f"[████░░░░] 3.46 GB/4.37 GB (79%) | 0.9 MB/s | "
                f"1h 05m elapsed | ETA 17m 14s\n",
                encoding="utf-8",
            )
            detail = parse_upload_log_detail(temp)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["percent"], 79.0)
            self.assertIsInstance(detail.get("log_ts"), datetime)

    def test_apply_live_upload_overrides_compose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            now = datetime.now()
            (temp / "publish_all.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S}   Upload part_01.mp4: "
                f"[████░░░░] 3.46 GB/4.37 GB (79%) | 0.9 MB/s | "
                f"1h 05m elapsed | ETA 17m 14s\n",
                encoding="utf-8",
            )
            st = {
                "ts": (now - timedelta(minutes=30)).isoformat(timespec="seconds"),
                "record_type": "Parking",
                "chunk_index": 1,
                "trip_index": 1,
                "phase": "compose",
                "percent": 99.9,
                "detail": "100% (0M 1.20x)",
                "speed": 1.2,
                "speed_unit": "x",
            }
            procs = [
                PipelineProc(
                    1,
                    60,
                    "publish",
                    "publish_70mai.py",
                    command="python lib/publish_70mai.py --types Parking --chunk 1",
                )
            ]
            with patch(
                "autopilot_dashboard.list_pipeline_processes",
                return_value=procs,
            ):
                fixed = apply_live_upload_from_log(temp, st)
            self.assertIsNotNone(fixed)
            assert fixed is not None
            self.assertEqual(fixed.get("phase"), "upload")
            self.assertEqual(float(fixed.get("percent") or 0), 79.0)

    def test_resolve_live_status_prefers_upload_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            now = datetime.now()
            (temp / "autopilot_status.json").write_text(
                '{"ts":"%s","record_type":"Parking","chunk_index":1,'
                '"trip_index":1,"phase":"compose","percent":99.9,'
                '"detail":"100%%","speed":1.2,"speed_unit":"x"}'
                % now.isoformat(timespec="seconds"),
                encoding="utf-8",
            )
            (temp / "publish_all.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S}   Upload part_01.mp4: "
                f"[████░░░░] 1.00 GB/4.37 GB (22%) | 0.9 MB/s | "
                f"18m elapsed | ETA 1h 02m\n",
                encoding="utf-8",
            )
            procs = [
                PipelineProc(
                    1,
                    60,
                    "publish",
                    "publish_70mai.py",
                    command="python lib/publish_70mai.py --types Parking --chunk 1",
                )
            ]
            with patch(
                "autopilot_dashboard.list_pipeline_processes",
                return_value=procs,
            ):
                st = resolve_live_status(temp)
            self.assertIsNotNone(st)
            assert st is not None
            self.assertEqual(st.get("phase"), "upload")
            self.assertEqual(float(st.get("percent") or 0), 22.0)


if __name__ == "__main__":
    unittest.main()
