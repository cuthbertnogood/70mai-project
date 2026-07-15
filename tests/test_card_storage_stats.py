#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from card_storage_stats import (
    collect_card_storage_stats,
    render_card_storage_text,
    write_card_storage_stats,
)


class CardStorageStatsTests(unittest.TestCase):
    def test_collect_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Normal" / "Front").mkdir(parents=True)
            (root / "Normal" / "Back").mkdir(parents=True)
            (root / "Normal" / "Front" / "a.MP4").write_bytes(b"x" * 1_000_000)
            (root / "GPSData000001.txt").write_text("gps")
            (root / ".70mai" / "import").mkdir(parents=True)
            (root / ".70mai" / "import" / "x.json").write_text("{}")

            data = collect_card_storage_stats(root)
            self.assertEqual(data["video"]["Normal"]["total_files"], 1)
            self.assertGreaterEqual(data["video"]["Normal"]["total_bytes"], 1_000_000)
            self.assertTrue(any(i["kind"] == "gps" for i in data["non_video"]))

            text = render_card_storage_text(data)
            self.assertIn("Normal", text)
            self.assertIn("GPSData000001.txt", text)

            path = write_card_storage_stats(root)
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.is_file())
            self.assertTrue((root / ".70mai" / "import" / "card_storage.json").is_file())


if __name__ == "__main__":
    unittest.main()
