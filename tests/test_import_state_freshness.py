#!/usr/bin/env python3
"""ImportStateStore load/save edge cases."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from import_state import ImportStateStore, local_import_state_path, sd_import_state_path


class ImportStateFreshnessTests(unittest.TestCase):
    def test_load_prefers_newer_local_over_stale_sd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sd"
            local_dir = root / "local"
            source.mkdir()
            local_dir.mkdir()
            sd_path = sd_import_state_path(source, "Normal")
            sd_path.parent.mkdir(parents=True)
            local_path = local_import_state_path(local_dir, "Normal")
            sd_path.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "files": {"old": {"status": "merged"}},
                    }
                ),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-07-29T12:00:00+00:00",
                        "files": {"new": {"status": "merged"}},
                    }
                ),
                encoding="utf-8",
            )
            store = ImportStateStore(
                source,
                "Normal",
                state_on_sd=True,
                local_dir=local_dir,
                chunk_minutes=10,
                gap_seconds=90,
            )
            self.assertIn("new", store._data.get("files", {}))
            self.assertNotIn("old", store._data.get("files", {}))

    def test_skipped_merge_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sd"
            local_dir = root / "local"
            source.mkdir()
            local_dir.mkdir()
            store = ImportStateStore(
                source,
                "Normal",
                state_on_sd=True,
                local_dir=local_dir,
                chunk_minutes=10,
                gap_seconds=90,
            )
            store.record_merge(
                record_type="Normal",
                camera="Front",
                filename="NO_20260425-130119_131019_F.mp4",
                status="skipped",
                clip_count=10,
                last_clip="NO20260425-131019-000010F.MP4",
            )
            reloaded = ImportStateStore(
                source,
                "Normal",
                state_on_sd=True,
                local_dir=local_dir,
                chunk_minutes=10,
                gap_seconds=90,
            )
            entry = reloaded.get_merge_entry(
                record_type="Normal",
                camera="Front",
                filename="NO_20260425-130119_131019_F.mp4",
            )
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["status"], "skipped")
            self.assertEqual(entry["clip_count"], 10)


if __name__ == "__main__":
    unittest.main()
