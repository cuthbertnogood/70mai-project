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
    count_bad_files_on_sd,
    drop_unreadable_clips,
    mp4_has_moov_atom,
    quarantine_corrupt_clip,
    sd_bad_clips_log_path,
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

    def test_mp4_has_moov_walks_atoms(self) -> None:
        """Synthetic MP4: good has moov; bogus mdat size has none."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def atom(typ: bytes, payload: bytes) -> bytes:
                return (8 + len(payload)).to_bytes(4, "big") + typ + payload

            ftyp = atom(b"ftyp", b"isom")
            moov = atom(b"moov", b"\x00" * 100)
            mdat = atom(b"mdat", b"\x00" * 1000)
            good = root / "good.MP4"
            good.write_bytes(ftyp + mdat + moov)
            self.assertTrue(mp4_has_moov_atom(good))

            # mdat size claims past EOF → walker rejects (no reachable moov).
            bad_hdr = (8 + 5_000_000).to_bytes(4, "big") + b"mdat" + b"\x00" * 200
            # Plant a fake "moov" string inside the truncated mdat payload.
            bad_body = bad_hdr + b"xxxxmoovxxxx"
            bad = root / "bad.MP4"
            bad.write_bytes(ftyp + bad_body)
            self.assertFalse(mp4_has_moov_atom(bad))

    def test_append_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Parking" / "Front"
            front.mkdir(parents=True)
            clip = front / "x.MP4"
            hist = root / "hist"
            append_bad_clip_record(
                path=clip,
                reason="moov",
                action="skip_only",
                record_type="Parking",
                camera="Front",
                history_dir=hist,
            )
            lines = bad_clips_log_path(hist).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("moov", lines[0])

    def test_append_mirrors_to_sd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Parking" / "Front"
            front.mkdir(parents=True)
            clip = front / "PA20250101-120000-000001F.MP4"
            clip.write_bytes(b"x")
            hist = root / "hist"
            append_bad_clip_record(
                path=clip,
                reason="moov missing",
                action="quarantined",
                record_type="Parking",
                camera="Front",
                history_dir=hist,
            )
            host = bad_clips_log_path(hist)
            sd = sd_bad_clips_log_path(root)
            self.assertTrue(host.is_file())
            self.assertTrue(sd.is_file())
            self.assertEqual(
                host.read_text(encoding="utf-8"),
                sd.read_text(encoding="utf-8"),
            )

    def test_count_bad_files_on_sd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Parking" / "Front"
            front.mkdir(parents=True)
            (front / "good.MP4").write_bytes(b"ok")
            (front / "bad.MP4.bad").write_bytes(b"no")
            (front / "other.MP4.20260101.bad").write_bytes(b"no")
            self.assertEqual(count_bad_files_on_sd(root), 2)
            self.assertEqual(count_bad_files_on_sd(None), 0)


if __name__ == "__main__":
    unittest.main()
