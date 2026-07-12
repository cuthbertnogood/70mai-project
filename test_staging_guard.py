#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from import_70mai import _ffmpeg_concat_copy, _foreign_fs_sources, _same_filesystem


class StagingGuardTests(unittest.TestCase):
    def test_foreign_fs_detects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp4"
            dest.write_bytes(b"x")
            # Same-dir source is same FS.
            src = Path(tmp) / "clip.MP4"
            src.write_bytes(b"y")
            self.assertTrue(_same_filesystem(src, dest))
            self.assertEqual(_foreign_fs_sources([src], dest), [])

    def test_concat_refuses_foreign_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp4"
            # Pretend source is on another device via mocked _same_filesystem.
            src = Path(tmp) / "sd_clip.MP4"
            src.write_bytes(b"y")
            with mock.patch(
                "import_70mai._same_filesystem", return_value=False
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    _ffmpeg_concat_copy("ffmpeg", [src], dest)
            self.assertIn("foreign filesystem", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
