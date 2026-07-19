"""Temp compose path layout (per record_type)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from publish_paths import (
    compose_trip_path,
    is_legacy_compose_path,
    parse_compose_output_path,
    publish_temp_dir,
    resolve_compose_trip_path,
)


class PublishPathsTests(unittest.TestCase):
    def test_typed_paths_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            normal = compose_trip_path(temp_dir, "Normal", 1, 1)
            parking = compose_trip_path(temp_dir, "Parking", 1, 1)
            self.assertNotEqual(normal, parking)
            normal.parent.mkdir(parents=True)
            normal.write_bytes(b"n")
            self.assertTrue(normal.is_file())
            self.assertFalse(parking.is_file())

    def test_resolve_prefers_typed_then_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            legacy = temp_dir / "chunk_01" / "trip_01.mp4"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"x")
            got = resolve_compose_trip_path(temp_dir, "Normal", 1, 1)
            self.assertEqual(got, legacy)
            self.assertTrue(is_legacy_compose_path(temp_dir, legacy))

    def test_parse_output_path(self) -> None:
        typed = Path(
            "video/Output/.publish_tmp/Normal/chunk_02/trip_03.mp4"
        )
        self.assertEqual(parse_compose_output_path(typed), ("Normal", 2, 3))
        legacy = Path("video/Output/.publish_tmp/chunk_01/trip_01.mp4")
        self.assertEqual(parse_compose_output_path(legacy), (None, 1, 1))

    def test_publish_temp_dir(self) -> None:
        typed = Path(
            "video/Output/.publish_tmp/Normal/chunk_02/trip_03.mp4"
        )
        self.assertEqual(
            publish_temp_dir(typed), Path("video/Output/.publish_tmp")
        )


if __name__ == "__main__":
    unittest.main()
