#!/usr/bin/env python3
"""Pre-flight estimate: trips, trip-based chunks, disk/quota — writes publish_plan.md."""

from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from import_70mai import (
    RECORD_TYPES,
    Clip,
    format_duration,
    log,
    probe_duration,
    scan_clips,
    split_sessions,
)

DEFAULT_CHUNK_MINUTES = 120
DEFAULT_SESSION_GAP = 120.0
DEFAULT_PLAN_FILE = Path("video/Output/publish_plan.md")
MB_PER_MIN_BALANCED = 45.0
YOUTUBE_DAILY_UPLOADS = 6


@dataclass(frozen=True)
class Trip:
    record_type: str
    index: int
    start: datetime
    end: datetime
    clip_count: int
    duration_sec: float

    @property
    def label(self) -> str:
        return (
            f"{self.start:%Y-%m-%d %H:%M} -> {self.end:%H:%M} "
            f"({format_duration(self.duration_sec)}, {self.clip_count} clips)"
        )


@dataclass(frozen=True)
class ChunkPlan:
    record_type: str
    index: int
    trips: tuple[Trip, ...]

    @property
    def duration_sec(self) -> float:
        return sum(t.duration_sec for t in self.trips)

    @property
    def start(self) -> datetime:
        return self.trips[0].start

    @property
    def end(self) -> datetime:
        return self.trips[-1].end

    @property
    def est_mb(self) -> float:
        return self.duration_sec / 60.0 * MB_PER_MIN_BALANCED

    @property
    def trip_labels(self) -> str:
        if len(self.trips) == 1:
            return f"поездка {self.trips[0].index}"
        nums = ", ".join(str(t.index) for t in self.trips)
        return f"поездки {nums}"


def probe_clips(clips: list[Clip], ffprobe: str) -> list[Clip]:
    if not clips:
        return []

    def probe_one(clip: Clip) -> Clip:
        duration = probe_duration(clip.path, ffprobe)
        return Clip(
            path=clip.path,
            record_type=clip.record_type,
            camera=clip.camera,
            timestamp=clip.timestamp,
            sequence=clip.sequence,
            duration=duration,
        )

    probed: list[Clip] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(probe_one, c) for c in clips]
        for fut in as_completed(futures):
            probed.append(fut.result())
    probed.sort(key=lambda c: (c.timestamp, c.sequence))
    return probed


def trips_from_clips(
    record_type: str, clips: list[Clip], session_gap: float
) -> list[Trip]:
    sessions = split_sessions(clips, session_gap)
    trips: list[Trip] = []
    for idx, session in enumerate(sessions, start=1):
        duration = sum(c.duration or 0.0 for c in session)
        trips.append(
            Trip(
                record_type=record_type,
                index=idx,
                start=session[0].timestamp,
                end=session[-1].timestamp,
                clip_count=len(session),
                duration_sec=duration,
            )
        )
    return trips


def pack_trips_to_chunks(trips: list[Trip], target_sec: float) -> list[ChunkPlan]:
    """Pack trips into upload chunks.

    - Trip >= target: single chunk (may exceed target).
    - Trip < target: accumulate consecutive trips until sum >= target.
    - Remaining short tail: final chunk.
    """
    if not trips:
        return []

    chunks: list[ChunkPlan] = []
    batch: list[Trip] = []
    batch_dur = 0.0
    chunk_idx = 0
    record_type = trips[0].record_type

    def flush_batch() -> None:
        nonlocal batch, batch_dur, chunk_idx
        if not batch:
            return
        chunk_idx += 1
        chunks.append(
            ChunkPlan(record_type=record_type, index=chunk_idx, trips=tuple(batch))
        )
        batch = []
        batch_dur = 0.0

    for trip in trips:
        if trip.duration_sec >= target_sec:
            flush_batch()
            chunk_idx += 1
            chunks.append(
                ChunkPlan(record_type=record_type, index=chunk_idx, trips=(trip,))
            )
        else:
            batch.append(trip)
            batch_dur += trip.duration_sec
            if batch_dur >= target_sec:
                flush_batch()

    flush_batch()
    return chunks


