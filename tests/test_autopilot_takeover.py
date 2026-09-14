#!/usr/bin/env python3
"""New autopilot launch kills leftovers of the previous run."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import autopilot
import publish_all_70mai as pa

PS_LINES = [
    f"{os.getpid()} /usr/bin/python3 lib/autopilot.py --no-browser",
    "111 /usr/bin/python3 /repo/lib/autopilot.py --no-browser",
    "222 /usr/bin/python3 lib/publish_all_70mai.py --wait --control",
    "333 /usr/bin/python3 lib/autopilot_dashboard.py",
    "444 /usr/bin/python3 lib/autopilot_diagnostics.py --root .",
    "555 /bin/bash ./run autopilot.py",
    "666 tail -f lib/autopilot.py",
]


class AutopilotTakeoverTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(pa, "_ps_ax_lines", return_value=PS_LINES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matches_supervisors_only(self) -> None:
        pids = autopilot._pids_matching(autopilot._AUTOPILOT_CMD_RE)
        self.assertEqual(pids, [111])

    def test_matches_conveyor(self) -> None:
        pids = autopilot._pids_matching(autopilot._PUBLISH_ALL_CMD_RE)
        self.assertEqual(pids, [222])

    def test_takeover_kills_supervisor_before_conveyor(self) -> None:
        killed: list[tuple[str, list[int]]] = []

        def fake_kill(pids: list[int], *, label: str) -> None:
            killed.append((label, list(pids)))

        with (
            mock.patch.object(pa, "_kill_pids", side_effect=fake_kill),
            mock.patch.object(pa, "force_takeover_pipeline") as workers,
            mock.patch.object(pa, "_clear_lock_path") as clear_lock,
        ):
            autopilot.takeover_previous_run()

        self.assertEqual(
            killed,
            [("autopilot.py", [111]), ("publish_all_70mai.py", [222])],
        )
        workers.assert_called_once_with()
        clear_lock.assert_called_once_with()


class PsAxLinesTests(unittest.TestCase):
    def test_ps_ax_lines_tolerates_non_utf8(self) -> None:
        completed = mock.Mock()
        completed.stdout = "111 python lib/autopilot.py\n222 cmd with \ufffd bad bytes\n"
        with mock.patch.object(pa.subprocess, "run", return_value=completed) as run:
            lines = pa._ps_ax_lines()
        run.assert_called_once()
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")
        self.assertEqual(len(lines), 2)
        self.assertIn("bad bytes", lines[1])


if __name__ == "__main__":
    unittest.main()
