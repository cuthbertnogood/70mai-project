#!/usr/bin/env python3
"""CLI: prune host video leftovers for types already uploaded to YouTube."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from plan_estimate import load_autopilot_plan
from publish_all_70mai import (
    DEFAULT_TEMP_DIR,
    DEFAULT_TYPES,
    DEFAULT_VIDEO_DIR,
    aggregate_plan,
    find_sd_card,
    load_merged_publish_state,
)
from publish_70mai import cleanup_after_successful_uploads, format_file_size, free_disk_gb


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete SSD merges / merge_stage / compose tmp for types already "
            "uploaded (state uploaded=true). SD card sources are not touched."
        )
    )
    parser.add_argument("--source", type=Path, help="SD mount (auto-detect)")
    parser.add_argument(
        "--types",
        nargs="+",
        default=DEFAULT_TYPES,
        choices=["Normal", "Event", "Parking"],
    )
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--temp-dir", type=Path, default=DEFAULT_TEMP_DIR)
    parser.add_argument(
        "--no-state-on-sd",
        action="store_true",
        help="Use host publish_*.state.json only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be freed without deleting",
    )
    args = parser.parse_args()

    source = args.source.resolve() if args.source else find_sd_card()
    if source is None:
        source = Path("/Volumes/Untitled")
        if not source.is_dir():
            print("ERROR: SD not found — pass --source", file=sys.stderr)
            return 1

    state_on_sd = not args.no_state_on_sd
    state = load_merged_publish_state(
        source,
        args.types,
        args.temp_dir,
        state_on_sd=state_on_sd,
        quiet=True,
    )
    chunks = load_autopilot_plan(args.temp_dir) or []
    if not chunks:
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        _trips, chunks, _dur, _total, _pending = aggregate_plan(
            source,
            args.types,
            args.temp_dir,
            state_on_sd=state_on_sd,
            ffprobe=ffprobe,
            chunk_minutes=120.0,
            session_gap=1800.0,
        )

    before = free_disk_gb(Path("."))
    print(
        f"Cleanup uploaded → video={args.video_dir} temp={args.temp_dir} "
        f"types={','.join(args.types)} free_before={before:.1f} GB"
        + (" (dry-run)" if args.dry_run else ""),
        flush=True,
    )
    if args.dry_run:
        from publish_70mai import type_fully_uploaded

        for record_type in args.types:
            ok = type_fully_uploaded(state, record_type, chunks)
            print(
                f"  {record_type}: "
                f"{'WOULD prune merges+stage+compose' if ok else 'skip (not fully uploaded)'}",
                flush=True,
            )
        print("Dry-run — no files deleted.", flush=True)
        return 0

    freed = cleanup_after_successful_uploads(
        video_dir=args.video_dir,
        temp_dir=args.temp_dir,
        state=state,
        chunks=chunks,
        types=args.types,
        prune_merged=True,
    )
    after = free_disk_gb(Path("."))
    print(
        f"Done. Freed {format_file_size(freed)}. Disk free {before:.1f} → {after:.1f} GB",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