def build_plan(
    source: Path,
    record_types: list[str],
    *,
    chunk_minutes: float,
    chunk_mode: str,
    session_gap: float,
    ffprobe: str,
) -> tuple[list[Trip], list[ChunkPlan], dict[str, float]]:
    target_sec = chunk_minutes * 60.0

    all_trips: list[Trip] = []
    all_chunks: list[ChunkPlan] = []
    dur_by_type: dict[str, float] = {}

    for record_type in record_types:
        front_raw = scan_clips(source, [record_type], ["Front"], warn=False)
        back_raw = scan_clips(source, [record_type], ["Back"], warn=False)
        if not front_raw:
            continue

        log(f"Probing {record_type}/Front ({len(front_raw)} clips)...")
        front = probe_clips(front_raw, ffprobe)
        back = probe_clips(back_raw, ffprobe) if back_raw else []

        front_dur = sum(c.duration or 0.0 for c in front)
        back_dur = sum(c.duration or 0.0 for c in back)
        pair_dur = min(front_dur, back_dur) if back else front_dur
        dur_by_type[record_type] = pair_dur

        front_trips = trips_from_clips(record_type, front, session_gap)
        back_trips = trips_from_clips(record_type, back, session_gap) if back else []
        if len(front_trips) != len(back_trips):
            log(
                f"  Note: Front {len(front_trips)} trips, Back {len(back_trips)} trips "
                "(using Front session boundaries)"
            )

        log(
            f"  {record_type}: {len(front_trips)} trips, "
            f"{format_duration(pair_dur)} (2-cam)"
        )

        if chunk_mode == "fixed":
            raise NotImplementedError("fixed chunk mode not implemented yet")
        chunks = pack_trips_to_chunks(front_trips, target_sec)

        all_trips.extend(front_trips)
        all_chunks.extend(chunks)
        log(
            f"  -> {len(chunks)} chunk(s) @ target {chunk_minutes:g} min ({chunk_mode})"
        )

    return all_trips, all_chunks, dur_by_type


def render_markdown(
    *,
    command: str,
    source: Path,
    chunk_minutes: float,
    chunk_mode: str,
    session_gap: float,
    dur_by_type: dict[str, float],
    trips: list[Trip],
    chunks: list[ChunkPlan],
    disk_free_gb: float,
) -> str:
    total_dur = sum(dur_by_type.values())
    total_chunks = len(chunks)
    peak_mb = max((c.est_mb for c in chunks), default=0.0)
    total_mb = sum(c.est_mb for c in chunks)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"## Plan run {now}",
        "",
        f"**Command:** `{command}`",
        f"**Source:** `{source}`",
        (
            f"**Chunk mode:** `{chunk_mode}` | target `{chunk_minutes:g}` min | "
            f"session gap `{session_gap:g}` s"
        ),
        "",
        "### Summary by type",
        "",
        "| Type | 2-cam duration | Trips | Chunks |",
        "|------|----------------|-------|--------|",
    ]

    for record_type, dur in dur_by_type.items():
        type_trips = [t for t in trips if t.record_type == record_type]
        type_chunks = [c for c in chunks if c.record_type == record_type]
        lines.append(
            f"| {record_type} | {format_duration(dur)} | "
            f"{len(type_trips)} | {len(type_chunks)} |"
        )

    lines.extend(
        [
            (
                f"| **Total** | **{format_duration(total_dur)}** | "
                f"**{len(trips)}** | **{total_chunks}** |"
            ),
            "",
            "### Chunks (trip packing)",
            "",
            "| # | Type | Trips | Wall range | Duration | ~MB |",
            "|---|------|-------|------------|----------|-----|",
        ]
    )

    for chunk in chunks:
        wall = f"{chunk.start:%m-%d %H:%M} -> {chunk.end:%H:%M}"
        lines.append(
            f"| {chunk.index} | {chunk.record_type} | {chunk.trip_labels} | "
            f"{wall} | {format_duration(chunk.duration_sec)} | {chunk.est_mb:.0f} |"
        )

    lines.extend(["", "### Trips detail", ""])
    current_type = ""
    for trip in trips:
        if trip.record_type != current_type:
            current_type = trip.record_type
            lines.append(f"**{current_type}**")
            lines.append("")
        lines.append(f"- {trip.index}. {trip.label}")

    quota_ok = total_chunks <= YOUTUBE_DAILY_UPLOADS
    disk_ok = peak_mb / 1024 < disk_free_gb * 0.9

    lines.extend(
        [
            "",
            "### Checks",
            "",
            f"- **Peak chunk (~MB):** {peak_mb:.0f} MB ({peak_mb / 1024:.1f} GB)",
            (
                f"- **Total composed (~MB):** {total_mb:.0f} MB "
                f"({total_mb / 1024:.1f} GB) if kept"
            ),
            (
                f"- **Disk free:** {disk_free_gb:.1f} GB — "
                + ("OK" if disk_ok else f"LOW (need ~{peak_mb / 1024:.1f} GB peak)")
            ),
            (
                f"- **YouTube uploads:** {total_chunks} — "
                + (
                    f"OK (≤{YOUTUBE_DAILY_UPLOADS}/day default)"
                    if quota_ok
                    else (
                        f"WARN (> {YOUTUBE_DAILY_UPLOADS}/day — "
                        "split across days or request quota)"
                    )
                )
            ),
            "",
        ]
    )
    return "\n".join(lines)


