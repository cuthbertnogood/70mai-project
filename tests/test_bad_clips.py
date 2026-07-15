"""Corrupt clip quarantine + history log."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from import_70mai import (
    Clip,
    append_bad_clip_record,
    bad_clips_log_path,
    drop_unreadable_clips,
    quarantine_corrupt_clip,
)


class BadClipsTests(unittest.TestCase):
    def test_quarantine_renames_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "PA20250903-095947-043342F.MP4"
            src.write_bytes(b"not-a-real-mp4")
            hist = root / "hist"
            dest = quarantine_corrupt_clip(
                src,
                reason="moov missing",
                record_type="Parking",
                camera="Front",
                history_dir=hist,
            )
            self.assertIsNotNone(dest)
            assert dest is not None
            self.assertTrue(dest.name.endswith(".bad"))
            self.assertFalse(src.exists())
            self.assertTrue(dest.is_file())
            log_path = bad_clips_log_path(hist)
            self.assertTrue(log_path.is_file())
            entry = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(entry["action"], "quarantined")
            self.assertEqual(entry["name"], "PA20250903-095947-043342F.MP4")
            self.assertEqual(entry["record_type"], "Parking")

    def test_drop_unreadable_keeps_good(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hist = root / "hist"
            good = root / "good.MP4"
            bad = root / "bad.MP4"
            good.write_bytes(b"x" * 20_000)
            bad.write_bytes(b"x" * 20_000)
            clips = [
                Clip(
                    path=good,
                    record_type="Parking",
                    camera="Front",
                    timestamp=datetime(2025, 1, 1, 12, 0, 0),
                    sequence=1,
                    duration=30.0,
                ),
                Clip(
                    path=bad,
                    record_type="Parking",
                    camera="Front",
                    timestamp=datetime(2025, 1, 1, 12, 0, 30),
                    sequence=2,
                    duration=30.0,
                ),
            ]

            def fake_reason(path: Path, _ffprobe: str) -> str | None:
                return "ffprobe unreadable" if path == bad else None

            with patch("import_70mai.clip_unreadable_reason", side_effect=fake_reason):
                kept = drop_unreadable_clips(
                    clips,
                    "ffprobe",
                    record_type="Parking",
                    camera="Front",
                    history_dir=hist,
                )
            self.assertEqual([c.path for c in kept], [good])
            self.assertFalse(bad.exists())
            self.assertTrue((root / "bad.MP4.bad").is_file())

    def test_append_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp)
            append_bad_clip_record(
                path=Path("/Volumes/Untitled/Parking/Front/x.MP4"),
                reason="moov",
                action="skip_only",
                record_type="Parking",
                camera="Front",
                history_dir=hist,
            )
            lines = bad_clips_log_path(hist).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("moov", lines[0])


if __name__ == "__main__":
    unittest.main()
