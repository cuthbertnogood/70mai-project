"""Compose encode progress → autopilot_status.json path parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from compose_70mai import _update_encode_status


class EncodeStatusTests(unittest.TestCase):
    def test_typed_publish_tmp_path_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "video" / "Output" / ".publish_tmp"
            out = root / "Normal" / "chunk_01" / "trip_02.mp4"
            out.parent.mkdir(parents=True)
            out.write_bytes(b"x" * 2048)
            writes: list[dict] = []

            def capture_write(temp_dir: Path, **kwargs) -> None:
                writes.append({"temp_dir": temp_dir, **kwargs})

            with mock.patch("autopilot_dashboard.read_status", return_value={}), mock.patch(
                "autopilot_dashboard.write_status", side_effect=capture_write
            ):
                _update_encode_status(
                    out,
                    pct=4.5,
                    output_bytes=2048,
                    stalled=False,
                    ffmpeg_pid=99,
                    speed=0.72,
                    elapsed_sec=120.0,
                    eta_sec=3600.0,
                )
            self.assertEqual(len(writes), 1)
            call = writes[0]
            self.assertEqual(call["temp_dir"], root)
            self.assertEqual(call["record_type"], "Normal")
            self.assertEqual(call["chunk_index"], 1)
            self.assertEqual(call["trip_index"], 2)
            self.assertEqual(call["phase"], "compose")
            self.assertAlmostEqual(float(call["percent"]), 4.5)


if __name__ == "__main__":
    unittest.main()
