#!/usr/bin/env python3
"""Import and merge 70mai dash cam clips from SD card into longer videos."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

FILENAME_RE = re.compile(
    r"^(NO|EV|PA)(\d{8})-(\d{6})-(\d+)([FB])\.MP4$",
    re.IGNORECASE,
)

RECORD_TYPES = ("Normal", "Event", "Parking")
CAMERAS = ("Front", "Back")
TYPE_PREFIX = {"Normal": "NO", "Event": "EV", "Parking": "PA"}


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def log(msg: str) -> None:
    print(msg, flush=True)


class ProgressTracker:
    def __init__(self, total: int, label: str) -> None:
        self.total = max(total, 1)
        self.label = label
        self.done = 0
        self.start = time.monotonic()
        log(f"== {label}: 0/{self.total} (0%) | elapsed {format_duration(0)}")

    def adjust_total(self, total: int) -> None:
        self.total = max(total, self.done, 1)

    def update(self, detail: str = "") -> None:
        self.done += 1
        elapsed = time.monotonic() - self.start
        pct = 100 * self.done / self.total
        rate = self.done / elapsed if elapsed > 0 else 0.0
        remaining = self.total - self.done
        eta = remaining / rate if rate > 0 else 0.0
        suffix = f" | {detail}" if detail else ""
        log(
            f"== {self.label}: {self.done}/{self.total} ({pct:.0f}%) "
            f"| elapsed {format_duration(elapsed)} "
            f"| ETA {format_duration(eta)}{suffix}"
        )


@dataclass(frozen=True)
class Clip:
    path: Path
    record_type: str
    camera: str
    timestamp: datetime
    sequence: int
    duration: float | None = None

    @property
    def prefix(self) -> str:
        return TYPE_PREFIX[self.record_type]

    @property
    def camera_suffix(self) -> str:
        return "F" if self.camera == "Front" else "B"


def parse_filename(path: Path, record_type: str, camera: str) -> Clip | None:
    match = FILENAME_RE.match(path.name)
    if not match:
        return None
    prefix, date_part, time_part, seq_part, cam_suffix = match.groups()
    expected_prefix = TYPE_PREFIX[record_type]
    expected_suffix = "F" if camera == "Front" else "B"
    if prefix.upper() != expected_prefix or cam_suffix.upper() != expected_suffix:
        return None
    timestamp = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    return Clip(
        path=path,
        record_type=record_type,
        camera=camera,
        timestamp=timestamp,
        sequence=int(seq_part),
    )


def parse_datetime(value: str) -> datetime:
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m-%d-%Y %H:%M:%S",
        "%m-%d-%Y %H:%M",
        "%m-%d-%Y",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date/time: {value!r}. Use YYYY-MM-DD[ HH:MM[:SS]] or MM-DD-YYYY[ HH:MM[:SS]]"
    )


def parse_time(value: str) -> tuple[int, int, int]:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.hour, parsed.minute, parsed.second
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid time: {value!r}. Use HH:MM or HH:MM:SS")


def clip_in_range(
    clip: Clip,
    range_start: datetime | None,
    range_end: datetime | None,
) -> bool:
    if range_start and clip.timestamp < range_start:
        return False
    if range_end and clip.timestamp >= range_end:
        return False
    return True


def filter_clips(
    clips: list[Clip],
    range_start: datetime | None,
    range_end: datetime | None,
) -> list[Clip]:
    return [clip for clip in clips if clip_in_range(clip, range_start, range_end)]


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def format_clock(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


@dataclass(frozen=True)
class SessionRange:
    start: datetime
    end: datetime
    clip_count: int


@dataclass(frozen=True)
class GpsFileSummary:
    path: Path
    point_count: int
    start: datetime
    end: datetime
    size_bytes: int
    points_by_day: tuple[tuple[date, int], ...]


def format_file_size(num_bytes: int) -> str:
    if num_bytes >= 1_000_000_000:
        return f"{num_bytes / 1_000_000_000:.1f} GB"
    if num_bytes >= 1_000_000:
        return f"{num_bytes / 1_000_000:.1f} MB"
    if num_bytes >= 1_000:
        return f"{num_bytes / 1_000:.1f} KB"
    return f"{num_bytes} B"


def scan_gps_files(source: Path) -> list[GpsFileSummary]:
    summaries: list[GpsFileSummary] = []
    for path in sorted(source.glob("GPSData*.txt")):
        if not path.is_file():
            continue
        first: datetime | None = None
        last: datetime | None = None
        count = 0
        by_day: dict[date, int] = {}
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("$"):
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    ts = int(parts[0])
                except ValueError:
                    continue
                timestamp = datetime.fromtimestamp(ts)
                count += 1
                if first is None:
                    first = timestamp
                last = timestamp
                day = timestamp.date()
                by_day[day] = by_day.get(day, 0) + 1
        if first is None or last is None:
            continue
        summaries.append(
            GpsFileSummary(
                path=path,
                point_count=count,
                start=first,
                end=last,
                size_bytes=path.stat().st_size,
                points_by_day=tuple(sorted(by_day.items())),
            )
        )
    return summaries


def print_events_section(event_groups: list[tuple[str, str, list[Clip]]]) -> None:
    log("\n=== Events ===")
    if not event_groups:
        log("  No event clips found.")
        return

    for _record_type, camera, clips in event_groups:
        ordered = sorted(clips, key=lambda c: (c.timestamp, c.sequence))
        start = ordered[0].timestamp
        end = ordered[-1].timestamp
        log(
            f"\nEvent / {camera} — {len(clips)} event(s), "
            f"{format_ts(start)} -> {format_ts(end)}"
        )
        by_day: dict[date, list[Clip]] = {}
        for clip in ordered:
            by_day.setdefault(clip.timestamp.date(), []).append(clip)
        for day in sorted(by_day):
            day_events = by_day[day]
            log(f"  {day.isoformat()}  ({len(day_events)} event(s))")
            for clip in day_events:
                log(f"    {format_clock(clip.timestamp)}  {clip.path.name}")


def print_gps_section(gps_files: list[GpsFileSummary]) -> None:
    log("\n=== GPS tracks ===")
    if not gps_files:
        log("  No GPSData*.txt files found on the card.")
        return

    overall_start = min(item.start for item in gps_files)
    overall_end = max(item.end for item in gps_files)
    total_points = sum(item.point_count for item in gps_files)
    log(
        f"  {len(gps_files)} file(s) | {total_points} points | "
        f"{format_ts(overall_start)} -> {format_ts(overall_end)}"
    )
    log(
        f"  calendar days: {format_day(overall_start)} .. {format_day(overall_end)}"
    )

    merged_days: dict[date, int] = {}
    for item in gps_files:
        for day, count in item.points_by_day:
            merged_days[day] = merged_days.get(day, 0) + count

    for item in gps_files:
        log(
            f"\n  {item.path.name} — {format_file_size(item.size_bytes)}, "
            f"{item.point_count} points"
        )
        log(f"    range: {format_ts(item.start)} -> {format_ts(item.end)}")
        log(f"    days:  {format_day(item.start)} .. {format_day(item.end)}")
        if len(item.points_by_day) <= 14:
            for day, count in item.points_by_day:
                log(f"      {day.isoformat()}  {count} points")
        else:
            for day, count in item.points_by_day[:7]:
                log(f"      {day.isoformat()}  {count} points")
            log(f"      ... ({len(item.points_by_day) - 10} more days)")
            for day, count in item.points_by_day[-3:]:
                log(f"      {day.isoformat()}  {count} points")

    if len(merged_days) <= 21:
        log("\n  GPS by date (all files):")
        for day in sorted(merged_days):
            log(f"    {day.isoformat()}  {merged_days[day]} points")
    else:
        log(
            f"\n  GPS by date: {len(merged_days)} days with data "
            f"({format_day(overall_start)} .. {format_day(overall_end)})"
        )


def session_ranges(clips: list[Clip], gap_seconds: float) -> list[SessionRange]:
    ranges: list[SessionRange] = []
    for session in split_sessions(clips, gap_seconds):
        ranges.append(
            SessionRange(
                start=session[0].timestamp,
                end=session[-1].timestamp,
                clip_count=len(session),
            )
        )
    return ranges


def collect_groups(
    source: Path,
    record_types: list[str],
    cameras: list[str],
    *,
    warn: bool = True,
) -> list[tuple[str, str, list[Clip]]]:
    groups: list[tuple[str, str, list[Clip]]] = []
    for record_type in record_types:
        for camera in cameras:
            folder = source / record_type / camera
            if not folder.is_dir():
                if warn:
                    log(f"Warning: missing folder {folder}")
                continue
            clips = scan_clips(source, [record_type], [camera])
            if clips:
                groups.append((record_type, camera, clips))
    return groups


def print_scan_report(
    source: Path,
    groups: list[tuple[str, str, list[Clip]]],
    gap_seconds: float,
    gps_files: list[GpsFileSummary],
) -> None:
    all_clips = [clip for _, _, clips in groups for clip in clips]
    recording_groups = [
        (record_type, camera, clips)
        for record_type, camera, clips in groups
        if record_type != "Event"
    ]
    event_groups = [
        (record_type, camera, clips)
        for record_type, camera, clips in groups
        if record_type == "Event"
    ]

    log(f"Scanning {source}")
    log(f"Session gap: {gap_seconds:g} sec (pauses longer than this start a new range)\n")

    if not all_clips and not gps_files:
        log("No clips or GPS data found on the card.")
        return

    log("=== Overall ===")
    if all_clips:
        overall_start = min(c.timestamp for c in all_clips)
        overall_end = max(c.timestamp for c in all_clips)
        log(
            f"  video: {len(all_clips)} clips | "
            f"{format_ts(overall_start)} -> {format_ts(overall_end)}"
        )
        log(
            f"  video days: {format_day(overall_start)} .. {format_day(overall_end)}"
        )
    else:
        log("  video: no clips found")

    if gps_files:
        gps_start = min(item.start for item in gps_files)
        gps_end = max(item.end for item in gps_files)
        gps_points = sum(item.point_count for item in gps_files)
        log(
            f"  GPS:   {gps_points} points in {len(gps_files)} file(s) | "
            f"{format_ts(gps_start)} -> {format_ts(gps_end)}"
        )
        log(f"  GPS days: {format_day(gps_start)} .. {format_day(gps_end)}")

    if recording_groups:
        log("\n=== By type / camera ===")
        for record_type, camera, clips in recording_groups:
            start = min(c.timestamp for c in clips)
            end = max(c.timestamp for c in clips)
            sessions = session_ranges(clips, gap_seconds)
            log(
                f"\n{record_type} / {camera} — "
                f"{len(clips)} clips, {format_ts(start)} -> {format_ts(end)}"
            )
            if len(sessions) == 1:
                session = sessions[0]
                log(
                    f"  continuous: {format_ts(session.start)} -> {format_ts(session.end)} "
                    f"({session.clip_count} clips)"
                )
            else:
                log(f"  {len(sessions)} recording session(s):")
                for idx, session in enumerate(sessions, start=1):
                    log(
                        f"    {idx}. {format_ts(session.start)} -> {format_ts(session.end)} "
                        f"({session.clip_count} clips)"
                    )

    print_events_section(event_groups)
    print_gps_section(gps_files)

    if all_clips:
        log("\n=== By date (video) ===")
        by_day: dict[date, list[Clip]] = {}
        for clip in all_clips:
            day = clip.timestamp.date()
            by_day.setdefault(day, []).append(clip)

        for day in sorted(by_day):
            day_clips = by_day[day]
            day_start = min(c.timestamp for c in day_clips)
            day_end = max(c.timestamp for c in day_clips)
            active_groups = sorted(
                {
                    f"{record_type}/{camera}"
                    for record_type, camera, clips in groups
                    if any(c.timestamp.date() == day for c in clips)
                }
            )
            log(
                f"  {day.isoformat()}  {format_clock(day_start)} — {format_clock(day_end)}  "
                f"| {len(day_clips)} clips | {', '.join(active_groups)}"
            )

    log("\nUse --date / --from-time / --to-time or --from / --to to export a range.")


def scan_clips(
    source: Path,
    record_types: list[str],
    cameras: list[str],
    *,
    warn: bool = True,
) -> list[Clip]:
    clips: list[Clip] = []
    for record_type in record_types:
        for camera in cameras:
            folder = source / record_type / camera
            if not folder.is_dir():
                if warn:
                    print(f"Warning: missing folder {folder}", file=sys.stderr)
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.suffix.upper() != ".MP4":
                    continue
                clip = parse_filename(path, record_type, camera)
                if clip:
                    clips.append(clip)
    return clips


def split_sessions(clips: list[Clip], gap_seconds: float) -> list[list[Clip]]:
    if not clips:
        return []
    ordered = sorted(clips, key=lambda c: (c.timestamp, c.sequence))
    sessions: list[list[Clip]] = [[ordered[0]]]
    for clip in ordered[1:]:
        prev = sessions[-1][-1]
        gap = (clip.timestamp - prev.timestamp).total_seconds()
        if gap > gap_seconds:
            sessions.append([clip])
        else:
            sessions[-1].append(clip)
    return sessions


def probe_duration(path: Path, ffprobe: str) -> float:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {path}")
    return float(result.stdout.strip())


def attach_durations(
    clips: list[Clip],
    ffprobe: str,
    cache: dict[Path, float],
    progress: ProgressTracker | None = None,
    assume_seconds: float | None = None,
) -> list[Clip]:
    enriched: list[Clip] = []
    for clip in clips:
        if clip.path not in cache:
            if assume_seconds is not None:
                cache[clip.path] = assume_seconds
            else:
                if progress:
                    progress.update(clip.path.name)
                cache[clip.path] = probe_duration(clip.path, ffprobe)
        enriched.append(
            Clip(
                path=clip.path,
                record_type=clip.record_type,
                camera=clip.camera,
                timestamp=clip.timestamp,
                sequence=clip.sequence,
                duration=cache[clip.path],
            )
        )
    return enriched


def split_chunks(session: list[Clip], chunk_seconds: float) -> list[list[Clip]]:
    if not session:
        return []
    chunks: list[list[Clip]] = []
    current: list[Clip] = []
    current_duration = 0.0
    for clip in session:
        duration = clip.duration or 0.0
        if current and current_duration + duration > chunk_seconds:
            chunks.append(current)
            current = [clip]
            current_duration = duration
        else:
            current.append(clip)
            current_duration += duration
    if current:
        chunks.append(current)
    return chunks


def output_name(chunk: list[Clip]) -> str:
    first = chunk[0]
    last = chunk[-1]
    start = first.timestamp.strftime("%Y%m%d-%H%M%S")
    end = last.timestamp.strftime("%H%M%S")
    return f"{first.prefix}_{start}_{end}_{first.camera_suffix}.mp4"


def escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def merge_clips(
    chunk: list[Clip],
    output_path: Path,
    ffmpeg: str,
    dry_run: bool,
    merge_progress: ProgressTracker | None = None,
) -> str:
    if output_path.exists():
        size_mb = output_path.stat().st_size / 1_000_000
        log(f"  skip (exists, {size_mb:.0f} MB): {output_path.name}")
        if merge_progress:
            merge_progress.update("skipped")
        return "skipped"

    total_duration = sum(c.duration or 0.0 for c in chunk)
    log(
        f"  merging {len(chunk)} clips ({total_duration / 60:.1f} min) "
        f"-> {output_path.name}"
    )
    if dry_run:
        if merge_progress:
            merge_progress.update("planned")
        return "planned"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merge_start = time.monotonic()
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as list_file:
        for clip in chunk:
            list_file.write(f"file '{escape_concat_path(clip.path)}'\n")
        list_path = Path(list_file.name)

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            log(f"  ERROR: {result.stderr.strip()}")
            if output_path.exists():
                output_path.unlink()
            if merge_progress:
                merge_progress.update("failed")
            return "failed"
        size_mb = output_path.stat().st_size / 1_000_000
        elapsed = time.monotonic() - merge_start
        if merge_progress:
            merge_progress.update(f"done {size_mb:.0f} MB in {format_duration(elapsed)}")
        else:
            log(f"  done: {size_mb:.0f} MB in {format_duration(elapsed)}")
        return "merged"
    finally:
        list_path.unlink(missing_ok=True)


def process_group(
    clips: list[Clip],
    output_dir: Path,
    gap_seconds: float,
    chunk_seconds: float,
    ffmpeg: str,
    ffprobe: str,
    dry_run: bool,
    duration_cache: dict[Path, float],
    probe_progress: ProgressTracker | None,
    merge_progress: ProgressTracker,
    assume_seconds: float | None = None,
) -> tuple[int, int, int, int]:
    if not clips:
        return 0, 0, 0, 0

    record_type = clips[0].record_type
    camera = clips[0].camera
    sessions = split_sessions(clips, gap_seconds)
    merged = 0
    skipped = 0
    failed = 0
    planned = 0

    log(f"\n--- {record_type}/{camera}: {len(clips)} clips, {len(sessions)} sessions ---")
    for session_idx, session in enumerate(sessions, start=1):
        uncached = sum(1 for clip in session if clip.path not in duration_cache)
        log(
            f"  session {session_idx}/{len(sessions)}: "
            f"{len(session)} clips, probing {uncached} new file(s)..."
        )
        session_with_duration = attach_durations(
            session,
            ffprobe,
            duration_cache,
            probe_progress,
            assume_seconds=assume_seconds,
        )
        chunks = split_chunks(session_with_duration, chunk_seconds)
        log(f"  session {session_idx}/{len(sessions)} -> {len(chunks)} output file(s)")
        for chunk in chunks:
            out_path = output_dir / record_type / camera / output_name(chunk)
            status = merge_clips(
                chunk, out_path, ffmpeg, dry_run, merge_progress
            )
            if status == "merged":
                merged += 1
            elif status == "skipped":
                skipped += 1
            elif status == "planned":
                planned += 1
            else:
                failed += 1
    return merged, skipped, failed, planned


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(
            f"Required tool '{name}' not found. Install ffmpeg, e.g. `brew install ffmpeg`."
        )
    return path


EXPORT_RANGE = """
Export range (clip start timestamp must satisfy: start <= t < end):

  Date + time window:
    --date 04-27-2026 --from-time 08:00 --to-time 09:00
    --date 2026-04-27                        # whole day

  Full datetime:
    --from "2026-04-27 08:00" --to "2026-04-27 09:00"

  Date formats: YYYY-MM-DD[ HH:MM[:SS]] or MM-DD-YYYY[ HH:MM[:SS]]
  Time formats: HH:MM or HH:MM:SS (for --from-time / --to-time)

