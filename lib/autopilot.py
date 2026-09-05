#!/usr/bin/env python3
"""Autopilot one-shot orchestrator with localhost web Dashboard."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from autopilot_control import (
    EXIT_USER_STOP,
    clear_control,
    clear_deferred_for_run,
    consume_control,
    peek_control,
    write_command,
    write_run_state,
)
from autopilot_web import DEFAULT_HOST, DEFAULT_PORT, AutopilotWebServer

DEFAULT_TEMP_DIR = Path("video/Output/.publish_tmp")
DEFAULT_VIDEO_DIR = Path("video/Output")
DEFAULT_TYPES = ["Normal", "Event", "Parking"]
RESTART_SEC = 60


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=1, autoraise=True)
    except OSError:
        pass


def _terminate_pipeline(*, child_pid: int | None = None) -> None:
    from publish_all_70mai import force_takeover_pipeline

    force_takeover_pipeline(lock_pid=child_pid)
    if child_pid and child_pid > 0:
        try:
            os.kill(child_pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(1)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass


class AutopilotSupervisor:
    def __init__(
        self,
        *,
        temp_dir: Path,
        video_dir: Path,
        types: list[str],
        host: str,
        port: int,
        min_free_gb: float,
        open_browser: bool,
        publish_args: list[str],
    ) -> None:
        self.temp_dir = temp_dir
        self.video_dir = video_dir
        self.types = types
        self.host = host
        self.port = port
        self.min_free_gb = min_free_gb
        self.open_browser = open_browser
        self.publish_args = publish_args
        self.quit_event = threading.Event()
        self.stop_requested = False
        self.child: subprocess.Popen[str] | None = None
        self._source: Path | None = None
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        clear_control(self.temp_dir)
        clear_deferred_for_run(self.temp_dir)

    def handle_control(self, action: str, data: dict) -> str:
        action = (action or "").strip().lower()
        if action == "quit":
            phase = ""
            try:
                from autopilot_control import read_run_state

                phase = str(read_run_state(self.temp_dir).get("phase") or "")
            except Exception:
                pass
            if phase in ("running", "restarting"):
                return "Quit доступен после завершения или Stop"
            self.quit_event.set()
            write_run_state(self.temp_dir, phase="quitting", message="выход")
            return "Выход…"
        if action == "stop":
            self.stop_requested = True
            write_command(self.temp_dir, "stop")
            _terminate_pipeline(child_pid=self.child.pid if self.child else None)
            write_run_state(
                self.temp_dir,
                phase="stopped",
                message="остановлено оператором",
                stop_requested=True,
                child_pid=None,
            )
            return "Stop: прогон остановлен"
        if action == "skip":
            live: dict = {}
            try:
                from autopilot_dashboard import read_status

                live = read_status(self.temp_dir) or {}
            except Exception:
                pass
            rt = str(data.get("record_type") or live.get("record_type") or "")
            ci = data.get("chunk_index", live.get("chunk_index"))
            write_command(
                self.temp_dir,
                "skip",
                record_type=rt,
                chunk_index=int(ci) if ci is not None else None,
            )
            _terminate_pipeline(child_pid=self.child.pid if self.child else None)
            return "Skip: текущий chunk будет отложен"
        if action == "repair":
            write_command(self.temp_dir, "repair")
            return "Repair запрошен"
        return f"Неизвестная команда: {action}"

    def _spawn_publish_all(self) -> subprocess.Popen[str]:
        root = _root()
        python = sys.executable
        cmd = [
            python,
            str(root / "lib" / "publish_all_70mai.py"),
            "--wait",
            "--no-dashboard",
            "--control",
            "--temp-dir",
            str(self.temp_dir),
            "--video-dir",
            str(self.video_dir),
            "--types",
            *self.types,
            "--min-free-gb",
            str(self.min_free_gb),
            *self.publish_args,
        ]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        lib_dir = str(root / "lib")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = lib_dir if not existing else f"{lib_dir}:{existing}"
        log_path = self.temp_dir / "publish_all.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(root),
        )
        return proc

    def _poll_child_control(self) -> None:
        ctrl = peek_control(self.temp_dir)
        if not ctrl:
            return
        cmd = str(ctrl.get("command") or "")
        if cmd == "stop":
            self.stop_requested = True
            _terminate_pipeline(child_pid=self.child.pid if self.child else None)

    def run_pipeline_loop(self) -> int:
        last_exit = 0
        while not self.quit_event.is_set() and not self.stop_requested:
            from publish_all_70mai import find_sd_card

            sd = find_sd_card()
            if sd:
                self._source = sd
            phase = "waiting_card" if sd is None else "running"
            write_run_state(
                self.temp_dir,
                phase=phase,
                message="ожидание SD" if sd is None else "пайплайн",
                sd_path=str(sd) if sd else None,
                stop_requested=self.stop_requested,
            )
            self.child = self._spawn_publish_all()
            write_run_state(
                self.temp_dir,
                phase="running" if sd else "waiting_card",
                message=f"pid {self.child.pid}",
                child_pid=self.child.pid,
                sd_path=str(sd) if sd else None,
            )
            while True:
                if self.quit_event.is_set():
                    _terminate_pipeline(child_pid=self.child.pid)
                    return last_exit
                if self.stop_requested:
                    _terminate_pipeline(child_pid=self.child.pid)
                    return EXIT_USER_STOP
                self._poll_child_control()
                rc = self.child.poll()
                if rc is not None:
                    last_exit = int(rc)
                    self.child = None
                    consume_control(self.temp_dir)
                    if self.stop_requested or last_exit == EXIT_USER_STOP:
                        write_run_state(
                            self.temp_dir,
                            phase="stopped",
                            message="остановлено",
                            stop_requested=True,
                            pipeline_exit=last_exit,
                        )
                        return EXIT_USER_STOP
                    if last_exit == 0:
                        write_run_state(
                            self.temp_dir,
                            phase="done",
                            message="все ролики обработаны",
                            pipeline_exit=0,
                            sd_path=str(self._source) if self._source else None,
                        )
                        return 0
                    write_run_state(
                        self.temp_dir,
                        phase="restarting",
                        message=f"сбой exit {last_exit}, рестарт через {RESTART_SEC}s",
                        pipeline_exit=last_exit,
                    )
                    time.sleep(RESTART_SEC)
                    break
                time.sleep(0.5)
        return last_exit

    def run(self) -> int:
        server = AutopilotWebServer(
            host=self.host,
            port=self.port,
            temp_dir=self.temp_dir,
            video_dir=self.video_dir,
            types=self.types,
            min_free_gb=self.min_free_gb,
            on_control=self.handle_control,
            quit_event=self.quit_event,
        )
        try:
            server.start()
        except OSError as exc:
            print(f"ERROR: cannot bind {self.host}:{self.port} — {exc}", file=sys.stderr)
            return 1
        url = server.url
        print(f"Autopilot Dashboard: {url}")
        if self.open_browser:
            _open_browser(url)
        pipeline_exit = 0
        try:
            pipeline_exit = self.run_pipeline_loop()
        finally:
            if self.child and self.child.poll() is None:
                _terminate_pipeline(child_pid=self.child.pid)
        while not self.quit_event.is_set():
            if pipeline_exit == EXIT_USER_STOP:
                phase = "stopped"
                msg = "остановлено — нажмите Quit"
            elif pipeline_exit == 0:
                phase = "done"
                msg = "готово — нажмите Quit"
            else:
                phase = "error"
                msg = f"завершено с кодом {pipeline_exit} — нажмите Quit"
            write_run_state(
                self.temp_dir,
                phase=phase,
                message=msg,
                pipeline_exit=pipeline_exit,
                sd_path=str(self._source) if self._source else None,
                dashboard_url=url,
            )
            server.set_source(self._source)
            time.sleep(0.5)
        server.stop()
        return pipeline_exit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Autopilot: SD → import → compose → YouTube (web Dashboard)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--temp-dir", type=Path, default=DEFAULT_TEMP_DIR)
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument(
        "--types",
        nargs="+",
        default=DEFAULT_TYPES,
        choices=["Normal", "Event", "Parking"],
    )
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "publish_args",
        nargs=argparse.REMAINDER,
        help="Extra args passed to publish_all_70mai.py (after --)",
    )
    args = parser.parse_args()
    publish_args = list(args.publish_args)
    if publish_args and publish_args[0] == "--":
        publish_args = publish_args[1:]

    from project_env import ensure_venv_python

    ensure_venv_python()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("ERROR: Dashboard must bind to loopback only", file=sys.stderr)
        return 1

    sup = AutopilotSupervisor(
        temp_dir=args.temp_dir,
        video_dir=args.video_dir,
        types=args.types,
        host=args.host,
        port=args.port,
        min_free_gb=args.min_free_gb,
        open_browser=not args.no_browser,
        publish_args=publish_args,
    )
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())
