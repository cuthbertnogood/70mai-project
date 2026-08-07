#!/usr/bin/env python3
"""Wait for autopilot compose to finish, run bench, emit status JSONL."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / "video/Output/.publish_tmp/bench_watch_status.json"
AUTOPILOT_STATUS = ROOT / "video/Output/.publish_tmp/autopilot_status.json"
BENCH_DIR = ROOT / "video/Output/.publish_tmp/bench_1h_hevc"
COMPOSE_LOG = BENCH_DIR / "compose.log"
BENCH_LOG = BENCH_DIR / "bench.log"
TIMING = BENCH_DIR / "timing.jsonl"
SUMMARY = BENCH_DIR / "summary.md"
PROFILE = os.environ.get("BENCH_PROFILE", "youtube")

ENCODE_RE = re.compile(
    r"Encode:.*?\((\d+\.?\d*)%\).*?speed ([0-9.]+)x|encoding \([^,]+, (\d+)%, ([0-9.]+)x\)"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_status(**fields) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": now_iso(), **fields}
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_autopilot() -> dict:
    if not AUTOPILOT_STATUS.is_file():
        return {}
    try:
        return json.loads(AUTOPILOT_STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def parse_compose_log() -> dict:
    if not COMPOSE_LOG.is_file():
        return {}
    try:
        lines = COMPOSE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    out: dict = {}
    for line in reversed(lines[-80:]):
        if "ERROR" in line:
            out["last_error"] = line.strip()[-200:]
            break
    for line in reversed(lines[-40:]):
        m = ENCODE_RE.search(line)
        if m:
            if m.group(1):
                out["percent"] = float(m.group(1))
                out["speed_x"] = float(m.group(2))
            else:
                out["percent"] = float(m.group(3))
                out["speed_x"] = float(m.group(4))
            out["compose_line"] = line.strip()[-120:]
            break
    mp4 = BENCH_DIR / "bench_1h.mp4"
    if mp4.is_file():
        out["output_bytes"] = mp4.stat().st_size
    return out


def bench_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "bench_hevc_no_lock.sh|compose_2cam_70mai.py.*bench_1h_hevc"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0
    except OSError:
        return False


def autopilot_compose_active() -> bool:
    """True if autopilot (not bench) is running compose ffmpeg."""
    try:
        r = subprocess.run(["pgrep", "-fl", "ffmpeg"], capture_output=True, text=True)
        if r.returncode != 0:
            return False
        for line in r.stdout.splitlines():
            if "bench_1h_hevc" in line:
                continue
            if "chunk_" in line or "trip_" in line or ".publish_tmp/Normal" in line:
                return True
            if "compose_2cam" in line and "bench_1h_hevc" not in line:
                return True
        return False
    except OSError:
        return False


def wait_autopilot_clear(poll: float = 30.0) -> None:
    while True:
        st = read_autopilot()
        phase = str(st.get("phase") or "")
        detail = st.get("detail") or ""
        composing = autopilot_compose_active() or phase == "compose"
        write_status(
            state="waiting_autopilot",
            autopilot_phase=phase,
            autopilot_detail=detail,
            autopilot_compose_active=composing,
            message="Ждём окончания compose автопилота (нет параллельного ffmpeg)",
        )
        if not composing:
            return
        time.sleep(poll)


def run_bench() -> int:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    write_status(
        state="starting",
        profile=PROFILE,
        message=f"Запуск bench profile={PROFILE}",
    )
    env = {**os.environ, "BENCH_PROFILE": PROFILE}
    proc = subprocess.Popen(
        [str(ROOT / "scripts/bench_hevc_no_lock.sh")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    write_status(state="running", profile=PROFILE, pid=proc.pid, phase="compose")
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if BENCH_LOG.is_file():
            pass
        info = parse_compose_log()
        write_status(
            state="running",
            profile=PROFILE,
            pid=proc.pid,
            bench_log_tail=line[-160:] if line else "",
            **info,
        )
    return proc.wait()


def main() -> int:
    write_status(state="init", profile=PROFILE, message="bench watcher started")
    if bench_running():
        write_status(state="running", message="bench уже запущен")
        while bench_running():
            info = parse_compose_log()
            write_status(state="running", **info)
            time.sleep(30)
        write_status(state="done", message="bench завершён (уже был запущен)")
        return 0

    wait_autopilot_clear()
    rc = run_bench()
    info = parse_compose_log()
    if SUMMARY.is_file():
        info["summary_path"] = str(SUMMARY)
    if rc == 0:
        write_status(state="done", exit_code=rc, message="bench OK", **info)
    else:
        write_status(
            state="failed",
            exit_code=rc,
            message=f"bench failed rc={rc}",
            **info,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