Scan SD card for available data ranges:
  python3 import_70mai.py --scan
"""


def parse_types_and_cameras(types: str, cameras: str) -> tuple[list[str], list[str]]:
    record_types = [t.strip() for t in types.split(",") if t.strip()]
    camera_list = [c.strip() for c in cameras.split(",") if c.strip()]
    invalid_types = set(record_types) - set(RECORD_TYPES)
    invalid_cameras = set(camera_list) - set(CAMERAS)
    if invalid_types:
        raise SystemExit(f"Unknown record types: {', '.join(sorted(invalid_types))}")
    if invalid_cameras:
        raise SystemExit(f"Unknown cameras: {', '.join(sorted(invalid_cameras))}")
    return record_types, camera_list


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import and merge 70mai SD card clips into longer videos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXPORT_RANGE,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/Volumes/Untitled"),
        help="SD card mount path",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "video" / "Output",
        help="Output directory for merged videos",
    )
    parser.add_argument(
        "--chunk-minutes",
        type=float,
        default=10.0,
        help="Target chunk length in minutes",
    )
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=120.0,
        help="Start new session if gap between clips exceeds this",
    )
    parser.add_argument(
        "--types",
        default="Normal,Event,Parking",
        help="Comma-separated record types to process",
    )
    parser.add_argument(
        "--cameras",
        default="Front,Back",
        help="Comma-separated cameras to process",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show merge plan without running ffmpeg",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan SD card and show available date/time ranges (no ffmpeg needed)",
    )
    parser.add_argument(
        "--from",
        dest="range_from",
        type=parse_datetime,
        metavar="DATETIME",
        help="Export range start, e.g. 2026-04-27 08:00 or 04-27-2026 08:00",
    )
    parser.add_argument(
        "--to",
        dest="range_to",
        type=parse_datetime,
        metavar="DATETIME",
        help="Export range end (exclusive), e.g. 2026-04-27 09:00",
    )
    parser.add_argument(
        "--date",
        type=parse_datetime,
        metavar="DATE",
        help="Shortcut: date only, use with --from-time and --to-time",
    )
    parser.add_argument(
        "--from-time",
        type=parse_time,
        metavar="HH:MM",
        help="Range start time on --date, e.g. 08:00",
    )
    parser.add_argument(
        "--to-time",
        type=parse_time,
        metavar="HH:MM",
        help="Range end time on --date (exclusive), e.g. 09:00",
    )
    args = parser.parse_args()

    range_start = args.range_from
    range_end = args.range_to
    if args.date or args.from_time or args.to_time:
        if not args.date:
            raise SystemExit("--date is required when using --from-time or --to-time")
        day = args.date.date()
        if args.from_time:
            h, m, s = args.from_time
            range_start = datetime(day.year, day.month, day.day, h, m, s)
        else:
            range_start = datetime(day.year, day.month, day.day)
        if args.to_time:
            h, m, s = args.to_time
            range_end = datetime(day.year, day.month, day.day, h, m, s)
        else:
            range_end = datetime(day.year, day.month, day.day, 23, 59, 59)
    if range_start and range_end and range_start >= range_end:
        raise SystemExit(f"Invalid range: {range_start} >= {range_end}")

    record_types, cameras = parse_types_and_cameras(args.types, args.cameras)
    if not args.source.is_dir():
        raise SystemExit(f"Source not found: {args.source}")

    if args.scan:
        if args.dry_run:
            log("Note: --dry-run is ignored with --scan")
        scan_types = list(dict.fromkeys([*record_types, "Event"]))
        groups = collect_groups(args.source, scan_types, cameras)
        gps_files = scan_gps_files(args.source)
        print_scan_report(args.source, groups, args.gap_seconds, gps_files)
        return 0

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    chunk_seconds = args.chunk_minutes * 60.0
    run_start = time.monotonic()

    log(f"Source:  {args.source}")
    log(f"Output:  {args.output}")
    log(f"Chunk:   {args.chunk_minutes:g} min")
    log(f"Gap:     {args.gap_seconds:g} sec")
    log(f"Types:   {', '.join(record_types)}")
    log(f"Cameras: {', '.join(cameras)}")
    if range_start or range_end:
        log(f"Range:   {range_start or '...'} -> {range_end or '...'}")
    if args.dry_run:
        log("Mode:    dry-run")

    groups: list[tuple[str, str, list[Clip]]] = []
    total_clips = 0
    for record_type in record_types:
        for camera in cameras:
            folder = args.source / record_type / camera
            if not folder.is_dir():
                log(f"Warning: missing folder {folder}")
                continue
            clips = scan_clips(args.source, [record_type], [camera], warn=False)
            clips = filter_clips(clips, range_start, range_end)
            if clips:
                groups.append((record_type, camera, clips))
                total_clips += len(clips)

    total_outputs = 0
    for _, _, clips in groups:
        for session in split_sessions(clips, args.gap_seconds):
            session_seconds = len(session) * 60
            total_outputs += max(1, -(-session_seconds // int(chunk_seconds)))

    log(f"\nFound {total_clips} clips in {len(groups)} group(s)")
    log(f"Estimated output files: ~{total_outputs}")

    assume_seconds = 60.0 if args.dry_run else None
    if assume_seconds is not None:
        log("Dry-run uses assumed 60s per clip (no ffprobe) for a fast preview")

    probe_progress = (
        None if assume_seconds is not None else ProgressTracker(total_clips, "Probing clip durations")
    )
    merge_progress = ProgressTracker(max(total_outputs, 1), "Merging output files")

    duration_cache: dict[Path, float] = {}
    total_merged = 0
    total_skipped = 0
    total_failed = 0
    total_planned = 0

    for group_idx, (record_type, camera, clips) in enumerate(groups, start=1):
        log(f"\n>>> Group {group_idx}/{len(groups)}: {record_type}/{camera}")
        merged, skipped, failed, planned = process_group(
            clips,
            args.output,
            args.gap_seconds,
            chunk_seconds,
            ffmpeg,
            ffprobe,
            args.dry_run,
            duration_cache,
            probe_progress,
            merge_progress,
            assume_seconds=assume_seconds,
        )
        total_merged += merged
        total_skipped += skipped
        total_failed += failed
        total_planned += planned

    elapsed = time.monotonic() - run_start
    if args.dry_run:
        log(
            f"\nDone in {format_duration(elapsed)}: "
            f"{total_planned} planned, {total_skipped} skipped, {total_failed} failed"
        )
    else:
        log(
            f"\nDone in {format_duration(elapsed)}: "
            f"{total_merged} merged, {total_skipped} skipped, {total_failed} failed"
        )
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
