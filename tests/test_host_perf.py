#!/usr/bin/env python3
"""Tests for the dependency-free host performance trace."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from host_perf import append_trace, host_snapshot


class HostPerfTests(unittest.TestCase):
    def test_append_trace_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "perf_trace.jsonl"
            append_trace(path, {"event": "stage_end", "elapsed_sec": 1.2})
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["event"], "stage_end")
            self.assertIn("ts", row)

    def test_snapshot_has_process_and_disk_metrics(self) -> None:
        row = host_snapshot(1, Path("."))
        self.assertIn("process_cpu_pct", row)
        self.assertIn("process_rss_mb", row)
        self.assertIn("host_cpu_count", row)


if __name__ == "__main__":
    unittest.main()