def append_plan_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not path.is_file():
        header = (
            "# 70mai publish plan\n\n"
            "Auto-generated by `plan_estimate.py` / `publish_70mai.py`.\n\n"
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(header + content + "\n")


def print_stdout_summary(
    chunks: list[ChunkPlan], dur_by_type: dict[str, float]
) -> None:
    log("")
    log("=== Trip-based chunk plan ===")
    for record_type, dur in dur_by_type.items():
        n_chunks = sum(1 for c in chunks if c.record_type == record_type)
        log(f"  {record_type:8} {format_duration(dur):>12}  ->  {n_chunks} chunk(s)")
    log("")
    for chunk in chunks:
        log(
            f"  Chunk {chunk.index:2} [{chunk.record_type}] "
            f"{format_duration(chunk.duration_sec):>10} ~{chunk.est_mb:.0f} MB  |  "
            f"{chunk.trip_labels}"
        )
    if chunks:
        log("")
        log(
            f"  Total: {len(chunks)} uploads, peak ~{max(c.est_mb for c in chunks):.0f} MB"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate trip-based publish chunks (pre-flight plan)"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/Volumes/Untitled"),
        help="SD card or merged video root (Normal/, Event/, ...)",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        choices=list(RECORD_TYPES),
        default=["Normal"],
        help="Record types to include (default: Normal)",
    )
    parser.add_argument(
        "--chunk-minutes",
        type=float,
        default=DEFAULT_CHUNK_MINUTES,
        help=f"Target chunk size in minutes (default: {DEFAULT_CHUNK_MINUTES})",
    )
    parser.add_argument(
        "--chunk-mode",
        choices=("trips", "fixed"),
        default="trips",
        help="Chunk packing: trips (default) or fixed wall-clock",
    )
    parser.add_argument(
        "--session-gap",
        type=float,
        default=DEFAULT_SESSION_GAP,
        help="Seconds between clips to start a new trip (default: 120)",
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        default=DEFAULT_PLAN_FILE,
        help="Append markdown report here",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Do not append plan file",
    )
    parser.add_argument(
        "--check-disk",
        type=Path,
        default=Path("."),
        help="Path for disk free check (default: cwd)",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        parser.error(f"Source not found: {args.source}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        parser.error("ffprobe not found (brew install ffmpeg)")

    cmd = " ".join(sys.argv)
    log(f"Source: {args.source}")
    log(
        f"Mode: {args.chunk_mode}, target {args.chunk_minutes:g} min, "
        f"gap {args.session_gap:g}s"
    )

    trips, chunks, dur_by_type = build_plan(
        args.source,
        args.types,
        chunk_minutes=args.chunk_minutes,
        chunk_mode=args.chunk_mode,
        session_gap=args.session_gap,
        ffprobe=ffprobe,
    )

    if not chunks:
        log("No clips found for selected types.")
        raise SystemExit(1)

    print_stdout_summary(chunks, dur_by_type)

    disk = shutil.disk_usage(args.check_disk)
    disk_free_gb = disk.free / (1024**3)
    md = render_markdown(
        command=cmd,
        source=args.source,
        chunk_minutes=args.chunk_minutes,
        chunk_mode=args.chunk_mode,
        session_gap=args.session_gap,
        dur_by_type=dur_by_type,
        trips=trips,
        chunks=chunks,
        disk_free_gb=disk_free_gb,
    )

    if not args.no_write:
        append_plan_file(args.plan_file, md)
        log(f"Plan appended: {args.plan_file}")


if __name__ == "__main__":
    main()
