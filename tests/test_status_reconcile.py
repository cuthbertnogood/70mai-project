"""Auto-fix stale autopilot_status.json from live ffmpeg/publish CLI."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path

from autopilot_dashboard import (
    PipelineProc,
    _parse_ffmpeg_compose,
    _parse_publish_cli,
    _status_is_stale,
    reconcile_status_with_processes,
    refresh_stale_compose_status,
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

    def test_parse_ffmpeg_typed_path(self) -> None:
        cmd = (
            "ffmpeg -i video/Output/Normal/Front/NO_x_F.mp4 "
            "video/Output/.publish_tmp/Normal/chunk_01/trip_02.mp4"
        )
        parsed = _parse_ffmpeg_compose(cmd)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["record_type"], "Normal")
        self.assertEqual(parsed["chunk_index"], 1)
        self.assertEqual(parsed["trip_index"], 2)

    def test_refresh_stale_compose_from_live_proc(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            stale_ts = (datetime.now() - timedelta(minutes=10)).isoformat(
                timespec="seconds"
            )
            st = {
                "ts": stale_ts,
                "record_type": "Normal",
                "chunk_index": 1,
                "trip_index": 1,
                "phase": "compose",
                "percent": 0.0,
            }
            out = temp_dir / "Normal" / "chunk_01" / "trip_01.mp4"
            out.parent.mkdir(parents=True)
            out.write_bytes(b"x" * 4096)
            log_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            (temp_dir / "publish_all.log").write_text(
                f"{log_ts}       … encoding (5m 0s, 12%, 0.70x)\n",
                encoding="utf-8",
            )

            def fake_procs() -> list[PipelineProc]:
                return [
                    PipelineProc(
                        pid=1,
                        etime_sec=100,
                        role="ffmpeg",
                        tip="ffmpeg encode",
                        command=(
                            "ffmpeg -i video/Output/Normal/Front/x_F.mp4 "
                            f"{out}"
                        ),
                    )
                ]

            import autopilot_dashboard as dash

            orig = dash.list_pipeline_processes
            dash.list_pipeline_processes = fake_procs  # type: ignore[method-assign]
            try:
                fixed = refresh_stale_compose_status(temp_dir, st)
            finally:
                dash.list_pipeline_processes = orig  # type: ignore[method-assign]
            self.assertIsNotNone(fixed)
            assert fixed is not None
            self.assertFalse(_status_is_stale(fixed))
            self.assertAlmostEqual(float(fixed["percent"]), 12.0)

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


if __name__ == "__main__":
    unittest.main()
