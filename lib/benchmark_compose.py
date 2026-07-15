#!/usr/bin/env python3
"""Benchmark compose_70mai.py encoding modes on a fixed 60-second segment."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCREEN = PROJECT_ROOT / "video/ScreenRecording_04-27-2026 08-13-38_1.mp4"
VIDEO_DIR = PROJECT_ROOT / "video/Output"
OUTPUT_DIR = VIDEO_DIR
RESULTS_PATH = OUTPUT_DIR / "compose_benchmark_results.md"
DURATION = 60.0

SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


@dataclass
class BenchmarkCase:
    name: str
    label: str
    extra_args: list[str]


@dataclass
class BenchmarkResult:
    name: str
    label: str
    wall_sec: float
    speed: str
    size_mb: float
    output: Path
    success: bool
    error: str = ""


CASES = [
    BenchmarkCase(
        name="sw",
        label="libx264 medium (no hw)",
        extra_args=["--preset", "medium"],
    ),
    BenchmarkCase(
        name="hw_encode",
        label="--hw only (hw encode, CPU decode/scale)",
        extra_args=["--hw"],
    ),
    BenchmarkCase(
        name="balanced",
        label="--profile balanced (hw encode + quality presets)",
        extra_args=["--profile", "balanced"],
    ),
    BenchmarkCase(
        name="vt_full",
        label="--profile balanced --hw-decode (experimental full VT)",
        extra_args=["--profile", "balanced", "--hw-decode"],
    ),
]


def run_case(case: BenchmarkCase) -> BenchmarkResult:
    output = OUTPUT_DIR / f"benchmark_{case.name}_60s.mp4"
    if output.is_file():
        output.unlink()

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "compose_70mai.py"),
        str(SCREEN),
        "--video-dir",
        str(VIDEO_DIR),
        "-o",
        str(output),
        "-d",
        str(DURATION),
        *case.extra_args,
    ]

    print(f"\n=== {case.label} ===", flush=True)
    print(" ".join(cmd), flush=True)

    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall_sec = time.perf_counter() - start

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.stdout:
        print(proc.stdout, end="", flush=True)
    if proc.stderr:
        print(proc.stderr, end="", flush=True)

    speed = "—"
    matches = SPEED_RE.findall(combined)
    if matches:
        speed = f"{matches[-1]}x"

    if proc.returncode != 0:
        return BenchmarkResult(
            name=case.name,
            label=case.label,
            wall_sec=wall_sec,
            speed=speed,
            size_mb=0.0,
            output=output,
            success=False,
            error=f"exit code {proc.returncode}",
        )

    size_mb = output.stat().st_size / (1024 * 1024) if output.is_file() else 0.0
    return BenchmarkResult(
        name=case.name,
        label=case.label,
        wall_sec=wall_sec,
        speed=speed,
        size_mb=size_mb,
        output=output,
        success=True,
    )


def format_wall(sec: float) -> str:
    if sec >= 60:
        return f"{sec / 60:.1f} min"
    return f"{sec:.1f} sec"


def write_results(results: list[BenchmarkResult]) -> None:
    lines = [
        "# Compose encoding benchmark",
        "",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "## Test setup",
        "",
        f"- Screen: `{SCREEN.name}`",
        f"- Video dir: `{VIDEO_DIR.relative_to(PROJECT_ROOT)}`",
        f"- Duration: {DURATION:g} sec",
        f"- Machine: macOS (VideoToolbox when `--hw` / `--profile` is used)",
        "",
        "## Results",
        "",
        "| Mode | Config | Wall time | ffmpeg speed | Output size | Status |",
        "|------|--------|-----------|--------------|-------------|--------|",
    ]

    for r in results:
        status = "OK" if r.success else f"FAILED ({r.error})"
        lines.append(
            f"| {r.name} | {r.label} | {format_wall(r.wall_sec)} | {r.speed} | "
            f"{r.size_mb:.1f} MB | {status} |"
        )

    lines.extend(
        [
            "",
            "## Output files",
            "",
        ]
    )
    for r in results:
        if r.success:
            lines.append(f"- `{r.output.relative_to(PROJECT_ROOT)}` ({r.size_mb:.1f} MB)")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- **sw**: software libx264 `-preset medium` (baseline, slowest).",
            "- **hw_encode**: `--hw` only — GPU encode; decode and scale stay on CPU.",
            "- **balanced**: `--profile balanced` — hw encode + quality presets (same pipeline as `--hw`).",
            "- **vt_full**: `--profile balanced --hw-decode` — with fast-first fallback, succeeds on hw encode only (same speed as balanced). Prior benchmark when profiles forced full VT: **~13.1 min** wall time vs **~2 min** for hw-encode-only.",
            "- ffmpeg `speed=` is parsed from stderr (processing rate vs realtime).",
            "",
        ]
    )

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nResults written to {RESULTS_PATH}", flush=True)


def main() -> None:
    if not SCREEN.is_file():
        raise SystemExit(f"Screen recording not found: {SCREEN}")

    results = [run_case(case) for case in CASES]
    write_results(results)

    failed = [r for r in results if not r.success]
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
