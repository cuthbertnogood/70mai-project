#!/usr/bin/env python3
"""Run a safe local Autopilot performance diagnostic.

The diagnostic never uploads, changes publish state, or touches the SD card.
It records host information and, when suitable local media exists, runs a
short compose benchmark against it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RESULT_FILENAME = "autopilot_diagnostics.json"


def _run_text(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").strip()


def host_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    if platform.system() == "Darwin":
        raw = _run_text(
            ["sysctl", "-n", "machdep.cpu.brand_string", "hw.ncpu", "hw.memsize"]
        )
        values = raw.splitlines()
        if values:
            info["cpu"] = values[0]
        if len(values) > 1:
            info["cpu_count"] = int(values[1])
        if len(values) > 2:
            info["memory_gb"] = round(int(values[2]) / 1e9, 1)
    return info


def find_media(root: Path, video_dir: Path) -> tuple[Path | None, bool]:
    screens = sorted(root.glob("video/**/ScreenRecording_*.mp4"))
    merged = any(video_dir.glob("**/NO_*_F.mp4")) and any(
        video_dir.glob("**/NO_*_B.mp4")
    )
    return (screens[0] if screens else None), merged


def write_result(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(args: argparse.Namespace) -> int:
    result_path = args.temp_dir / RESULT_FILENAME
    started = time.time()
    result: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": host_info(),
    }
    write_result(result_path, result)

    screen, merged = find_media(args.root, args.video_dir)
    if screen is None or not merged:
        result.update(
            status="ready",
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            elapsed_sec=round(time.time() - started, 2),
            benchmark=False,
            message=(
                "Локальные ScreenRecording и Front/Back merged-файлы не найдены; "
                "собрана информация о хосте. Для полного теста нужен тестовый набор."
            ),
            media={"screen": str(screen) if screen else None, "merged": merged},
        )
        write_result(result_path, result)
        return 0

    output = args.temp_dir / "diagnostic_compose.mp4"
    cmd = [
        sys.executable,
        str(args.root / "lib" / "compose_70mai.py"),
        str(screen),
        "--video-dir",
        str(args.video_dir),
        "--profile",
        args.profile,
        "-d",
        str(args.duration),
        "-o",
        str(output),
    ]
    started_mono = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - started_mono
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    speed = None
    for line in reversed(combined.splitlines()):
        if "speed " in line and "x" in line:
            try:
                speed = float(line.rsplit("speed ", 1)[1].split("x", 1)[0])
                break
            except ValueError:
                pass
    result.update(
        status="done" if proc.returncode == 0 else "error",
        finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_sec=round(elapsed, 2),
        benchmark=True,
        profile=args.profile,
        duration_sec=args.duration,
        speed_x=speed,
        returncode=proc.returncode,
        media={"screen": str(screen), "merged": merged},
        message=(
            "Compose benchmark завершён без upload."
            if proc.returncode == 0
            else "Compose benchmark завершился ошибкой; подробности в diagnostics.log."
        ),
    )
    (args.temp_dir / "diagnostics.log").write_text(combined, encoding="utf-8")
    if output.exists():
        output.unlink()
    write_result(result_path, result)
    return 0 if proc.returncode == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe local Autopilot benchmark")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--profile", default="youtube")
    parser.add_argument("--duration", type=float, default=60.0)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
