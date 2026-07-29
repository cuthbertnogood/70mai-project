#!/usr/bin/env python3
"""Finished pipeline stages show result text, not bare ✓."""

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
    _format_pipeline_block,
    collect_stage_outcomes,
)


class StageOutcomeTests(unittest.TestCase):
    def test_collect_compose_and_import_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            video = temp / "video"
            (video / "Parking" / "Front").mkdir(parents=True)
            log = temp / "publish_all.log"
            log.write_text(
                "2026-07-29 11:09:15 --- Parking/Front done: 1 merged, 0 skipped\n"
                "2026-07-29 11:25:20 --- Parking/Back done: 1 merged, 0 skipped\n"
                "2026-07-29 11:25:20 [merge] DONE PA_x_F.mp4: 26000 MB in 2h 01m\n",
                encoding="utf-8",
            )
            part = temp / "Parking" / "part_01.mp4"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"x" * (4 * 1024**3 // 100))  # ~40MB stub size display
            rows = [
                TripRow(
                    key="p:1:1",
                    record_type="Parking",
                    chunk_index=1,
                    trip_index=1,
                    label="all parking",
                    duration_sec=7321,
                    status="upload",
                )
            ]
            out = collect_stage_outcomes(
                temp_dir=temp,
                video_dir=video,
                st={"record_type": "Parking", "chunk_index": 1, "phase": "upload"},
                rows=rows,
                copy_done=True,
                merge_done=True,
                compose_done=True,
                upload_done=False,
            )
            self.assertIn("Front", out["copy"])
            self.assertIn("Back", out["copy"])
            self.assertIn("PA_x_F.mp4", out["merge"])
            self.assertIn("part_01.mp4", out["compose"])
            self.assertIn("2h", out["compose"])

    def test_pipeline_done_line_includes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "publish_all.log").write_text(
                "2026-07-29 11:09:15 --- Parking/Front done: 1 merged, 0 skipped\n"
                "2026-07-29 11:25:20 --- Parking/Back done: 1 merged, 0 skipped\n",
                encoding="utf-8",
            )
            part = temp / "Parking" / "part_01.mp4"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"0" * 1024)
            st = {
                "phase": "upload",
                "record_type": "Parking",
                "chunk_index": 1,
                "trip_index": 1,
                "percent": 50.0,
                "detail": "2.0 GB/4.0 GB · 0.9 MB/s",
            }
            rows = [
                TripRow(
                    key="p:1:1",
                    record_type="Parking",
                    chunk_index=1,
                    trip_index=1,
                    label="all parking",
                    duration_sec=7321,
                    status="upload",
                    percent=50.0,
                )
            ]
            lines = _format_pipeline_block(
                st,
                rows,
                temp_dir=temp,
                video_dir=temp / "video",
                compact=True,
            )
            text = "\n".join(lines)
            self.assertIn("copy", text)
            self.assertIn("✓ готово —", text)
            self.assertIn("compose", text)
            self.assertRegex(text, r"compose\s+✓ готово —")


if __name__ == "__main__":
    unittest.main()
