#!/usr/bin/env python3
"""Smoke checks after code changes — imports, CLI --help, dashboard API."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


class SmokeImportsTests(unittest.TestCase):
    def test_core_modules_import(self) -> None:
        import autopilot_dashboard  # noqa: F401
        import card_identity  # noqa: F401
        import import_70mai  # noqa: F401
        import import_state  # noqa: F401
        import plan_estimate  # noqa: F401
        import publish_all_70mai  # noqa: F401
        import publish_state  # noqa: F401

    def test_dashboard_public_api(self) -> None:
        from autopilot_dashboard import Dashboard, TripRow

        dash = Dashboard()
        for name in (
            "start",
            "stop",
            "render",
            "from_plan",
            "reload_plan_if_changed",
        ):
            self.assertTrue(
                hasattr(dash, name),
                f"Dashboard missing .{name}()",
            )
        _ = TripRow(
            key="Normal:1:1",
            record_type="Normal",
            chunk_index=1,
            trip_index=1,
            label="trip 1",
            duration_sec=60.0,
        )

    def test_card_reset_helpers(self) -> None:
        from card_identity import format_sd_clip_summary, refresh_card_identity
        from publish_state import clear_host_card_cache, reset_portable_sd_state

        self.assertTrue(callable(format_sd_clip_summary))
        self.assertTrue(callable(refresh_card_identity))
        self.assertTrue(callable(reset_portable_sd_state))
        self.assertTrue(callable(clear_host_card_cache))


class SmokeCliTests(unittest.TestCase):
    _PY = ROOT / ".venv" / "bin" / "python"
    _ENV = {"PYTHONPATH": str(LIB)}

    def _run_help(self, script: str) -> subprocess.CompletedProcess[str]:
        py = self._PY if self._PY.is_file() else Path(sys.executable)
        return subprocess.run(
            [str(py), str(LIB / script), "--help"],
            cwd=ROOT,
            env={**dict(__import__("os").environ), **self._ENV},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_publish_all_help(self) -> None:
        r = self._run_help("publish_all_70mai.py")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_autopilot_dashboard_help(self) -> None:
        r = self._run_help("autopilot_dashboard.py")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_import_70mai_help(self) -> None:
        r = self._run_help("import_70mai.py")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)


if __name__ == "__main__":
    unittest.main()
