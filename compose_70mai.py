#!/usr/bin/env python3
"""Compose vertical video: Screen Recording + Front + Back dashcam."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCREEN_RE = re.compile(
    r"^ScreenRecording_(\d{2}-\d{2}-\d{4}) (\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)
MERGED_RE = re.compile(
    r"^NO_(\d{8})-(\d{6})_(\d{6})_([FB])\.mp4$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MergedClip:
    path: Path
    start: datetime
    end: datetime
    camera: str  # "Front" or "Back"
    duration: float | None = None


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_screen_start(path: Path) -> datetime:
    match = SCREEN_RE.match(path.name)
    if not match:
        raise ValueError(
            f"Cannot parse screen recording start from {path.name!r}. "
            "Expected: ScreenRecording_MM-DD-YYYY HH-MM-SS_*.mp4"
        )
    date_part, time_part = match.groups()
    return datetime.strptime(
        date_part.replace("-", "/") + " " + time_part.replace("-", ":"),
        "%m/%d/%Y %H:%M:%S",
    )


def parse_merged_file(path: Path) -> MergedClip | None:
    match = MERGED_RE.match(path.name)
    if not match:
        return None
    date_part, start_part, end_part, cam_suffix = match.groups()
    start = datetime.strptime(date_part + start_part, "%Y%m%d%H%M%S")
    end = datetime.strptime(date_part + end_part, "%Y%m%d%H%M%S")
    camera = "Front" if cam_suffix.upper() == "F" else "Back"
    return MergedClip(path=path, start=start, end=end, camera=camera)


def scan_merged_clips(video_dir: Path, camera: str, *, probe: bool = True) -> list[MergedClip]:
    folder = video_dir / "Normal" / camera
    if not folder.is_dir():
        return []
    clips: list[MergedClip] = []
    for path in sorted(folder.glob("NO_*.mp4")):
        parsed = parse_merged_file(path)
        if parsed:
            duration = probe_duration(path) if probe else None
            clips.append(
                MergedClip(
                    path=parsed.path,
                    start=parsed.start,
                    end=parsed.end,
                    camera=parsed.camera,
                    duration=duration,
                )
            )
    return clips


def clip_covers(clip: MergedClip, moment: datetime) -> bool:
    if moment < clip.start:
        return False
    if clip.duration is not None:
        return moment < clip.start + timedelta(seconds=clip.duration)
    return moment <= clip.end


def find_clip_at(clips: list[MergedClip], moment: datetime) -> tuple[MergedClip, float]:
    for clip in clips:
        if clip_covers(clip, moment):
            offset = (moment - clip.start).total_seconds()
            return clip, offset
    raise ValueError(f"No merged clip covers {moment:%Y-%m-%d %H:%M:%S}")


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


@dataclass(frozen=True)
class Segment:
    path: Path
    ss: float
    duration: float


def plan_segments(
    clips: list[MergedClip],
    wall_start: datetime,
    duration: float,
    sync_offset: float,
) -> list[Segment]:
    """Build one or more segments when duration crosses chunk boundaries."""
    moment = wall_start + timedelta(seconds=sync_offset)
    remaining = duration
    segments: list[Segment] = []

    while remaining > 0.01:
        clip, offset = find_clip_at(clips, moment)
        duration = clip.duration if clip.duration is not None else probe_duration(clip.path)
        available = duration - offset
        take = min(remaining, available)
        segments.append(Segment(path=clip.path, ss=offset, duration=take))
        remaining -= take
        moment += timedelta(seconds=take)

    return segments


def build_filter(
    width: int,
    screen_inputs: int,
    front_inputs: int,
    back_inputs: int,
) -> str:
    parts: list[str] = []
    idx = 0

    def scale_chain(label_in: str, label_out: str) -> None:
        parts.append(f"[{label_in}:v]scale={width}:-2,setsar=1[{label_out}]")

    if screen_inputs == 1:
        scale_chain("0", "v0")
        screen_label = "v0"
    else:
        labels = []
        for i in range(screen_inputs):
            out = f"s{i}"
            scale_chain(str(i), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={screen_inputs}:v=1:a=0[v0]")
        screen_label = "v0"

    front_base = screen_inputs
    if front_inputs == 1:
        scale_chain(str(front_base), "v1")
        front_label = "v1"
    else:
        labels = []
        for i in range(front_inputs):
            inp = front_base + i
            out = f"f{i}"
            scale_chain(str(inp), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={front_inputs}:v=1:a=0[v1]")
        front_label = "v1"

    back_base = front_base + front_inputs
    if back_inputs == 1:
        scale_chain(str(back_base), "v2")
        back_label = "v2"
    else:
        labels = []
        for i in range(back_inputs):
            inp = back_base + i
            out = f"b{i}"
            scale_chain(str(inp), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={back_inputs}:v=1:a=0[v2]")
        back_label = "v2"

    parts.append(f"[{screen_label}][{front_label}][{back_label}]vstack=inputs=3[vout]")
    return ";".join(parts)


def run_compose(
    screen: Path,
    video_dir: Path,
    output: Path,
    *,
    from_offset: float,
    duration: float,
    sync_offset_front: float,
    sync_offset_back: float,
    width: int,
    crf: int,
    preset: str,
    fps: int,
    dry_run: bool,
) -> None:
    screen_start = parse_screen_start(screen)
    wall_start = screen_start + timedelta(seconds=from_offset)
    wall_end = wall_start + timedelta(seconds=duration)

    front_clips = scan_merged_clips(video_dir, "Front")
    back_clips = scan_merged_clips(video_dir, "Back")
    if not front_clips:
        raise SystemExit(f"No Front merged clips in {video_dir / 'Normal' / 'Front'}")
    if not back_clips:
        raise SystemExit(f"No Back merged clips in {video_dir / 'Normal' / 'Back'}")

    front_segments = plan_segments(
        front_clips, wall_start, duration, sync_offset_front
    )
    back_segments = plan_segments(
        back_clips, wall_start, duration, sync_offset_back
    )

    log(f"Screen start:  {screen_start:%Y-%m-%d %H:%M:%S}")
    log(f"Wall range:    {wall_start:%Y-%m-%d %H:%M:%S} -> {wall_end:%Y-%m-%d %H:%M:%S}")
    log(f"Duration:      {duration:g} sec")
    log(f"Front offset:  {sync_offset_front:+g} sec")
    log(f"Back offset:   {sync_offset_back:+g} sec")
    log("")
    log("Front segments:")
    for seg in front_segments:
        log(f"  ss={seg.ss:.1f} t={seg.duration:.1f}  {seg.path.name}")
    log("Back segments:")
    for seg in back_segments:
        log(f"  ss={seg.ss:.1f} t={seg.duration:.1f}  {seg.path.name}")

    cmd: list[str] = ["ffmpeg", "-y"]
    cmd.extend(["-ss", str(from_offset), "-t", str(duration), "-i", str(screen)])

    for seg in front_segments:
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])
    for seg in back_segments:
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])

    filter_complex = build_filter(
        width,
        screen_inputs=1,
        front_inputs=len(front_segments),
        back_inputs=len(back_segments),
    )
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", "[vout]", "-map", "0:a?"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-r",
            str(fps),
            str(output),
        ]
    )

    log("")
    log("Command:")
    log(" ".join(f'"{a}"' if " " in a else a for a in cmd))

    if dry_run:
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)
    log(f"\nDone: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vertical compose: Screen Recording (top) + Front + Back"
    )
    parser.add_argument(
        "screen",
        type=Path,
        help="Screen recording MP4 (sync reference)",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=Path("video/Output"),
        help="Directory with Normal/Front and Normal/Back merged clips",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output MP4 path (default: video/Output/<name>)",
    )
    parser.add_argument(
        "--from-offset",
        type=float,
        default=0.0,
        help="Start offset in seconds from screen recording start (default: 0)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=60.0,
        help="Output duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--sync-offset-front",
        type=float,
        default=0.0,
        help="Fine-tune Front sync in seconds relative to wall clock",
    )
    parser.add_argument(
        "--sync-offset-back",
        type=float,
        default=0.0,
        help="Fine-tune Back sync in seconds relative to wall clock",
    )
    parser.add_argument("--width", type=int, default=1206, help="Output width in px")
    parser.add_argument("--crf", type=int, default=20, help="x264 CRF quality")
    parser.add_argument("--preset", default="medium", help="x264 preset")
    parser.add_argument("--fps", type=int, default=25, help="Output frame rate")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    args = parser.parse_args()

    if not args.screen.is_file():
        parser.error(f"Screen recording not found: {args.screen}")

    if args.output:
        output = args.output
    else:
        start = parse_screen_start(args.screen)
        moment = start + timedelta(seconds=args.from_offset)
        end = moment + timedelta(seconds=args.duration)
        output = (
            args.video_dir
            / f"NO_{moment:%Y%m%d-%H%M%S}_{end:%H%M%S}_3cam.mp4"
        )

    try:
        run_compose(
            args.screen,
            args.video_dir,
            output,
            from_offset=args.from_offset,
            duration=args.duration,
            sync_offset_front=args.sync_offset_front,
            sync_offset_back=args.sync_offset_back,
            width=args.width,
            crf=args.crf,
            preset=args.preset,
            fps=args.fps,
            dry_run=args.dry_run,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    main()
