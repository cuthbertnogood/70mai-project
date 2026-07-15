#!/usr/bin/env python3
"""Autopilot: per-~2h video conveyor with SD→SSD staging.

For each pending chunk (~120 min of trips, or one Event/Parking mega-file):
  1. if SSD merges already cover the window → skip import
  2. else copy only that window SD→SSD, then concat on SSD
  3. compose 2-cam ~2h MP4; delete 10-min merges (after-compose)
  4. upload to YouTube; delete composed MP4

Run: ./scripts/publish_all_70mai.sh --wait
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from import_70mai import format_duration, format_log_line, log as _console_log
from plan_estimate import (
    DEFAULT_SESSION_GAP,
    SINGLE_VIDEO_TYPES,
    format_youtube_quota_note,
    build_plan,
)
from publish_70mai import chunk_uploaded, trip_uploaded
from publish_state import AuthStore, StateStore

_log_sink = None


def log(msg: str) -> None:
    """Print to terminal and mirror to publish_all.log when tee is active."""
    _console_log(msg)
    if _log_sink is not None:
        _log_sink.write(format_log_line(msg) + "\n")
        _log_sink.flush()


def setup_log_tee(path: Path) -> None:
    global _log_sink
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_sink = path.open("a", encoding="utf-8")


def close_log_tee() -> None:
    global _log_sink
    if _log_sink is not None:
        _log_sink.close()
        _log_sink = None

DEFAULT_SOURCE = Path("/Volumes/Untitled")
DEFAULT_TYPES = ["Normal", "Event", "Parking"]
DEFAULT_VIDEO_DIR = Path("video/Output")
DEFAULT_TEMP_DIR = Path("video/Output/.publish_tmp")
DEFAULT_LOG = DEFAULT_TEMP_DIR / "publish_all.log"
IMPORT_CHUNK_MINUTES = 10.0
IMPORT_MERGE_RETRY_MAX = 3
IMPORT_MERGE_RETRY_DELAY_SEC = 15
LOCK_FILE = DEFAULT_TEMP_DIR / ".publish_all.lock"
SD_POLL_SEC = 15
PUBLISH_WAIT_SEC = 30
PUBLISH_WAIT_MAX_SEC = 90
_PUBLISH_CMD_RE = re.compile(r"(?:^|[\s/])publish_70mai\.py(?:\s|$)")

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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_publish_70mai_cmd(cmd: str) -> bool:
    if "publish_all_70mai" in cmd:
        return False
    if not _PUBLISH_CMD_RE.search(cmd):
        return False
    # Ignore shell one-liners that merely mention the script name (pgrep, tail, etc.).
    return "python" in cmd.lower()


def _publish_pids() -> list[int]:
    """PIDs running publish_70mai.py (not publish_all_70mai.py or shell wrappers)."""
    try:
        out = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_s, cmd = parts
        if not _is_publish_70mai_cmd(cmd):
            continue
        try:
            pids.append(int(pid_s))
        except ValueError:
            continue
    return pids


def _orphan_ffmpeg_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_s, cmd = parts
        lower = cmd.lower()
        if "ffmpeg" not in lower:
            continue
        if ".publish_tmp" not in cmd and "/chunk_" not in cmd:
            continue
        try:
            pids.append(int(pid_s))
        except ValueError:
            continue
    return pids


def kill_orphan_ffmpeg() -> None:
    pids = _orphan_ffmpeg_pids()
    if pids:
        _kill_pids(pids, label="orphan ffmpeg")


def _kill_pids(pids: list[int], *, label: str) -> None:
    targets = [p for p in pids if p != os.getpid()]
    if not targets:
        return
    log(f"Sending SIGTERM to {label}: {targets}")
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(3)
    survivors = [p for p in targets if _pid_alive(p)]
    if survivors:
        log(f"Sending SIGKILL to {label}: {survivors}")
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(1)


def ensure_publish_slot(
    *,
    wait_sec: int = PUBLISH_WAIT_SEC,
    max_wait_sec: int = PUBLISH_WAIT_MAX_SEC,
    force: bool = False,
) -> None:
    """Wait for publish_70mai.py to finish; kill stale/orphan processes on timeout or --force-restart."""
    if force:
        kill_orphan_ffmpeg()
        pids = _publish_pids()
        if pids:
            _kill_pids(pids, label="publish_70mai.py")
        return

    waited = 0
    while True:
        pids = _publish_pids()
        if not pids:
            return
        if waited >= max_wait_sec:
            log(
                f"publish_70mai.py still running after {waited}s (pids {pids}) — killing stale process(es)"
            )
            _kill_pids(pids, label="publish_70mai.py")
            if _publish_pids():
                log("ERROR: publish_70mai.py did not stop after SIGKILL")
                raise SystemExit(1)
            return
        log(f"Another publish_70mai.py is running (pids {pids}) — waiting {wait_sec}s...")
        time.sleep(wait_sec)
        waited += wait_sec


def acquire_lock(*, force: bool = False) -> None:
    DEFAULT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.is_file():
        raw = LOCK_FILE.read_text(encoding="utf-8").strip()
        try:
            pid = int(raw)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            if force:
                log(f"Force-restart: killing autopilot pid {pid} (lock {LOCK_FILE})")
                _kill_pids([pid], label="publish_all_70mai.py")
            else:
                log(f"ERROR: another publish_all may be running (lock {LOCK_FILE}, pid {pid})")
                raise SystemExit(1)
        elif pid and not _pid_alive(pid):
            log(f"Removing stale autopilot lock (pid {pid} not running)")
        LOCK_FILE.unlink(missing_ok=True)
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
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    lib_dir = str(Path(__file__).resolve().parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = lib_dir if not existing else f"{lib_dir}:{existing}"
    with log_path.open("a", encoding="utf-8") as handle:
        proc = subprocess.run(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
    return proc.returncode


def load_merged_publish_state(
    source: Path,
    types: list[str],
    temp_dir: Path,
    *,
    state_on_sd: bool,
    quiet: bool = False,
) -> dict:
    """Combine trip_parts from per-type publish state files."""
    merged: dict = {"trip_parts": [], "parts": []}
    for record_type in types:
        store = StateStore(source, temp_dir, record_type, state_on_sd=state_on_sd)
        state = store.load(resume=True, quiet=quiet)
        merged["trip_parts"].extend(state.get("trip_parts", []))
        merged["parts"].extend(state.get("parts", []))
        if state.get("playlist_id") and not merged.get("playlist_id"):
            merged["playlist_id"] = state["playlist_id"]
    return merged


def aggregate_plan(
    source: Path,
    types: list[str],
    temp_dir: Path,
    *,
    state_on_sd: bool,
    ffprobe: str,
    chunk_minutes: float,
    session_gap: float,
) -> tuple[list, list, dict[str, float], int, int]:
    all_trips: list = []
    all_chunks: list = []
    dur_by_type: dict[str, float] = {}
    total = 0
    pending = 0
    for record_type in types:
        store = StateStore(source, temp_dir, record_type, state_on_sd=state_on_sd)
        state = store.load(resume=True)
        trips, chunks, dur, type_total, type_pending = pending_trips(
            source,
            [record_type],
            state,
            ffprobe=ffprobe,
            chunk_minutes=chunk_minutes,
            session_gap=session_gap,
        )
        all_trips.extend(trips)
        all_chunks.extend(chunks)
        dur_by_type.update(dur)
        total += type_total
        pending += type_pending
    return all_trips, all_chunks, dur_by_type, total, pending


def chunk_is_done(state: dict, chunk) -> bool:
    """True if this ~2h chunk was already uploaded (chunk state or all trips)."""
    if chunk_uploaded(state, chunk.record_type, chunk.index):
        return True
    if not chunk.trips:
        return False
    return all(
        trip_uploaded(state, chunk.record_type, chunk.index, trip_idx)
        for trip_idx, _ in enumerate(chunk.trips, start=1)
    )


def chunk_merges_ready(
    video_dir: Path,
    chunk,
    *,
    min_coverage: float = 0.98,
) -> bool:
    """True when Front+Back merged files on SSD already cover every trip in chunk.

    Reuses existing NO_/PA_/EV_ merges — no SD re-copy for that window.
    Uses ffprobe (probe=True) so short Parking/Event merges are not false-ready.
    """
    from compose_70mai import plan_segments, scan_merged_clips

    record_type = chunk.record_type
    front = scan_merged_clips(
        video_dir, "Front", record_type=record_type, probe=True
    )
    back = scan_merged_clips(
        video_dir, "Back", record_type=record_type, probe=True
    )
    if not front or not back:
        return False
    for trip in chunk.trips:
        try:
            fs = plan_segments(front, trip.start, trip.duration_sec, 0.0)
            bs = plan_segments(back, trip.start, trip.duration_sec, 0.0)
        except (ValueError, OSError, RuntimeError):
            return False
        if not fs or not bs:
            return False
        front_cov = sum(seg.duration for seg in fs)
        back_cov = sum(seg.duration for seg in bs)
        need = trip.duration_sec * min_coverage
        if front_cov < need or back_cov < need:
            return False
    return True


def pending_trips(
    source: Path,
    types: list[str],
    state: dict,
    *,
    ffprobe: str,
    chunk_minutes: float,
    session_gap: float,
) -> tuple[list, list, dict[str, float], int, int]:
    trips, chunks, dur_by_type = build_plan(
        source,
        types,
        chunk_minutes=chunk_minutes,
        chunk_mode="trips",
        session_gap=session_gap,
        ffprobe=ffprobe,
    )
    # Count pending *chunks* (one YouTube video each, ~target chunk_minutes).
    total = len(chunks)
    pending = sum(1 for chunk in chunks if not chunk_is_done(state, chunk))
    return trips, chunks, dur_by_type, total, pending


def auto_title(trips: list) -> str:
    if trips:
        return f"70mai {trips[0].start:%Y-%m-%d}"
    return f"70mai {datetime.now():%Y-%m-%d}"


def format_gb(n_bytes: int) -> str:
    return f"{n_bytes / (1024**3):.1f} GB"


def autopilot_disk_usage(
    video_dir: Path,
    temp_dir: Path,
    types: list[str] | None = None,
) -> dict[str, int]:
    """Bytes used by autopilot video intermediates (freed as trips finish / prune).

    Counts only video files (*.mp4 / *.MP4): merged under video_dir and
    composed trips under temp_dir/chunk_*.
    """
    types = types or ["Normal", "Event", "Parking"]
    merged = 0
    for record_type in types:
        for cam in ("Front", "Back"):
            merged += video_files_size_bytes(video_dir / record_type / cam)
    composed = 0
    if temp_dir.is_dir():
        for chunk_dir in temp_dir.glob("chunk_*"):
            composed += video_files_size_bytes(chunk_dir)
    return {
        "merged": merged,
        "composed": composed,
        "total": merged + composed,
    }


def video_files_size_bytes(path: Path) -> int:
    """Sum size of *.mp4 / *.MP4 under path (non-video files ignored)."""
    total = 0
    if not path.is_dir():
        return 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                if not name.lower().endswith(".mp4"):
                    continue
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def print_plan_summary(
    chunks: list,
    *,
    total_trips: int,
    pending: int,
    state_paths: list[Path],
    inventory_summary: Path | None = None,
    check_disk: Path = Path("."),
    min_free_gb: float = 20.0,
    video_dir: Path = Path("video/Output"),
    temp_dir: Path = Path("video/Output/.publish_tmp"),
    types: list[str] | None = None,
    chunk_minutes: float = 120.0,
) -> None:
    from publish_70mai import free_disk_gb

    free = free_disk_gb(check_disk)
    usage = autopilot_disk_usage(video_dir, temp_dir, types=types)
    log("")
    log("=== Autopilot plan ===")
    if state_paths:
        names = ", ".join(p.name for p in state_paths)
        log(f"  State: {names}")
    if inventory_summary is not None:
        log(f"  Card inventory: {inventory_summary}")
    log(
        f"  Videos (~{chunk_minutes:g} min target): "
        f"{total_trips} total, {pending} pending upload"
    )
    log(
        f"  Disk free: {free:.1f} GB "
        f"(reserve {min_free_gb:g} GB"
        + (", OK)" if free >= min_free_gb else ", LOW — prune/wait before compose)")
    )
    log(
        f"  Autopilot video: {format_gb(usage['total'])} "
        f"(merged {format_gb(usage['merged'])}, "
        f"compose tmp {format_gb(usage['composed'])}) "
        "— freed after each ролик uploads (import→compose→YouTube→prune)"
    )
    for chunk in chunks:
        if chunk.record_type in SINGLE_VIDEO_TYPES:
            label = (
                "all events → 1 video"
                if chunk.record_type == "Event"
                else "all parking → 1 video"
            )
        else:
            label = chunk.trip_labels
        log(
            f"  [{chunk.record_type}] chunk {chunk.index}: {label} "
            f"({chunk.duration_sec / 60:.0f} min, ~{chunk.est_mb:.0f} MB est.)"
        )
    quota_note = format_youtube_quota_note(
        pending, temp_dir / "youtube_upload.diag.jsonl"
    )
    if quota_note:
        log(quota_note)
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
        metavar="TYPE",
        help="Record types to process (default: Normal Event — Event/Parking = one merged YouTube video each)",
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
        "--no-auth-on-sd",
        action="store_true",
        help="Keep YouTube OAuth only on host (~/.config/70mai/)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help="Master log file (append)",
    )
    parser.add_argument(
        "--force-restart",
        action="store_true",
        help="Kill stale publish_70mai / lock holder and start fresh (used by watchdog)",
    )
    parser.add_argument(
        "--profile",
        default="balanced",
        help="Compose profile: balanced | draft | quality | hevc (default: balanced)",
    )
    parser.add_argument(
        "--prune-merged",
        choices=("off", "after-compose", "after-upload"),
        default="after-compose",
        help=(
            "Delete 10-min merges once used in the ~2h compose "
            "(default: after-compose — free disk before upload; "
            "after-upload = wait until YouTube confirms)"
        ),
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=20.0,
        metavar="GB",
        help="Disk reserve before each compose (default: 20)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable live TTY progress table",
    )
    parser.add_argument(
        "--upload-chunk-mb",
        type=int,
        default=None,
        metavar="MB",
        help="YouTube upload chunk in MB (default: 256; 0 = whole file)",
    )
    parser.add_argument(
        "--no-overlap",
        action="store_true",
        help="Disable compose/upload overlap in publish",
    )
    parser.add_argument(
        "--repair",
        choices=("auto", "diagnose", "off"),
        default="auto",
        help=(
            "Self-heal short/stale Parking/Event merges before compose "
            "(default: auto; diagnose = log only; off = legacy)"
        ),
    )
    args = parser.parse_args()

    from project_env import ensure_venv_python

    ensure_venv_python()
    python = sys.executable
    last_sd_source: Path | None = None

    def run_once() -> int:
        nonlocal last_sd_source
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

        if last_sd_source is not None and source != last_sd_source:
            log(f"SD card changed: {last_sd_source} → {source}")
        last_sd_source = source

        ffprobe = shutil.which("ffprobe")
        if not ffprobe and not args.dry_run:
            log("ffprobe not found")
            return 1

        auth_label = "_".join(args.types)
        state_on_sd = not args.no_state_on_sd
        auth_on_sd = not args.no_auth_on_sd
        log(
            f"Autopilot: types={', '.join(args.types)} "
            "(Event = merge all → one YouTube upload) "
            f"repair={args.repair}"
        )
        try:
            from card_identity import refresh_card_identity
            from publish_state import get_or_create_card_id

            refresh_card_identity(
                source, get_or_create_card_id(source, create=not args.dry_run)
            )
        except OSError as exc:
            log(f"Warning: card identity refresh failed ({exc})")
        try:
            creds, token = AuthStore.ensure_ready(
                source,
                auth_label,
                auth_on_sd=auth_on_sd,
                state_on_sd=state_on_sd,
                types=args.types,
                dry_run=args.dry_run,
            )
        except (FileNotFoundError, RuntimeError) as exc:
            log(str(exc))
            return 1

        import_label = "_".join(args.types)
        trips, chunks, dur_by_type, total, pending = aggregate_plan(
            source,
            args.types,
            args.temp_dir,
            state_on_sd=state_on_sd,
            ffprobe=ffprobe or "ffprobe",
            chunk_minutes=args.chunk_minutes,
            session_gap=args.session_gap,
        )
        if pending == 0:
            log("All trips/events already uploaded — nothing to do.")
            return 0

        import_store = None
        inventory_summary = None
        if state_on_sd and not args.dry_run:
            from import_state import ImportStateStore, sd_summary_path

            import_store = ImportStateStore(
                source,
                import_label,
                state_on_sd=True,
                local_dir=args.temp_dir,
                chunk_minutes=IMPORT_CHUNK_MINUTES,
                gap_seconds=args.session_gap,
            )
            import_store.save_inventory_from_plan(
                types=args.types,
                trips=trips,
                chunks=chunks,
                dur_by_type=dur_by_type,
                publish_state=load_merged_publish_state(
                    source,
                    args.types,
                    args.temp_dir,
                    state_on_sd=state_on_sd,
                ),
            )
            inventory_summary = sd_summary_path(source)

        state_paths = [
            StateStore(source, args.temp_dir, rt, state_on_sd=state_on_sd).primary_path
            for rt in args.types
        ]
        print_plan_summary(
            chunks,
            total_trips=total,
            pending=pending,
            state_paths=state_paths,
            inventory_summary=inventory_summary,
            check_disk=Path("."),
            min_free_gb=args.min_free_gb,
            video_dir=args.video_dir,
            temp_dir=args.temp_dir,
            types=args.types,
            chunk_minutes=args.chunk_minutes,
        )

        from autopilot_dashboard import Dashboard, write_status

        merged_state = load_merged_publish_state(
            source, args.types, args.temp_dir, state_on_sd=state_on_sd
        )
        dashboard = Dashboard.from_plan(
            chunks,
            merged_state,
            temp_dir=args.temp_dir,
            video_dir=args.video_dir,
            check_disk=Path("."),
            min_free_gb=args.min_free_gb,
            enabled=not args.no_dashboard and not args.dry_run,
            source=source,
            types=args.types,
            state_on_sd=state_on_sd,
        )
        dashboard.start()
        dashboard.render()

        from youtube_upload import (
            ensure_youtube_oauth_for_upload,
            log_oauth_reauth_help,
            oauth_needs_reauth,
        )

        yt_ok, yt_detail = ensure_youtube_oauth_for_upload(
            creds,
            token,
            interactive=not args.dry_run,
        )
        if yt_ok:
            log(f"YouTube upload ready ({yt_detail})")
        elif oauth_needs_reauth(yt_detail):
            log_oauth_reauth_help(
                token_path=token,
                credentials_path=creds,
                reason=yt_detail,
            )
            # Mark first pending trip so dashboard shows the blocker.
            for chunk in chunks:
                for trip_idx, trip in enumerate(chunk.trips, start=1):
                    if not trip_uploaded(
                        merged_state, chunk.record_type, chunk.index, trip_idx
                    ):
                        write_status(
                            args.temp_dir,
                            record_type=chunk.record_type,
                            chunk_index=chunk.index,
                            trip_index=trip_idx,
                            phase="oauth",
                            detail="YouTube OAuth",
                            reason="oauth: invalid_grant — нужен повторный вход (см. лог)",
                        )
                        break
                else:
                    continue
                break
            dashboard.render()
            log("")
            log(
                "Autopilot остановлен: YouTube OAuth недоступен. "
                "Выполните шаги выше и перезапустите (--skip-import если import уже готов)."
            )
            return 1
        else:
            log(
                f"Warning: YouTube upload is not ready ({yt_detail}) — "
                "compose may continue, but upload may fail"
            )

        ensure_publish_slot(force=args.force_restart)

        append_log(args.log, f"publish_all start source={source} pending={pending}")
        failed = 0

        try:
            # Per-~2h chunk conveyor: import window → compose → YouTube → prune → next.
            # Never import the whole SD card before the first upload.
            for record_type in args.types:
                type_store = StateStore(
                    source, args.temp_dir, record_type, state_on_sd=state_on_sd
                )
                type_state = type_store.load(resume=True)
                type_trips, type_chunks, _, _, type_pending = pending_trips(
                    source,
                    [record_type],
                    type_state,
                    ffprobe=ffprobe or "ffprobe",
                    chunk_minutes=args.chunk_minutes,
                    session_gap=args.session_gap,
                )
                if type_pending == 0:
                    log(f"{record_type}: all uploaded, skipping")
                    continue

                type_title = args.title or auto_title(type_trips)
                log(
                    f"\n>>> {record_type}: {type_pending} pending "
                    f"~{args.chunk_minutes:g} min video(s)"
                )

                for chunk in type_chunks:
                    type_state = type_store.load(resume=True, quiet=True)
                    if chunk_is_done(type_state, chunk):
                        log(
                            f"  Skip chunk {chunk.index} "
                            f"({chunk.trip_labels}) — already uploaded"
                        )
                        continue

                    log("")
                    log(
                        f"=== Ролик {record_type} chunk {chunk.index}: "
                        f"{format_duration(chunk.duration_sec)} | "
                        f"{chunk.trip_labels} ==="
                    )
                    log(
                        f"  Window: {chunk.start:%Y-%m-%d %H:%M:%S} → "
                        f"{chunk.end:%Y-%m-%d %H:%M:%S}"
                    )

                    force_import = False
                    repair_store = None
                    if not args.skip_import and args.repair != "off":
                        from pipeline_repair import diagnose_and_repair
                        from publish_70mai import chunk_uploaded as _chunk_uploaded

                        type_store_chk = StateStore(
                            source,
                            args.temp_dir,
                            record_type,
                            state_on_sd=state_on_sd,
                        )
                        already_up = _chunk_uploaded(
                            type_store_chk.load(resume=True, quiet=True),
                            record_type,
                            chunk.index,
                        )
                        if state_on_sd and not args.dry_run:
                            from import_state import ImportStateStore

                            repair_store = ImportStateStore(
                                source,
                                record_type,
                                state_on_sd=True,
                                local_dir=args.temp_dir,
                                chunk_minutes=IMPORT_CHUNK_MINUTES,
                                gap_seconds=args.session_gap,
                            )
                        ok_publish, _issues, actions = diagnose_and_repair(
                            source,
                            args.video_dir,
                            chunk,
                            temp_dir=args.temp_dir,
                            import_store=repair_store,
                            uploaded=already_up,
                            mode=args.repair,
                        )
                        if not ok_publish and args.repair == "auto":
                            force_import = True
                            if actions:
                                log(
                                    f"  [repair] {len(actions)} action(s) — "
                                    "forcing import rebuild"
                                )
                        elif not ok_publish and args.repair == "diagnose":
                            log(
                                "  [repair] diagnose-only: blockers found — "
                                "import will still run if merges not ready"
                            )

                    if not args.skip_import:
                        merges_ok = (
                            not force_import
                            and chunk_merges_ready(args.video_dir, chunk)
                        )
                        if merges_ok:
                            log(
                                "  SSD merges already cover this window — "
                                "skip import (reuse, no SD copy)"
                            )
                        else:
                            from runtime_config import import_settings

                            imp = import_settings(force=True)
                            import_cmd = [
                                python,
                                "lib/import_70mai.py",
                                "--source",
                                str(source),
                                "--types",
                                record_type,
                                "--output",
                                str(args.video_dir),
                                "--gap-seconds",
                                str(imp.get("gap_seconds") or args.session_gap),
                                "--chunk-minutes",
                                str(imp.get("chunk_minutes") or IMPORT_CHUNK_MINUTES),
                                "--chunk-clips",
                                str(int(imp.get("chunk_clips") or 10)),
                                "--stage-batch-clips",
                                str(int(imp.get("stage_batch_clips") or 10)),
                                "--merge-workers",
                                str(int(imp.get("merge_workers") or 1)),
                            ]
                            # Event/Parking = all clips → one file; Normal = this ~2h window only.
                            if record_type not in SINGLE_VIDEO_TYPES:
                                range_end = chunk.end + timedelta(seconds=1)
                                import_cmd.extend(
                                    [
                                        "--from",
                                        chunk.start.strftime("%Y-%m-%d %H:%M:%S"),
                                        "--to",
                                        range_end.strftime("%Y-%m-%d %H:%M:%S"),
                                    ]
                                )
                            if state_on_sd:
                                import_cmd.extend(
                                    ["--state-on-sd", "--skip-inventory-refresh"]
                                )
                            # Live dashboard: copy∥merge heartbeats → autopilot_status.json
                            import_cmd.extend(
                                ["--status-dir", str(args.temp_dir)]
                            )
                            log(
                                "  Import: SD→SSD stage, then concat "
                                f"(window only for {record_type})"
                                + (" [repair rebuild]" if force_import else "")
                            )
                            ec = 0
                            for import_attempt in range(1, IMPORT_MERGE_RETRY_MAX + 1):
                                if force_import and import_attempt > 1:
                                    log(
                                        f"[repair] retry import "
                                        f"({import_attempt}/{IMPORT_MERGE_RETRY_MAX})"
                                    )
                                    from pipeline_repair import diagnose_and_repair

                                    diagnose_and_repair(
                                        source,
                                        args.video_dir,
                                        chunk,
                                        temp_dir=args.temp_dir,
                                        import_store=repair_store
                                        if state_on_sd
                                        else None,
                                        uploaded=False,
                                        mode=args.repair,
                                    )
                                ec = run_step(
                                    import_cmd,
                                    log_path=args.log,
                                    dry_run=args.dry_run,
                                )
                                if ec == 0:
                                    break
                                if import_attempt < IMPORT_MERGE_RETRY_MAX:
                                    log(
                                        f"Import had merge failure(s) — auto-retry "
                                        f"{import_attempt + 1}/{IMPORT_MERGE_RETRY_MAX} "
                                        f"in {IMPORT_MERGE_RETRY_DELAY_SEC}s…"
                                    )
                                    time.sleep(IMPORT_MERGE_RETRY_DELAY_SEC)
                            if ec != 0:
                                log(
                                    f"Import failed for chunk {chunk.index} "
                                    f"(exit {ec}) — see {args.log}"
                                )
                                return ec

                    # One YouTube video ≈ this chunk (trips concat if several short ones).
                    publish_cmd = [
                        python,
                        "lib/publish_70mai.py",
                        "--source",
                        str(source),
                        "--types",
                        record_type,
                        "--video-dir",
                        str(args.video_dir),
                        "--temp-dir",
                        str(args.temp_dir),
                        "--chunk-minutes",
                        str(args.chunk_minutes),
                        "--chunk",
                        str(chunk.index),
                        "--resume",
                        "--resume-upload",
                        "--continue-on-error",
                        "--state-on-sd" if state_on_sd else "--no-state-on-sd",
                        "--credentials",
                        str(creds),
                        "--token",
                        str(token),
                        "--auth-on-sd" if auth_on_sd else "--no-auth-on-sd",
                        "--title",
                        type_title,
                        "--profile",
                        args.profile,
                        "--prune-merged",
                        args.prune_merged,
                        "--min-free-gb",
                        str(args.min_free_gb),
                        "--repair",
                        args.repair,
                    ]
                    if args.upload_chunk_mb is not None:
                        publish_cmd.extend(
                            ["--upload-chunk-mb", str(args.upload_chunk_mb)]
                        )
                    if args.no_overlap:
                        publish_cmd.append("--no-overlap")
                    ec = run_step(
                        publish_cmd,
                        log_path=args.log,
                        dry_run=args.dry_run,
                    )
                    if ec != 0:
                        failed = 1
                        log(
                            f"Publish [{record_type}] chunk {chunk.index} "
                            f"finished with errors (exit {ec}) — see {args.log}"
                        )

            log("")
            from publish_70mai import free_disk_gb

            usage = autopilot_disk_usage(
                args.video_dir, args.temp_dir, types=args.types
            )
            log(
                f"Autopilot done. Log: {args.log} | "
                f"disk free {free_disk_gb(Path('.')):.1f} GB | "
                f"video left {format_gb(usage['total'])} "
                f"(merged {format_gb(usage['merged'])}, "
                f"compose tmp {format_gb(usage['composed'])})"
            )
            for record_type in args.types:
                type_store = StateStore(
                    source, args.temp_dir, record_type, state_on_sd=state_on_sd
                )
                type_path = type_store.primary_path
                if type_path.is_file():
                    uploaded = sum(
                        1
                        for p in json.loads(type_path.read_text()).get(
                            "trip_parts", []
                        )
                        if p.get("uploaded")
                    )
                    log(
                        f"State [{record_type}]: {uploaded} trip(s) uploaded "
                        f"in {type_path.name}"
                    )
            return failed
        finally:
            dashboard.stop()

    acquire_lock(force=args.force_restart)
    setup_log_tee(args.log)
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
        close_log_tee()
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
