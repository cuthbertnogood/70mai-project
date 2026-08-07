#!/usr/bin/env python3
"""Snapshot autopilot performance metrics for bottleneck analysis.

Writes JSONL rows to video/Output/.publish_tmp/perf_monitor.jsonl while
autopilot runs. Safe to run alongside live publish_all.

Usage:
  ./scripts/monitor_autopilot_perf.py
  ./scripts/monitor_autopilot_perf.py --interval 30 --max-samples 200
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = PROJECT_ROOT / "video" / "Output" / ".publish_tmp"
STATUS_PATH = TEMP_DIR / "autopilot_status.json"
LOG_PATH = TEMP_DIR / "publish_all.log"
OUT_PATH = TEMP_DIR / "perf_monitor.jsonl"

RE_COPY_OK = re.compile(r"\[copy\][^\n]*:\s*ok in (\d+)s")
RE_MERGE_DONE = re.compile(r"\[merge\] DONE .*: (\d+) MB in (\d+)s")
RE_ENCODE_SPEED = re.compile(r"speed ([0-9.]+)x|speed=\s*([\d.]+)x")
RE_UPLOAD_MBPS = re.compile(r"(\d+\.?\d*) MB/s")


def tail_lines(path: Path, n: int = 400) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 256_000)
            f.seek(max(0, size - block))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def parse_recent_metrics(lines: list[str]) -> dict:
    copy_secs: list[int] = []
    merge_mb_s: list[float] = []
    encode_speeds: list[float] = []
    upload_mbps: list[float] = []

    for line in lines:
        m = RE_COPY_OK.search(line)
        if m:
            copy_secs.append(int(m.group(1)))
        m = RE_MERGE_DONE.search(line)
        if m:
            mb, sec = int(m.group(1)), int(m.group(2))
            if sec > 0:
                merge_mb_s.append(mb / sec)
        m = RE_ENCODE_SPEED.search(line)
        if m:
            val = m.group(1) or m.group(2)
            if val:
                encode_speeds.append(float(val))
        m = RE_UPLOAD_MBPS.search(line)
        if m:
            upload_mbps.append(float(m.group(1)))

    def stats(vals: list[float]) -> dict | None:
        if not vals:
            return None
        tail = vals[-20:]
        return {
            "n": len(tail),
            "last": tail[-1],
            "avg": round(sum(tail) / len(tail), 2),
            "min": round(min(tail), 2),
            "max": round(max(tail), 2),
        }

    return {
        "copy_ok_sec": stats([float(x) for x in copy_secs]),
        "merge_mb_s": stats(merge_mb_s),
        "encode_Nx": stats(encode_speeds),
        "upload_MB_s": stats(upload_mbps),
    }


def load_status() -> dict:
    if not STATUS_PATH.is_file():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def snapshot(log_tail: int) -> dict:
    st = load_status()
    conveyors = st.get("conveyors") or {}
    lines = tail_lines(LOG_PATH, log_tail)
    return {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "phase": st.get("phase"),
        "detail": st.get("detail"),
        "percent": st.get("percent"),
        "record_type": st.get("record_type"),
        "chunk_index": st.get("chunk_index"),
        "stalled": st.get("stalled"),
        "conveyors": conveyors,
        "stage_ahead": st.get("stage_ahead"),
        "metrics": parse_recent_metrics(lines),
        "log_bytes": LOG_PATH.stat().st_size if LOG_PATH.is_file() else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor autopilot performance")
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Seconds between snapshots (default: 30)",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=0,
        help="Stop after N samples (0 = until import done or Ctrl+C)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUT_PATH,
        help=f"JSONL output path (default: {OUT_PATH})",
    )
    parser.add_argument("--log-tail", type=int, default=400)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    print(f"Monitoring → {args.output} (interval {args.interval}s)", flush=True)

    try:
        while True:
            row = snapshot(args.log_tail)
            with args.output.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
            phase = row.get("phase") or "?"
            detail = row.get("detail") or ""
            m = row.get("metrics") or {}
            copy_s = (m.get("copy_ok_sec") or {}).get("avg")
            merge_s = (m.get("merge_mb_s") or {}).get("avg")
            parts = [f"[{n}] {phase} {detail}"]
            if copy_s is not None:
                parts.append(f"copy~{copy_s}s/clip")
            if merge_s is not None:
                parts.append(f"merge~{merge_s:.0f}MB/s")
            print(" | ".join(parts), flush=True)

            if args.max_samples and n >= args.max_samples:
                break
            if phase not in ("import", "compose", "upload") and n > 3:
                # idle / done — keep monitoring compose/upload if they start
                pass
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
