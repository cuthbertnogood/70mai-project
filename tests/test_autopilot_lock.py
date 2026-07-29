#!/usr/bin/env python3
"""Autopilot lock uses atomic mkdir (no TOCTOU file write)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_all_70mai as pa


class AutopilotLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.lock = self.root / ".publish_all.lock"
        self.addCleanup(self._tmp.cleanup)
        self._patchers = [
            mock.patch.object(pa, "DEFAULT_TEMP_DIR", self.root),
            mock.patch.object(pa, "LOCK_DIR", self.lock),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_acquire_creates_dir_with_pid(self) -> None:
        pa.acquire_lock()
        self.assertTrue(self.lock.is_dir())
        self.assertEqual(
            (self.lock / "pid").read_text(encoding="utf-8").strip(),
            str(os.getpid()),
        )
        pa.release_lock()
        self.assertFalse(self.lock.exists())

    def test_second_acquire_exits_while_holder_alive(self) -> None:
        self.lock.mkdir()
        (self.lock / "pid").write_text("99999", encoding="utf-8")
        with mock.patch.object(pa, "_pid_alive", return_value=True):
            with mock.patch.object(pa, "_ask_console_restart", return_value=False):
                with self.assertRaises(SystemExit):
                    pa.acquire_lock()
        self.assertTrue(self.lock.is_dir())
        self.assertEqual(
            (self.lock / "pid").read_text(encoding="utf-8").strip(), "99999"
        )

    def test_stale_lock_dir_is_replaced(self) -> None:
        self.lock.mkdir()
        (self.lock / "pid").write_text("1", encoding="utf-8")
        with mock.patch.object(pa, "_pid_alive", return_value=False):
            pa.acquire_lock()
        self.assertEqual(
            (self.lock / "pid").read_text(encoding="utf-8").strip(),
            str(os.getpid()),
        )
        pa.release_lock()

    def test_legacy_file_lock_migrates(self) -> None:
        self.lock.write_text("1", encoding="utf-8")
        with mock.patch.object(pa, "_pid_alive", return_value=False):
            pa.acquire_lock()
        self.assertTrue(self.lock.is_dir())
        self.assertEqual(
            (self.lock / "pid").read_text(encoding="utf-8").strip(),
            str(os.getpid()),
        )
        pa.release_lock()


if __name__ == "__main__":
    unittest.main()
