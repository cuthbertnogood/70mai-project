"""Auto-fix stale autopilot_status.json from live ffmpeg/publish CLI."""

from __future__ import annotations

import unittest

from autopilot_dashboard import (
    PipelineProc,
    _parse_ffmpeg_compose,
    _parse_publish_cli,
    reconcile_status_with_processes,
)


class StatusReconcileTests(unittest.TestCase):
    def test_parse_publish_cli(self) -> None:
        cmd = (
            "python lib/publish_70mai.py --source /Volumes/Untitled "
            "--types Normal --chunk 1 --video-dir video/Output"
        )
        self.assertEqual(_parse_publish_cli(cmd), ("Normal", 1))

    def test_parse_ffmpeg_normal(self) -> None:
        cmd = (
            "ffmpeg -i video/Output/Normal/Front/NO_x_F.mp4 "
            "video/Output/.publish_tmp/chunk_01/trip_01.mp4"
        )
        parsed = _parse_ffmpeg_compose(cmd)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["record_type"], "Normal")
        self.assertEqual(parsed["chunk_index"], 1)

    def test_reconcile_fixes_wrong_record_type(self) -> None:
        st = {
            "record_type": "Parking",
            "chunk_index": 1,
            "trip_index": 1,
            "phase": "compose",
            "percent": 12.0,
        }

        def fake_procs() -> list[PipelineProc]:
            return [
                PipelineProc(
                    pid=1,
                    etime_sec=100,
                    role="ffmpeg",
                    tip="ffmpeg encode",
                    command=(
                        "ffmpeg -i video/Output/Normal/Front/x_F.mp4 "
                        "video/Output/.publish_tmp/chunk_01/trip_01.mp4"
                    ),
                )
            ]

        import autopilot_dashboard as dash

        orig = dash.list_pipeline_processes
        dash.list_pipeline_processes = fake_procs  # type: ignore[method-assign]
        try:
            fixed = reconcile_status_with_processes(Path("/tmp"), st)
        finally:
            dash.list_pipeline_processes = orig  # type: ignore[method-assign]
        self.assertIsNotNone(fixed)
        assert fixed is not None
        self.assertEqual(fixed["record_type"], "Normal")
        self.assertEqual(fixed["percent"], 12.0)


from pathlib import Path

if __name__ == "__main__":
    unittest.main()
