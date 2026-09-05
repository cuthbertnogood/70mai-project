#!/usr/bin/env python3
"""Tests for Autopilot control channel."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import autopilot_control as ac


class AutopilotControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_defer_chunk_persists_without_uploaded(self) -> None:
        ac.defer_chunk(self.temp_dir, record_type="Normal", chunk_index=2)
        self.assertTrue(
            ac.is_chunk_deferred(self.temp_dir, record_type="Normal", chunk_index=2)
        )
        self.assertFalse(
            ac.is_chunk_deferred(self.temp_dir, record_type="Normal", chunk_index=1)
        )

    def test_skip_command_targets_chunk(self) -> None:
        ac.write_command(
            self.temp_dir,
            "skip",
            record_type="Event",
            chunk_index=1,
        )
        ctrl = ac.peek_control(self.temp_dir)
        self.assertTrue(
            ac.control_targets_chunk(
                ctrl,
                record_type="Event",
                chunk_index=1,
            )
        )
        self.assertFalse(
            ac.control_targets_chunk(
                ctrl,
                record_type="Normal",
                chunk_index=1,
            )
        )

    def test_consume_control_clears_file(self) -> None:
        ac.write_command(self.temp_dir, "repair")
        self.assertTrue(ac.control_path(self.temp_dir).is_file())
        data = ac.consume_control(self.temp_dir)
        self.assertEqual(data.get("command"), "repair")
        self.assertFalse(ac.control_path(self.temp_dir).exists())

    def test_stop_exit_constants(self) -> None:
        self.assertEqual(ac.EXIT_USER_STOP, 3)
        self.assertEqual(ac.EXIT_SKIP_CHUNK, 4)

    def test_handle_chunk_control_stop(self) -> None:
        ac.write_command(self.temp_dir, "stop")
        ec, repair = ac.handle_chunk_control(
            self.temp_dir,
            record_type="Normal",
            chunk_index=1,
        )
        self.assertEqual(ec, ac.EXIT_USER_STOP)
        self.assertFalse(repair)

    def test_handle_chunk_skip_defers(self) -> None:
        ac.write_command(
            self.temp_dir,
            "skip",
            record_type="Parking",
            chunk_index=1,
        )
        ec, repair = ac.handle_chunk_control(
            self.temp_dir,
            record_type="Parking",
            chunk_index=1,
        )
        self.assertEqual(ec, ac.EXIT_SKIP_CHUNK)
        self.assertFalse(repair)
        self.assertTrue(
            ac.is_chunk_deferred(
                self.temp_dir,
                record_type="Parking",
                chunk_index=1,
            )
        )


if __name__ == "__main__":
    unittest.main()
