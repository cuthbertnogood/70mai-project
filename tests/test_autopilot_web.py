#!/usr/bin/env python3
"""Tests for Autopilot web Dashboard HTTP server."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from autopilot_web import (
    AutopilotWebServer,
    build_pipeline_payload,
    build_status_payload,
)


class AutopilotWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self._tmp.name)
        self.video_dir = self.temp_dir / "video"
        self.video_dir.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def test_build_status_payload_minimal(self) -> None:
        payload = build_status_payload(
            temp_dir=self.temp_dir,
            video_dir=self.video_dir,
            types=["Normal"],
            source=None,
            min_free_gb=20.0,
        )
        self.assertIn("run", payload)
        self.assertIn("summary", payload)
        self.assertIn("rows", payload)
        self.assertIn("diagnostics", payload)
        self.assertIn("sd_card", payload)
        self.assertIn("pipeline", payload)
        self.assertIn("nodes", payload["pipeline"])

    def test_build_pipeline_payload_volumes(self) -> None:
        sd_card = {
            "present": True,
            "trips": [
                {
                    "import_status": "none",
                    "duration_sec": 6 * 3600,
                    "size_bytes": 60_000_000_000,
                    "clip_count": 300,
                },
                {
                    "import_status": "imported",
                    "duration_sec": 4 * 3600,
                    "size_bytes": 40_000_000_000,
                    "clip_count": 200,
                },
                {
                    "import_status": "uploaded",
                    "duration_sec": 2 * 3600,
                    "size_bytes": 20_000_000_000,
                    "clip_count": 100,
                },
            ],
        }
        rows = [
            {
                "record_type": "Normal",
                "chunk_index": 1,
                "status": "done",
                "duration_sec": 2 * 3600,
            },
            {
                "record_type": "Normal",
                "chunk_index": 2,
                "status": "upload",
                "duration_sec": 3 * 3600,
            },
            {
                "record_type": "Normal",
                "chunk_index": 3,
                "status": "pending",
                "duration_sec": 1 * 3600,
            },
        ]
        pipeline = build_pipeline_payload(
            sd_card=sd_card,
            rows=rows,
            usage={"merged": 5_000_000_000, "composed": 2_000_000_000, "total": 7_000_000_000},
            live={"phase": "compose"},
            processes=[],
            temp_dir=self.temp_dir,
        )
        by_id = {n["id"]: n for n in pipeline["nodes"]}
        self.assertAlmostEqual(by_id["sd"]["passed"]["hours_sec"], 12 * 3600)
        self.assertEqual(by_id["sd"]["passed"]["clips"], 600)
        self.assertAlmostEqual(by_id["copy"]["passed"]["hours_sec"], 6 * 3600)
        self.assertEqual(by_id["copy"]["passed"]["clips"], 300)
        self.assertEqual(by_id["merge"]["on_disk"]["bytes"], 5_000_000_000)
        self.assertAlmostEqual(by_id["compose"]["passed"]["hours_sec"], 5 * 3600)
        self.assertEqual(by_id["compose"]["passed"]["chunks"], 2)
        self.assertEqual(by_id["compose"]["state"], "active")
        self.assertEqual(pipeline["active_ids"], ["compose"])

    def test_filemap_falls_back_to_find_sd_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "Normal" / "Front"
            front.mkdir(parents=True)
            (front / "NO20260814-100000-000001F.MP4").write_bytes(b"x" * 10)
            import autopilot_web as aw

            real_find = None
            try:
                from publish_all_70mai import find_sd_card as real_find

                def fake_find():
                    return root

                import publish_all_70mai as pa

                pa.find_sd_card = fake_find
                payload = aw.build_file_map_payload(
                    None,
                    ["Normal"],
                    video_dir=self.video_dir,
                    temp_dir=self.temp_dir,
                )
            finally:
                if real_find is not None:
                    import publish_all_70mai as pa

                    pa.find_sd_card = real_find

            self.assertTrue(payload["present"])
            self.assertEqual(payload["total"], 1)

    def test_filemap_endpoint(self) -> None:
        quit_event = threading.Event()
        port = self._free_port()
        server = AutopilotWebServer(
            host="127.0.0.1",
            port=port,
            temp_dir=self.temp_dir,
            video_dir=self.video_dir,
            types=["Normal"],
            min_free_gb=20.0,
            on_control=lambda action, data: "ok",
            quit_event=quit_event,
        )
        server.start()
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/filemap",
                timeout=5,
            ) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            self.assertIn("groups", parsed)
            self.assertIn("counts", parsed)
            self.assertIn("total", parsed)
        finally:
            server.stop()

    def test_server_binds_loopback_only(self) -> None:
        quit_event = threading.Event()
        port = self._free_port()
        server = AutopilotWebServer(
            host="127.0.0.1",
            port=port,
            temp_dir=self.temp_dir,
            video_dir=self.video_dir,
            types=["Normal"],
            min_free_gb=20.0,
            on_control=lambda action, data: "ok",
            quit_event=quit_event,
        )
        server.start()
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status",
                timeout=5,
            ) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            self.assertIn("summary", parsed)
        finally:
            server.stop()

    def test_control_post_stop(self) -> None:
        seen: list[str] = []
        quit_event = threading.Event()
        port = self._free_port()

        def on_control(action: str, data: dict) -> str:
            seen.append(action)
            return f"handled {action}"

        server = AutopilotWebServer(
            host="127.0.0.1",
            port=port,
            temp_dir=self.temp_dir,
            video_dir=self.video_dir,
            types=["Normal"],
            min_free_gb=20.0,
            on_control=on_control,
            quit_event=quit_event,
        )
        server.start()
        try:
            import urllib.request

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/control",
                data=json.dumps({"action": "stop"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(body.get("ok"))
            self.assertEqual(seen, ["stop"])
        finally:
            server.stop()




if __name__ == "__main__":
    unittest.main()
