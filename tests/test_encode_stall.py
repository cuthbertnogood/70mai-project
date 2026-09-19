"""Encode stall abort thresholds and EncodeStallError."""

from __future__ import annotations

import unittest

from compose_70mai import (
    ENCODE_STALL_ABORT_SEC,
    ENCODE_STALL_TAIL_ABORT_SEC,
    EncodeStallError,
    encode_stall_abort_sec,
)


class EncodeStallTests(unittest.TestCase):
    def test_tail_abort_faster_near_done(self) -> None:
        self.assertEqual(encode_stall_abort_sec(50.0), ENCODE_STALL_ABORT_SEC)
        self.assertEqual(encode_stall_abort_sec(98.9), ENCODE_STALL_ABORT_SEC)
        self.assertEqual(encode_stall_abort_sec(99.0), ENCODE_STALL_TAIL_ABORT_SEC)
        self.assertEqual(encode_stall_abort_sec(100.0), ENCODE_STALL_TAIL_ABORT_SEC)
        self.assertLess(ENCODE_STALL_TAIL_ABORT_SEC, ENCODE_STALL_ABORT_SEC)

    def test_encode_stall_error_is_called_process_error(self) -> None:
        err = EncodeStallError(9, ["ffmpeg"], stderr="hung")
        self.assertIsInstance(err, EncodeStallError)
        self.assertEqual(err.returncode, 9)


if __name__ == "__main__":
    unittest.main()
