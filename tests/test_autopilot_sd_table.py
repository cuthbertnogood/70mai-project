#!/usr/bin/env python3
"""Tests for SD trip table on Autopilot Dashboard."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autopilot_sd_table import build_sd_card_payload
from import_state import sd_import_dir


class AutopilotSdTableTests(unittest.TestCase):
    def test_scan_builds_trip_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            p1 = front / "NO20260814-100000-000001F.MP4"
            p2 = front / "NO20260814-100100-000002F.MP4"
            p1.write_bytes(b"x" * 1_000_000)
            p2.write_bytes(b"x" * 2_000_000)
            payload = build_sd_card_payload(root, ["Normal"], ttl_sec=0)
            self.assertTrue(payload["present"])
            self.assertEqual(len(payload["trips"]), 1)
            trip = payload["trips"][0]
            self.assertEqual(trip["record_type"], "Normal")
            self.assertEqual(trip["clip_count"], 2)
            self.assertEqual(trip["size_bytes"], 3_000_000)

    def test_uses_inventory_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Event" / "Front"
            front.mkdir(parents=True)
            clip = front / "EV20260718-103701-068294F.MP4"
            clip.write_bytes(b"x" * 500_000)
            inv_dir = sd_import_dir(root)
            inv_dir.mkdir(parents=True)
            inv = {
                "updated_at": "2026-07-21T00:00:00Z",
                "record_types": {
                    "Event": {
                        "trips": [
                            {
                                "index": 1,
                                "start": "2026-07-18 10:37:01",
                                "end": "2026-07-18 11:17:33",
                                "duration_sec": 2400.0,
                                "duration": "40m 00s",
                                "clip_count": 1,
                            }
                        ]
                    }
                },
            }
            (inv_dir / "card_inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )
            payload = build_sd_card_payload(root, ["Event"], ttl_sec=0)
            self.assertEqual(len(payload["trips"]), 1)
            self.assertEqual(payload["trips"][0]["duration_sec"], 2400.0)
            self.assertEqual(payload["trips"][0]["size_bytes"], 500_000)

    def test_event_counts_all_clips_not_timeline_window(self) -> None:
        """Event/Parking clips span the card; inventory trip end is timeline length only."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Event" / "Front"
            front.mkdir(parents=True)
            for name in (
                "EV20260308-213106-000001F.MP4",
                "EV20260815-232924-000002F.MP4",
            ):
                (front / name).write_bytes(b"x" * 100)
            inv_dir = sd_import_dir(root)
            inv_dir.mkdir(parents=True)
            inv = {
                "updated_at": "2026-08-15T00:00:00Z",
                "record_types": {
                    "Event": {
                        "trips": [
                            {
                                "index": 1,
                                "start": "2026-03-08 21:31:06",
                                "end": "2026-03-08 23:00:37",
                                "duration_sec": 5371.0,
                                "duration": "1h 29m 31s",
                                "clip_count": 2,
                            }
                        ]
                    }
                },
            }
            (inv_dir / "card_inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )
            payload = build_sd_card_payload(root, ["Event"], ttl_sec=0)
            self.assertEqual(payload["trips"][0]["clip_count"], 2)


if __name__ == "__main__":
    unittest.main()
