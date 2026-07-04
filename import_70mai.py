#!/usr/bin/env python3
"""Import and merge 70mai dash cam clips from SD card into longer videos."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FILENAME_RE = re.compile(
    r"^(NO|EV|PA)(\d{8})-(\d{6})-(\d+)([FB])\.MP4$",
    re.IGNORECASE,
)

RECORD_TYPES = ("Normal", "Event", "Parking")
CAMERAS = ("Front", "Back")
TYPE_PREFIX = {"Normal": "NO", "Event": "EV", "Parking": "PA"}


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


def scan_clips(source: Path, record_types: list[str], cameras: list[str]) -> list[Clip]:
    clips: list[Clip] = []
    for record_type in record_types:
        for camera in cameras:
            folder = source / record_type / camera
            if not folder.is_dir():
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


def attach_durations(clips: list[Clip], ffprobe: str, cache: dict[Path, float]) -> list[Clip]:
    enriched: list[Clip] = []
    for clip in clips:
        if clip.path not in cache:
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
) -> str:
    if output_path.exists():
        print(f"  skip (exists): {output_path.name}")
        return "skipped"

    total_duration = sum(c.duration or 0.0 for c in chunk)
    print(
        f"  merge {len(chunk)} clips ({total_duration / 60:.1f} min) -> {output_path.name}"
    )
    if dry_run:
        return "planned"

    output_path.parent.mkdir(parents=True, exist_ok=True)
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
            print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
            if output_path.exists():
                output_path.unlink()
            return "failed"
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

    print(f"\n{record_type}/{camera}: {len(clips)} clips, {len(sessions)} sessions")
    for session_idx, session in enumerate(sessions, start=1):
        session_with_duration = attach_durations(session, ffprobe, duration_cache)
        chunks = split_chunks(session_with_duration, chunk_seconds)
        print(
            f"  session {session_idx}/{len(sessions)}: "
            f"{len(session)} clips -> {len(chunks)} output file(s)"
        )
        for chunk in chunks:
            out_path = output_dir / record_type / camera / output_name(chunk)
            status = merge_clips(chunk, out_path, ffmpeg, dry_run)
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import and merge 70mai SD card clips into longer videos."
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
        default=Path(__file__).resolve().parent / "video",
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
    args = parser.parse_args()

    record_types = [t.strip() for t in args.types.split(",") if t.strip()]
    cameras = [c.strip() for c in args.cameras.split(",") if c.strip()]
    invalid_types = set(record_types) - set(RECORD_TYPES)
    invalid_cameras = set(cameras) - set(CAMERAS)
    if invalid_types:
        raise SystemExit(f"Unknown record types: {', '.join(sorted(invalid_types))}")
    if invalid_cameras:
        raise SystemExit(f"Unknown cameras: {', '.join(sorted(invalid_cameras))}")
    if not args.source.is_dir():
        raise SystemExit(f"Source not found: {args.source}")

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    chunk_seconds = args.chunk_minutes * 60.0

    print(f"Source:  {args.source}")
    print(f"Output:  {args.output}")
    print(f"Chunk:   {args.chunk_minutes:g} min")
    print(f"Gap:     {args.gap_seconds:g} sec")
    print(f"Types:   {', '.join(record_types)}")
    print(f"Cameras: {', '.join(cameras)}")
    if args.dry_run:
        print("Mode:    dry-run")

    duration_cache: dict[Path, float] = {}
    total_merged = 0
    total_skipped = 0
    total_failed = 0
    total_planned = 0

    for record_type in record_types:
        for camera in cameras:
            folder = args.source / record_type / camera
            if not folder.is_dir():
                continue
            clips: list[Clip] = []
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.suffix.upper() != ".MP4":
                    continue
                clip = parse_filename(path, record_type, camera)
                if clip:
                    clips.append(clip)
            merged, skipped, failed, planned = process_group(
                clips,
                args.output,
                args.gap_seconds,
                chunk_seconds,
                ffmpeg,
                ffprobe,
                args.dry_run,
                duration_cache,
            )
            total_merged += merged
            total_skipped += skipped
            total_failed += failed
            total_planned += planned

    if args.dry_run:
        print(
            f"\nDone: {total_planned} planned, {total_skipped} skipped, "
            f"{total_failed} failed"
        )
    else:
        print(
            f"\nDone: {total_merged} merged, {total_skipped} skipped, "
            f"{total_failed} failed"
        )
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
