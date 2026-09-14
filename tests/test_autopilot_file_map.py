#!/usr/bin/env python3
"""Tests for per-file block map on Autopilot Dashboard."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autopilot_file_map import build_file_map_payload, clear_file_map_cache
from clip_timeline import MANIFEST_VERSION
from import_70mai import bad_clips_log_path
from import_state import sd_import_dir


class AutopilotFileMapTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_file_map_cache()

    def test_empty_when_no_card(self) -> None:
        payload = build_file_map_payload(None, ["Normal"], ttl_sec=0)
        self.assertFalse(payload["present"])
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["total"], 0)

    def test_groups_front_and_back_sorted_by_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Normal" / "Front"
            back = root / "Normal" / "Back"
            front.mkdir(parents=True)
            back.mkdir(parents=True)
            late = front / "NO20260814-100100-000002F.MP4"
            early = front / "NO20260814-100000-000001F.MP4"
            back_clip = back / "NO20260814-100000-000001B.MP4"
            early.write_bytes(b"x" * 10)
            late.write_bytes(b"x" * 20)
            back_clip.write_bytes(b"x" * 30)

            payload = build_file_map_payload(root, ["Normal"], ttl_sec=0)
            self.assertTrue(payload["present"])
            self.assertEqual(payload["total"], 3)
            self.assertEqual(len(payload["groups"]), 2)
            self.assertEqual(payload["groups"][0]["camera"], "Front")
            self.assertEqual(payload["groups"][1]["camera"], "Back")
            front_names = [b["n"] for b in payload["groups"][0]["blocks"]]
            self.assertEqual(front_names, [early.name, late.name])
            self.assertEqual(payload["groups"][0]["blocks"][0]["st"], "oncard")

    def test_uploaded_beats_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = Path(tmp) / "video"
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            clip_name = "NO20260814-100000-000001F.MP4"
            (front / clip_name).write_bytes(b"x" * 10)

            merge = video / "NO_20260814-100000_100100_F.mp4"
            merge.parent.mkdir(parents=True)
            merge.write_bytes(b"x")
            manifest = {
                "version": MANIFEST_VERSION,
                "record_type": "Normal",
                "camera": "Front",
                "merge": merge.name,
                "clips": [{"key": "k", "wall": "2026-08-14T10:00:00", "dur": 60.0,
                           "offset": 0.0, "src": clip_name}],
            }
            merge.with_name(merge.name + ".timeline.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            inv_dir = sd_import_dir(root)
            inv_dir.mkdir(parents=True)
            inv = {
                "record_types": {
                    "Normal": {
                        "clip_youtube": {
                            "Front": {
                                clip_name: {
                                    "youtube_url": "https://youtu.be/abc",
                                }
                            }
                        }
                    }
                }
            }
            (inv_dir / "card_inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )

            payload = build_file_map_payload(
                root, ["Normal"], video_dir=video, ttl_sec=0
            )
            block = payload["groups"][0]["blocks"][0]
            self.assertEqual(block["st"], "uploaded")
            self.assertEqual(payload["counts"]["uploaded"], 1)

    def test_bad_clip_beats_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp_dir = Path(tmp) / "host"
            temp_dir.mkdir()
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            clip_name = "NO20260814-100000-000001F.MP4"
            (front / clip_name).write_bytes(b"x" * 10)

            inv_dir = sd_import_dir(root)
            inv_dir.mkdir(parents=True)
            inv = {
                "record_types": {
                    "Normal": {
                        "clip_youtube": {
                            "Front": {
                                clip_name: {
                                    "youtube_url": "https://youtu.be/abc",
                                }
                            }
                        }
                    }
                }
            }
            (inv_dir / "card_inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )
            bad_log = bad_clips_log_path(temp_dir)
            bad_log.write_text(
                json.dumps({"name": clip_name, "reason": "corrupt"}) + "\n",
                encoding="utf-8",
            )

            payload = build_file_map_payload(
                root, ["Normal"], temp_dir=temp_dir, ttl_sec=0
            )
            self.assertEqual(payload["groups"][0]["blocks"][0]["st"], "error")
            self.assertEqual(payload["counts"]["error"], 1)

    def test_uploaded_from_inventory_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            clip_name = "NO20260814-100000-000001F.MP4"
            (front / clip_name).write_bytes(b"x" * 10)
            inv_dir = sd_import_dir(root)
            inv_dir.mkdir(parents=True)
            inv = {
                "record_types": {
                    "Normal": {
                        "trips": [
                            {
                                "index": 1,
                                "start": "2026-08-14 10:00:00",
                                "end": "2026-08-14 10:01:00",
                                "youtube_url": "https://youtu.be/abc",
                            }
                        ],
                        "clip_youtube": {},
                    }
                }
            }
            (inv_dir / "card_inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )
            payload = build_file_map_payload(root, ["Normal"], ttl_sec=0)
            self.assertEqual(payload["groups"][0]["blocks"][0]["st"], "uploaded")
            self.assertEqual(payload["counts"]["uploaded"], 1)

    def test_uploaded_from_video_id_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            clip_name = "NO20260814-100000-000001F.MP4"
            (front / clip_name).write_bytes(b"x" * 10)
            inv_dir = sd_import_dir(root)
            inv_dir.mkdir(parents=True)
            inv = {
                "record_types": {
                    "Normal": {
                        "clip_youtube": {
                            "Front": {
                                clip_name: {"video_id": "abc123XYZ"},
                            }
                        }
                    }
                }
            }
            (inv_dir / "card_inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )
            payload = build_file_map_payload(root, ["Normal"], ttl_sec=0)
            self.assertEqual(payload["groups"][0]["blocks"][0]["st"], "uploaded")

    def test_counts_match_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            for name in (
                "NO20260814-100000-000001F.MP4",
                "NO20260814-100100-000002F.MP4",
            ):
                (front / name).write_bytes(b"x")

            payload = build_file_map_payload(root, ["Normal"], ttl_sec=0)
            counted = sum(payload["counts"].values())
            block_total = sum(len(g["blocks"]) for g in payload["groups"])
            self.assertEqual(payload["total"], counted)
            self.assertEqual(payload["total"], block_total)


if __name__ == "__main__":
    unittest.main()
