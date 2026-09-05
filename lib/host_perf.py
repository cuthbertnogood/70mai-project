"""Low-overhead host and stage timing trace for one publish run.

The trace is intentionally dependency-free: macOS ``ps`` supplies process
usage, while Python supplies wall-clock and disk measurements.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _process_tree(root_pid: int) -> tuple[list[int], float, int]:
    """Return descendant pids, summed CPU percent, and RSS in KB."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,%cpu=,rss="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return [], 0.0, 0

    rows: dict[int, tuple[int, float, int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
            cpu, rss = float(fields[2]), int(fields[3])
        except ValueError:
            continue
        rows[pid] = (ppid, cpu, rss)

    pids = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _cpu, _rss) in rows.items():
            if ppid in pids and pid not in pids:
                pids.add(pid)
                changed = True
    cpu = sum(rows[pid][1] for pid in pids if pid in rows)
    rss = sum(rows[pid][2] for pid in pids if pid in rows)
    return sorted(pids), round(cpu, 1), rss


def host_snapshot(root_pid: int, disk_path: Path) -> dict:
    pids, cpu, rss_kb = _process_tree(root_pid)
    try:
        disk = shutil.disk_usage(disk_path)
        disk_data = {
            "disk_free_gb": round(disk.free / 1024**3, 2),
            "disk_used_gb": round(disk.used / 1024**3, 2),
        }
    except OSError:
        disk_data = {}
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    return {
        "ts": _now(),
        "event": "sample",
        "root_pid": root_pid,
        "tree_pids": pids,
        "process_cpu_pct": cpu,
        "process_rss_mb": round(rss_kb / 1024, 1),
        "host_cpu_count": os.cpu_count() or 1,
        "load_1m": round(load[0], 2),
        **disk_data,
    }


def append_trace(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": _now(), **row}, ensure_ascii=False) + "\n")


class HostSampler:
    """Sample a subprocess tree while a pipeline stage is running."""

    def __init__(self, path: Path, *, root_pid: int, disk_path: Path, interval: float = 10.0):
        self.path = path
        self.root_pid = root_pid
        self.disk_path = disk_path
        self.interval = max(1.0, interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="host-perf", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            append_trace(self.path, host_snapshot(self.root_pid, self.disk_path))
