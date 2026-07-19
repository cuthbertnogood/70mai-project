#!/usr/bin/env python3
"""Dashboard visibility for background prefetch import."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from autopilot_dashboard import (  # noqa: E402
    PipelineProc,
    PrefetchImportState,
    format_prefetch_stage,
    resolve_prefetch_import,
)


class DashboardPrefetchTests(unittest.TestCase):
    def test_resolve_prefetch_when_publish_and_import_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            log = temp / "publish_all.log"
            log.write_text(
                "2026-07-19 19:00:00   Prefetch import chunk 2 ∥ compose/upload chunk 1\n"
                "2026-07-19 19:00:01 >>> [prefetch background] chunk 2 [Normal]: python import\n"
                "2026-07-19 19:00:02 [copy] Front 1/10: clip.MP4\n",
                encoding="utf-8",
            )
            procs = [
                PipelineProc(1, 120, "import", "import_70mai.py"),
                PipelineProc(2, 60, "publish", "publish_70mai.py"),
            ]
            state = resolve_prefetch_import(temp, procs)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.chunk_index, 2)
            self.assertEqual(state.record_type, "Normal")
            self.assertEqual(state.during_chunk, 1)

    def test_resolve_prefetch_none_for_sync_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            log = temp / "publish_all.log"
            log.write_text(
                "2026-07-19 19:00:00   Import: SD→SSD stage, then concat (window only for Normal)\n",
                encoding="utf-8",
            )
            procs = [PipelineProc(1, 30, "import", "import_70mai.py")]
            self.assertIsNone(resolve_prefetch_import(temp, procs))

    def test_format_prefetch_stage_includes_log_bits(self) -> None:
        pf = PrefetchImportState(
            chunk_index=2, record_type="Normal", during_chunk=1, pid=99
        )
        text = format_prefetch_stage(
            pf,
            {"copy": "Front 3/12 clip.MP4", "merge": "NO_20250701.mp4"},
        )
        self.assertIn("prefetch ch.2", text)
        self.assertIn("publish ch.1", text)
        self.assertIn("Front 3/12", text)


if __name__ == "__main__":
    unittest.main()
