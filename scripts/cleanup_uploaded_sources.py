#!/usr/bin/env python3
"""Delete merged source MP4s for trips already uploaded to YouTube.

Reads publish state from SD (or local cache), rebuilds trip time ranges from the
SD card, and removes merged files in video/Output that fall fully inside uploaded
trip windows. Source clips on the SD card are untouched — import can rebuild.

Default is dry-run; pass --apply to delete files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LIB = ROOT / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from import_70mai import format_file_size, log
from plan_estimate import build_plan
from publish_70mai import prune_merged_for_trip, trip_uploaded
from publish_state import StateStore

DEFAULT_SOURCE = Path("/Volumes/Untitled")
DEFAULT_VIDEO_DIR = Path("video/Output")
DEFAULT_TEMP_DIR = Path("video/Output/.publish_tmp")
DEFAULT_TYPES = ("Normal", "Event")
DEFAULT_CHUNK_MINUTES = 120.0
DEFAULT_SESSION_GAP = 120.0


def find_sd_card() -> Path | None:
    volumes = Path("/Volumes")
    if not volumes.is_dir():
        return None
    for mount in sorted(volumes.iterdir()):
        if not mount.is_dir() or mount.name.startswith("."):
            continue
        if (mount / "Normal" / "Front").is_dir() and (mount / "Normal" / "Back").is_dir():
            return mount
    return None


def resolve_source(path: Path | None) -> Path:
    if path is not None:
        if not path.is_dir():
            raise SystemExit(f"Source not found: {path}")
        return path.resolve()
    found = find_sd_card()
    if found is None:
        raise SystemExit(
            "SD card not found. Mount the card or pass --source /Volumes/Untitled"
        )
    return found.resolve()


def collect_uploaded_trips(
    source: Path,
    types: list[str],
    temp_dir: Path,
    *,
    state_on_sd: bool,
    ffprobe: str,
    chunk_minutes: float,
    session_gap: float,
) -> list[tuple[str, object]]:
    uploaded: list[tuple[str, object]] = []
    for record_type in types:
        store = StateStore(source, temp_dir, record_type, state_on_sd=state_on_sd)
        state = store.load(resume=True)
        _trips, chunks, _dur = build_plan(
            source,
            [record_type],
            chunk_minutes=chunk_minutes,
            chunk_mode="trips",
            session_gap=session_gap,
            ffprobe=ffprobe,
        )
        for chunk in chunks:
            for trip_idx, trip in enumerate(chunk.trips, start=1):
                if trip_uploaded(state, record_type, chunk.index, trip_idx):
                    uploaded.append((record_type, trip))
    return uploaded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove merged source files for uploaded trips (dry-run by default)."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="SD mount (default: auto-detect /Volumes/Untitled)",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=DEFAULT_VIDEO_DIR,
        help="Merged output root (default: video/Output)",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=DEFAULT_TEMP_DIR,
        help="Local publish state cache",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=list(DEFAULT_TYPES),
        choices=["Normal", "Event", "Parking"],
    )
    parser.add_argument("--chunk-minutes", type=float, default=DEFAULT_CHUNK_MINUTES)
    parser.add_argument("--session-gap", type=float, default=DEFAULT_SESSION_GAP)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--no-state-on-sd",
        action="store_true",
        help="Read publish state only from --temp-dir",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete files (default: dry-run only)",
    )
    args = parser.parse_args()

    from project_env import ensure_venv_python

    ensure_venv_python()

    source = resolve_source(args.source)
    video_dir = args.video_dir.resolve()
    temp_dir = args.temp_dir.resolve()
    state_on_sd = not args.no_state_on_sd

    log(f"Source: {source}")
    log(f"Video dir: {video_dir}")
    log(f"Mode: {'APPLY (delete)' if args.apply else 'dry-run'}")

    uploaded_trips = collect_uploaded_trips(
        source,
        args.types,
        temp_dir,
        state_on_sd=state_on_sd,
        ffprobe=args.ffprobe,
        chunk_minutes=args.chunk_minutes,
        session_gap=args.session_gap,
    )
    if not uploaded_trips:
        log("No uploaded trips found in publish state — nothing to prune.")
        return 0

    log(f"Uploaded trips to prune: {len(uploaded_trips)}")
    total_freed = 0
    total_files = 0

    for record_type, trip in uploaded_trips:
        log(
            f"  {record_type} trip {trip.index}: "
            f"{trip.start:%Y-%m-%d %H:%M:%S} -> {trip.end:%H:%M:%S}"
        )
        if args.apply:
            freed = prune_merged_for_trip(video_dir, record_type, trip.start, trip.end)
            total_freed += freed
        else:
            from compose_70mai import scan_merged_clips
            from publish_70mai import MERGED_PRUNE_MARGIN

            lo = trip.start - MERGED_PRUNE_MARGIN
            hi = trip.end + MERGED_PRUNE_MARGIN
            trip_bytes = 0
            trip_count = 0
            for camera in ("Front", "Back"):
                for clip in scan_merged_clips(
                    video_dir, camera, record_type=record_type, probe=False
                ):
                    if clip.start >= lo and clip.end <= hi:
                        try:
                            trip_bytes += clip.path.stat().st_size
                        except OSError:
                            continue
                        trip_count += 1
                        log(f"    would delete: {clip.path.name} ({camera})")
            total_freed += trip_bytes
            total_files += trip_count

    log("")
    if args.apply:
        log(f"Freed: {format_file_size(total_freed)}")
    else:
        log(
            f"Would delete {total_files} file(s), "
            f"free {format_file_size(total_freed)}"
        )
        log("Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
