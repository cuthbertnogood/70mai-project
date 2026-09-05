#!/usr/bin/env python3
"""chunk_state_matches / prune_stale_parts_for_plan — wall_start binding."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from plan_estimate import ChunkPlan, Trip
from publish_70mai import (
    chunk_state_matches,
    chunk_uploaded,
    prune_stale_parts_for_plan,
)
from publish_all_70mai import chunk_is_done
from publish_state import StateStore, empty_publish_state, save_state_file, sd_state_path


def _trip(index: int, start: str, end: str, *, duration: float = 600.0) -> Trip:
    return Trip(
        record_type="Normal",
        index=index,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        clip_count=1,
        duration_sec=duration,
    )


def _chunk(
    index: int,
    trips: list[Trip],
    *,
    record_type: str = "Normal",
) -> ChunkPlan:
    typed = tuple(
        Trip(
            record_type=record_type,
            index=t.index,
            start=t.start,
            end=t.end,
            clip_count=t.clip_count,
            duration_sec=t.duration_sec,
        )
        for t in trips
    )
    return ChunkPlan(record_type=record_type, index=index, trips=typed)


class ChunkStateMatchTests(unittest.TestCase):
    def test_mismatch_wall_start_not_uploaded(self) -> None:
        chunk = _chunk(
            1,
            [_trip(1, "2026-07-29T09:34:31", "2026-07-29T09:55:41")],
        )
        part = {
            "record_type": "Normal",
            "index": 1,
            "wall_start": "2026-07-06T10:36:50",
            "trip_indices": [1],
            "uploaded": True,
            "video_id": "oldVid",
        }
        self.assertFalse(chunk_state_matches(chunk, part))
        state = {"parts": [part], "trip_parts": []}
        self.assertFalse(chunk_uploaded(state, "Normal", 1, chunk=chunk))
        self.assertFalse(chunk_is_done(state, chunk))

    def test_matching_wall_start_is_uploaded(self) -> None:
        chunk = _chunk(
            1,
            [
                _trip(1, "2026-07-29T09:34:31", "2026-07-29T09:55:41"),
                _trip(2, "2026-07-29T18:32:54", "2026-07-29T19:09:00"),
            ],
        )
        part = {
            "record_type": "Normal",
            "index": 1,
            "wall_start": "2026-07-29T09:34:31",
            "trip_indices": [1, 2],
            "duration_sec": 1200.0,
            "uploaded": True,
            "video_id": "okVid",
        }
        self.assertTrue(chunk_state_matches(chunk, part))
        state = {"parts": [part], "trip_parts": []}
        self.assertTrue(chunk_uploaded(state, "Normal", 1, chunk=chunk))
        self.assertTrue(chunk_is_done(state, chunk))

    def test_legacy_without_wall_start_not_uploaded(self) -> None:
        chunk = _chunk(1, [_trip(1, "2026-07-29T09:34:31", "2026-07-29T09:55:41")])
        part = {
            "record_type": "Normal",
            "index": 1,
            "uploaded": True,
            "video_id": "legacy",
        }
        self.assertFalse(chunk_state_matches(chunk, part))
        state = {"parts": [part], "trip_parts": []}
        self.assertFalse(chunk_uploaded(state, "Normal", 1, chunk=chunk))

    def test_orphan_trip_part_without_wall_start_not_done(self) -> None:
        """Stale trip_parts alone must not mark a new-period chunk uploaded."""
        from publish_70mai import trip_uploaded

        chunk = _chunk(
            3,
            [_trip(8, "2026-08-15T12:04:52", "2026-08-15T14:15:17")],
        )
        state = {
            "parts": [],
            "trip_parts": [
                {
                    "record_type": "Normal",
                    "chunk_index": 3,
                    "trip_index": 1,
                    "uploaded": True,
                    "video_id": "hQwMQYuFTck",
                    "youtube_url": "https://youtu.be/hQwMQYuFTck",
                }
            ],
        }
        self.assertFalse(trip_uploaded(state, "Normal", 3, 1, chunk=chunk))
        self.assertFalse(chunk_is_done(state, chunk))
        reasons = prune_stale_parts_for_plan(state, [chunk])
        self.assertTrue(reasons)
        self.assertEqual(state["trip_parts"], [])

    def test_trip_part_matching_wall_start_is_uploaded(self) -> None:
        from publish_70mai import mark_trip_state, trip_uploaded

        chunk = _chunk(
            3,
            [_trip(8, "2026-08-15T12:04:52", "2026-08-15T14:15:17")],
        )
        state: dict = {"parts": [], "trip_parts": []}
        mark_trip_state(
            state,
            record_type="Normal",
            chunk_index=3,
            trip_index=1,
            video_id="okVid",
            uploaded=True,
            output_path=None,
            wall_start=chunk.trips[0].start,
        )
        self.assertTrue(trip_uploaded(state, "Normal", 3, 1, chunk=chunk))
        self.assertTrue(chunk_is_done(state, chunk))
        self.assertEqual(prune_stale_parts_for_plan(state, [chunk]), [])

    def test_prune_stale_parts_drops_mismatch_and_saves(self) -> None:
        chunk = _chunk(
            1,
            [_trip(1, "2026-07-29T09:34:31", "2026-07-29T09:55:41")],
        )
        state = {
            "parts": [
                {
                    "record_type": "Normal",
                    "index": 1,
                    "wall_start": "2026-07-06T10:36:50",
                    "trip_indices": [1, 2, 3, 4, 5],
                    "uploaded": True,
                    "video_id": "stale",
                },
                {
                    "record_type": "Normal",
                    "index": 9,
                    "wall_start": "2026-01-01T00:00:00",
                    "trip_indices": [9],
                    "uploaded": True,
                    "video_id": "other",
                },
            ],
            "trip_parts": [
                {
                    "record_type": "Normal",
                    "chunk_index": 1,
                    "trip_index": 1,
                    "uploaded": True,
                    "video_id": "trip_stale",
                }
            ],
        }
        reasons = prune_stale_parts_for_plan(state, [chunk])
        self.assertGreaterEqual(len(reasons), 1)
        self.assertTrue(any("chunk 1" in r for r in reasons))
        self.assertEqual(len(state["parts"]), 1)
        self.assertEqual(state["parts"][0]["index"], 9)
        self.assertEqual(state["trip_parts"], [])
        self.assertFalse(chunk_is_done(state, chunk))

    def test_event_duration_mismatch(self) -> None:
        trip = Trip(
            record_type="Event",
            index=1,
            start=datetime.fromisoformat("2026-08-01T10:00:00"),
            end=datetime.fromisoformat("2026-08-01T12:00:00"),
            clip_count=10,
            duration_sec=7200.0,
        )
        chunk = ChunkPlan(record_type="Event", index=1, trips=(trip,))
        part = {
            "record_type": "Event",
            "index": 1,
            "wall_start": "2026-08-01T10:00:00",
            "trip_indices": [1],
            "duration_sec": 1000.0,
            "uploaded": True,
        }
        self.assertFalse(chunk_state_matches(chunk, part))

    def test_prune_persists_via_state_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "card"
            source.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            from publish_state import get_or_create_card_id

            card_id = get_or_create_card_id(source)
            chunk = _chunk(
                1,
                [_trip(1, "2026-07-29T09:34:31", "2026-07-29T09:55:41")],
            )
            sd_path = sd_state_path(source, "Normal")
            save_state_file(
                sd_path,
                {
                    **empty_publish_state(source, "Normal", card_id=card_id),
                    "parts": [
                        {
                            "record_type": "Normal",
                            "index": 1,
                            "wall_start": "2026-07-06T10:36:50",
                            "trip_indices": [1],
                            "uploaded": True,
                            "video_id": "stale",
                        }
                    ],
                },
            )
            store = StateStore(source, temp, "Normal", state_on_sd=True)
            state = store.load(resume=True, quiet=True)
            self.assertEqual(len(state.get("parts", [])), 1)
            reasons = prune_stale_parts_for_plan(state, [chunk])
            self.assertTrue(reasons)
            store.save(state)
            reloaded = store.load(resume=True, quiet=True)
            self.assertEqual(reloaded.get("parts"), [])
            self.assertFalse(chunk_is_done(reloaded, chunk))


if __name__ == "__main__":
    unittest.main()
