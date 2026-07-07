#!/usr/bin/env python3
"""Autopilot: SD card → import → compose 2-cam → YouTube → delete local MP4.

Run outside Cursor — one command after inserting the dashcam SD card:

  ./scripts/publish_all_70mai.sh --wait

Skips trips already uploaded (state on SD card `.70mai/publish/` + local cache).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from import_70mai import log
from plan_estimate import DEFAULT_SESSION_GAP, build_plan
from publish_70mai import trip_uploaded
from publish_state import StateStore

DEFAULT_SOURCE = Path("/Volumes/Untitled")
DEFAULT_TYPES = ["Normal"]
DEFAULT_VIDEO_DIR = Path("video/Output")
DEFAULT_TEMP_DIR = Path("video/Output/.publish_tmp")
DEFAULT_LOG = DEFAULT_TEMP_DIR / "publish_all.log"
LOCK_FILE = DEFAULT_TEMP_DIR / ".publish_all.lock"
SD_POLL_SEC = 15
PUBLISH_WAIT_SEC = 30

# Directories that indicate a 70mai SD card layout.
SD_MARKERS = (
    ("Normal", "Front"),
    ("Normal", "Back"),
)


def find_sd_card() -> Path | None:
    candidates: list[Path] = []
    if DEFAULT_SOURCE.is_dir() and _looks_like_70mai_sd(DEFAULT_SOURCE):
        candidates.append(DEFAULT_SOURCE.resolve())
    volumes = Path("/Volumes")
    if volumes.is_dir():
        for vol in sorted(volumes.iterdir()):
            if vol.name.startswith("."):
                continue
            if _looks_like_70mai_sd(vol):
                candidates.append(vol.resolve())
    if not candidates:
        return None
    # Prefer Untitled, else first match.
    for c in candidates:
        if c.name == "Untitled":
            return c
    return candidates[0]


def _looks_like_70mai_sd(path: Path) -> bool:
    return all((path / rec / cam).is_dir() for rec, cam in SD_MARKERS)


def wait_for_sd(*, poll_sec: int = SD_POLL_SEC) -> Path:
    log(f"Waiting for 70mai SD card (poll every {poll_sec}s)...")
    while True:
        sd = find_sd_card()
        if sd:
            log(f"SD card found: {sd}")
            return sd
        time.sleep(poll_sec)


def wait_for_other_publish(*, poll_sec: int = PUBLISH_WAIT_SEC) -> None:
    while _publish_running():
        log("Another publish_70mai.py is running — waiting...")
        time.sleep(poll_sec)


def _publish_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "publish_70mai\\.py"],
            capture_output=True,
            text=True,
        )
        return out.returncode == 0
    except OSError:
        return False


def acquire_lock() -> None:
    DEFAULT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.is_file():
        pid = LOCK_FILE.read_text(encoding="utf-8").strip()
        log(f"ERROR: another publish_all may be running (lock {LOCK_FILE}, pid {pid})")
        raise SystemExit(1)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def append_log(path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{'=' * 60}\n")
        handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {header}\n")
        handle.write(f"{'=' * 60}\n")


def run_step(
    cmd: list[str],
    *,
    log_path: Path,
    dry_run: bool,
) -> int:
    append_log(log_path, " ".join(cmd))
    log(f"\n>>> {' '.join(cmd)}")
    if dry_run:
        log("(dry-run — skipped)")
        return 0
    with log_path.open("a", encoding="utf-8") as handle:
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT)
    return proc.returncode


def pending_trips(
    source: Path,
    types: list[str],
    state: dict,
    *,
    ffprobe: str,
    chunk_minutes: float,
    session_gap: float,
) -> tuple[list, list, int, int]:
    trips, chunks, _ = build_plan(
        source,
        types,
        chunk_minutes=chunk_minutes,
        chunk_mode="trips",
        session_gap=session_gap,
        ffprobe=ffprobe,
    )
    total = sum(len(c.trips) for c in chunks)
    pending = 0
    for chunk in chunks:
        for trip_idx, _trip in enumerate(chunk.trips, start=1):
            if not trip_uploaded(state, chunk.record_type, chunk.index, trip_idx):
                pending += 1
    return trips, chunks, total, pending


def auto_title(trips: list) -> str:
    if trips:
        return f"70mai {trips[0].start:%Y-%m-%d}"
    return f"70mai {datetime.now():%Y-%m-%d}"


def print_plan_summary(
    chunks: list,
    *,
    total_trips: int,
    pending: int,
    state_path: Path,
) -> None:
    log("")
    log("=== Autopilot plan ===")
    log(f"  State: {state_path}")
    log(f"  Trips: {total_trips} total, {pending} pending upload")
    for chunk in chunks:
        log(
            f"  Chunk {chunk.index}: {chunk.trip_labels} "
            f"({chunk.duration_sec / 60:.0f} min, ~{chunk.est_mb:.0f} MB est.)"
        )
    log("")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Autopilot: SD → import → compose → YouTube → delete",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="SD card path (auto-detect /Volumes if omitted)",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait until 70mai SD card is inserted",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="After finishing, wait for SD again (daemon-style)",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=DEFAULT_TYPES,
        choices=["Normal", "Event", "Parking"],
    )
    parser.add_argument("--title", default="", help="YouTube base title (auto from SD date)")
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--temp-dir", type=Path, default=DEFAULT_TEMP_DIR)
    parser.add_argument("--chunk-minutes", type=float, default=120.0)
    parser.add_argument("--session-gap", type=float, default=DEFAULT_SESSION_GAP)
    parser.add_argument("--skip-import", action="store_true", help="Skip import/merge step")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-state-on-sd",
        action="store_true",
        help="Keep upload state only on host, not on SD card",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help="Master log file (append)",
    )
    args = parser.parse_args()

    from project_env import ensure_venv_python

    ensure_venv_python()
    python = sys.executable

    def run_once() -> int:
        if args.wait or args.source is None:
            if args.wait:
                source = wait_for_sd()
            else:
                source = find_sd_card()
                if source is None:
                    log("No 70mai SD card found. Insert card or use --wait.")
                    return 1
        else:
            source = args.source.resolve()
            if not source.is_dir():
                log(f"Source not found: {source}")
                return 1

        ffprobe = shutil.which("ffprobe")
        if not ffprobe and not args.dry_run:
            log("ffprobe not found")
            return 1

        label = "_".join(args.types)
        state_on_sd = not args.no_state_on_sd
        state_store = StateStore(
            source, args.temp_dir, label, state_on_sd=state_on_sd
        )
        st_path = state_store.primary_path
        state = state_store.load(resume=True)

        trips, chunks, total, pending = pending_trips(
            source,
            args.types,
            state,
            ffprobe=ffprobe or "ffprobe",
            chunk_minutes=args.chunk_minutes,
            session_gap=args.session_gap,
        )
        if pending == 0:
            log("All trips already uploaded — nothing to do.")
            return 0

        title = args.title or auto_title(trips)
        print_plan_summary(
            chunks,
            total_trips=total,
            pending=pending,
            state_path=st_path,
        )

        wait_for_other_publish()

        append_log(args.log, f"publish_all start source={source} pending={pending}")
        failed = 0

        if not args.skip_import:
            ec = run_step(
                [
                    python,
                    "import_70mai.py",
                    "--source",
                    str(source),
                    "--types",
                    *args.types,
                    "--output",
                    str(args.video_dir),
                ],
                log_path=args.log,
                dry_run=args.dry_run,
            )
            if ec != 0:
                log(f"Import failed (exit {ec}) — see {args.log}")
                return ec

        ec = run_step(
            [
                python,
                "publish_70mai.py",
                "--source",
                str(source),
                "--types",
                *args.types,
                "--video-dir",
                str(args.video_dir),
                "--temp-dir",
                str(args.temp_dir),
                "--per-trip-upload",
                "--resume",
                "--resume-upload",
                "--continue-on-error",
                "--state-on-sd" if state_on_sd else "--no-state-on-sd",
                "--title",
                title,
            ],
            log_path=args.log,
            dry_run=args.dry_run,
        )
        if ec != 0:
            failed = 1
            log(f"Publish finished with errors (exit {ec}) — see {args.log}")

        log("")
        log(f"Autopilot done. Log: {args.log}")
        if st_path.is_file():
            uploaded = sum(
                1
                for p in json.loads(st_path.read_text()).get("trip_parts", [])
                if p.get("uploaded")
            )
            log(f"State: {uploaded} trip(s) marked uploaded in {st_path.name}")
        return failed

    acquire_lock()
    try:
        if args.loop:
            while True:
                ec = run_once()
                if ec not in (0, 1):
                    return ec
                log(f"Loop: sleeping {SD_POLL_SEC}s before next SD check...")
                time.sleep(SD_POLL_SEC)
        return run_once()
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
