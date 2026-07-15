"""Merge-short strikes + accept_short markers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from import_70mai import (
    MERGE_SHORT_STRIKE_LIMIT,
    bump_merge_short_strikes,
    clear_merge_short_strikes,
    mark_accept_short_merge,
    read_merge_short_strikes,
    user_accepted_short_merge,
)


class MergeShortStrikesTests(unittest.TestCase):
    def test_bump_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            self.assertEqual(read_merge_short_strikes(stage), 0)
            self.assertEqual(bump_merge_short_strikes(stage, detail="a"), 1)
            self.assertEqual(bump_merge_short_strikes(stage, detail="b"), 2)
            self.assertEqual(read_merge_short_strikes(stage), 2)
            self.assertGreaterEqual(MERGE_SHORT_STRIKE_LIMIT, 3)
            clear_merge_short_strikes(stage)
            self.assertEqual(read_merge_short_strikes(stage), 0)

    def test_accept_short_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "PA_test_F.mp4"
            out.write_bytes(b"x")
            self.assertFalse(user_accepted_short_merge(out))
            mark_accept_short_merge(out, "short")
            self.assertTrue(user_accepted_short_merge(out))


if __name__ == "__main__":
    unittest.main()
