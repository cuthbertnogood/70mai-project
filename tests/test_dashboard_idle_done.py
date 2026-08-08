#!/usr/bin/env python3
"""Idle/done: don't show ghost upload or compose-wait after work finished."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from autopilot_dashboard import (  # noqa: E402
    apply_idle_done_status,
    idle_complete_message,
    is_idle_complete,
    parse_autopilot_finished,
    resolve_live_status,
    _format_pipeline_block,
    _status_is_stale,
)


class DashboardIdleDoneTests(unittest.TestCase):
    def test_parse_finished_nothing_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "publish_all.log").write_text(
                "2026-07-29 17:25:21 All trips/events already uploaded — nothing to do.\n",
                encoding="utf-8",
            )
            got = parse_autopilot_finished(temp)
            self.assertIsNotNone(got)
            assert got is not None
            self.assertEqual(got["detail"], "nothing to do")

    def test_apply_idle_done_clears_ghost_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            old = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
            (temp / "autopilot_status.json").write_text(
                json.dumps(
                    {
                        "ts": old,
                        "record_type": "Parking",
                        "chunk_index": 1,
                        "trip_index": 1,
                        "phase": "upload",
                        "percent": 100.0,
                        "detail": "4.37 GB/4.37 GB",
                    }
                ),
                encoding="utf-8",
            )
            (temp / "publish_all.log").write_text(
                "2026-07-29 17:25:21 All trips/events already uploaded — nothing to do.\n",
                encoding="utf-8",
            )
            with patch(
                "autopilot_dashboard.list_pipeline_processes", return_value=[]
            ):
                st = apply_idle_done_status(temp, json.loads(
                    (temp / "autopilot_status.json").read_text(encoding="utf-8")
                ))
            self.assertEqual((st or {}).get("phase"), "done")
            self.assertFalse(_status_is_stale(st))

    def test_resolve_live_status_promotes_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            old = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
            (temp / "autopilot_status.json").write_text(
                json.dumps(
                    {
                        "ts": old,
                        "record_type": "Parking",
                        "chunk_index": 1,
                        "trip_index": 1,
                        "phase": "upload",
                        "percent": 100.0,
                        "detail": "ghost",
                    }
                ),
                encoding="utf-8",
            )
            (temp / "publish_all.log").write_text(
                "2026-07-29 17:25:21 All trips/events already uploaded — nothing to do.\n",
                encoding="utf-8",
            )
            with patch(
                "autopilot_dashboard.list_pipeline_processes", return_value=[]
            ):
                st = resolve_live_status(temp)
            self.assertEqual((st or {}).get("phase"), "done")

    def test_pipeline_block_no_compose_wait_when_done(self) -> None:
        st = {
            "phase": "done",
            "detail": "все ролики залиты — нечего загружать",
            "percent": 100.0,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        lines = _format_pipeline_block(st, [], stale=False)
        text = "\n".join(lines)
        self.assertIn("✓ готово", text)
        self.assertIn("Всё завершено", text)
        self.assertNotIn("покрытие:", text)
        self.assertNotIn("условие:", text)

    def test_is_idle_complete_when_all_chunks_done(self) -> None:
        from autopilot_dashboard import TripRow

        rows = [
            TripRow(
                key="Normal:1:1",
                record_type="Normal",
                chunk_index=1,
                trip_index=1,
                label="t1",
                duration_sec=60.0,
                status="done",
            )
        ]
        self.assertTrue(is_idle_complete(rows=rows, procs=[]))

    def test_idle_complete_message_russian(self) -> None:
        msg = idle_complete_message(
            st={"detail": "все ролики залиты — нечего загружать"},
        )
        self.assertIn("Всё завершено", msg)
        self.assertIn("активных этапов нет", msg)


if __name__ == "__main__":
    unittest.main()
