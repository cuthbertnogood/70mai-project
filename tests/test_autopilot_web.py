#!/usr/bin/env python3
"""Tests for Autopilot web Dashboard HTTP server."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from autopilot_web import AutopilotWebServer, build_status_payload


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
