#!/usr/bin/env python3
"""Compose vertical 2-camera video: Front (top) + Back (bottom)."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from compose_70mai import (
    DEFAULT_PROFILE,
    PROFILES,
    Segment,
    append_hwaccel_args,
    append_video_encode_args,
    apply_profile,
    describe_pipeline,
    log,
    plan_segments,
    run_ffmpeg_with_progress,
    scan_merged_clips,
)
from import_70mai import format_duration, parse_datetime
from telemetry_overlay import render_telemetry_video, resolve_gps_sources


def build_filter_2cam(
    width: int,
    front_inputs: int,
    back_inputs: int,
    *,
    use_vt_scale: bool = False,
) -> str:
    parts: list[str] = []

    def scale_chain(label_in: str, label_out: str) -> None:
        if use_vt_scale:
            parts.append(
                f"[{label_in}:v]scale_vt=w={width},"
                f"hwdownload,format=nv12,setsar=1[{label_out}]"
            )
        else:
            parts.append(f"[{label_in}:v]scale={width}:-2,setsar=1[{label_out}]")

    if front_inputs == 1:
        scale_chain("0", "v0")
        front_label = "v0"
    else:
        labels = []
        for i in range(front_inputs):
            out = f"f{i}"
            scale_chain(str(i), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={front_inputs}:v=1:a=0[v0]")
        front_label = "v0"

    back_base = front_inputs
    if back_inputs == 1:
        scale_chain(str(back_base), "v1")
        back_label = "v1"
    else:
        labels = []
        for i in range(back_inputs):
            inp = back_base + i
            out = f"b{i}"
            scale_chain(str(inp), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={back_inputs}:v=1:a=0[v1]")
        back_label = "v1"

    parts.append(f"[{front_label}][{back_label}]vstack=inputs=2[vout]")
    return ";".join(parts)


def append_telemetry_overlay(filter_complex: str, telemetry_input: int, fps: int) -> str:
    return (
        f"{filter_complex};"
        f"[{telemetry_input}:v]fps={fps}[tel];"
        f"[tel]format=rgba[telrgba];"
        f"[vout][telrgba]overlay=W-w-20:20:format=auto,format=yuv420p[vfinal]"
    )


def build_audio_filter_2cam(front_count: int) -> tuple[str, str | None]:
    if front_count <= 0:
        return "", None
    if front_count == 1:
        return "", "0:a?"
    labels = "".join(f"[{i}:a]" for i in range(front_count))
    return f"{labels}concat=n={front_count}:v=0:a=1[aout]", "[aout]"


def build_compose_2cam_cmd(
    front_segments: list[Segment],
    back_segments: list[Segment],
    output: Path,
    *,
    width: int,
    crf: int,
    preset: str,
    fps: int,
    hw: bool,
    hw_quality: int,
    hw_decode: bool,
    use_vt_scale: bool,
    audio_source: str,
    telemetry_path: Path | None = None,
) -> list[str]:
    cmd: list[str] = ["ffmpeg", "-y"]

    for seg in front_segments:
        if hw_decode:
            append_hwaccel_args(cmd)
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])

    for seg in back_segments:
        if hw_decode:
            append_hwaccel_args(cmd)
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])

    telemetry_input: int | None = None
    if telemetry_path is not None:
        telemetry_input = len(front_segments) + len(back_segments)
        cmd.extend(["-i", str(telemetry_path)])

    filter_complex = build_filter_2cam(
        width,
        front_inputs=len(front_segments),
        back_inputs=len(back_segments),
        use_vt_scale=use_vt_scale,
    )

    if audio_source == "front":
        audio_filter, audio_map = build_audio_filter_2cam(len(front_segments))
    elif audio_source == "back":
        base = len(front_segments)
        if len(back_segments) == 1:
            audio_filter, audio_map = "", f"{base}:a?"
        else:
            labels = "".join(f"[{base + i}:a]" for i in range(len(back_segments)))
            audio_filter = (
                f"{labels}concat=n={len(back_segments)}:v=0:a=1[aout]"
            )
            audio_map = "[aout]"
    else:
        audio_filter, audio_map = "", None

    if audio_filter:
        filter_complex = f"{filter_complex};{audio_filter}"

    video_map = "[vout]"
    if telemetry_input is not None:
        filter_complex = append_telemetry_overlay(filter_complex, telemetry_input, fps)
        video_map = "[vfinal]"

    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", video_map])
    if audio_map:
        cmd.extend(["-map", audio_map])

    append_video_encode_args(
        cmd,
        hw=hw,
        crf=crf,
        preset=preset,
        hw_quality=hw_quality,
        fps=fps,
    )
    cmd.append(str(output))
    return cmd


def run_compose_2cam(
    video_dir: Path,
    output: Path,
    *,
    wall_start: datetime,
    duration: float,
    sync_offset_front: float = 0.0,
    sync_offset_back: float = 0.0,
    width: int,
    crf: int,
    preset: str,
    fps: int,
    hw: bool,
    hw_quality: int,
    hw_decode: bool,
    use_vt_scale: bool,
    audio_source: str = "front",
    telemetry: bool = False,
    gps_dir: Path | None = None,
    telemetry_map_size: int = 280,
    gps_offset_sec: float | None = None,
    dry_run: bool = False,
) -> None:
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

    log(f"Wall range:    {wall_start:%Y-%m-%d %H:%M:%S} -> {wall_end:%Y-%m-%d %H:%M:%S}")
    log(f"Duration:      {duration:g} sec ({format_duration(duration)})")
    log(f"Front offset:  {sync_offset_front:+g} sec")
    log(f"Back offset:   {sync_offset_back:+g} sec")
    encoder = (
        f"h264_videotoolbox ({hw_quality * 100}k)"
        if hw
        else f"libx264 (crf {crf}, {preset})"
    )
    log(f"Encoder:       {encoder}")
    log(f"Audio:         {audio_source}")
    log(f"Telemetry:     {'on' if telemetry else 'off'}")
    log(f"Pipeline:      {describe_pipeline(hw=hw, hw_decode=hw_decode, use_vt_scale=use_vt_scale)}")
    log("")
    log("Front segments:")
    for seg in front_segments:
        log(f"  ss={seg.ss:.1f} t={seg.duration:.1f}  {seg.path.name}")
    log("Back segments:")
    for seg in back_segments:
        log(f"  ss={seg.ss:.1f} t={seg.duration:.1f}  {seg.path.name}")

    if hw and hw_decode:
        attempts: list[tuple[bool, bool, str]] = [
            (False, False, "hw encode only"),
            (True, False, "hw decode + CPU scale"),
            (True, True, "full VT (hw decode + scale_vt)"),
        ]
    elif hw:
        attempts = [(False, False, "hw encode only")]
    else:
        attempts = [(False, False, "software encode")]

    last_error: subprocess.CalledProcessError | None = None
    telemetry_path: Path | None = None
    telemetry_tmp: tempfile.TemporaryDirectory[str] | None = None

    if telemetry and not dry_run:
        gps_sources = resolve_gps_sources(gps_dir, video_dir, Path("/Volumes/Untitled"))
        if not gps_sources:
            log("Telemetry: no GPSData*.txt found — continuing without overlay.")
            telemetry = False
        else:
            telemetry_tmp = tempfile.TemporaryDirectory(prefix="70mai_telemetry_")
            telemetry_path = Path(telemetry_tmp.name) / "overlay.mov"
            log(f"GPS sources:   {', '.join(p.name for p in gps_sources)}")
            if not render_telemetry_video(
                gps_sources=gps_sources,
                wall_start=wall_start,
                duration_sec=duration,
                fps=fps,
                output=telemetry_path,
                map_size=telemetry_map_size,
                gps_offset_sec=gps_offset_sec,
            ):
                telemetry = False
                telemetry_path = None

    for attempt_hw_decode, attempt_vt_scale, label in attempts:
        cmd = build_compose_2cam_cmd(
            front_segments,
            back_segments,
            output,
            width=width,
            crf=crf,
            preset=preset,
            fps=fps,
            hw=hw,
            hw_quality=hw_quality,
            hw_decode=attempt_hw_decode,
            use_vt_scale=attempt_vt_scale,
            audio_source=audio_source,
            telemetry_path=telemetry_path if telemetry else None,
        )

        log("")
        log(f"Attempt: {label}")
        log("Command:")
        log(" ".join(f'"{a}"' if " " in a else a for a in cmd))

        if dry_run:
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_ffmpeg_with_progress(cmd, duration_sec=duration)
            if label != describe_pipeline(
                hw=hw, hw_decode=hw_decode, use_vt_scale=use_vt_scale
            ):
                log(f"\nNote: fell back to {label}")
            log(f"\nDone: {output}")
            if telemetry_tmp is not None:
                telemetry_tmp.cleanup()
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if output.is_file():
                output.unlink()
            if attempt_hw_decode or attempt_vt_scale:
                log(f"\n{label} failed (exit {exc.returncode}), trying fallback...")
            else:
                raise

    if telemetry_tmp is not None:
        telemetry_tmp.cleanup()

    if last_error is not None:
        raise last_error


def default_output_path(video_dir: Path, wall_start: datetime, duration: float) -> Path:
    end = wall_start + timedelta(seconds=duration)
    return (
        video_dir
        / f"NO_{wall_start:%Y%m%d-%H%M%S}_{end:%H%M%S}_2cam.mp4"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vertical compose: Front (top) + Back (bottom)"
    )
    parser.add_argument(
        "--from",
        dest="wall_from",
        type=parse_datetime,
        required=True,
        metavar="DATETIME",
        help='Wall-clock start, e.g. "2026-04-27 08:00"',
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--to",
        dest="wall_to",
        type=parse_datetime,
        help="Wall-clock end (exclusive)",
    )
    group.add_argument(
        "-d",
        "--duration",
        type=float,
        help="Duration in seconds",
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
        help="Output MP4 path",
    )
    parser.add_argument(
        "--sync-offset-front",
        type=float,
        default=0.0,
        help="Fine-tune Front sync (seconds)",
    )
    parser.add_argument(
        "--sync-offset-back",
        type=float,
        default=0.0,
        help="Fine-tune Back sync (seconds)",
    )
    parser.add_argument(
        "--audio",
        choices=("front", "back"),
        default="front",
        help="Audio source (default: front)",
    )
    parser.add_argument("--width", type=int, default=1206)
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--hw", action="store_true")
    parser.add_argument("--hw-quality", type=int, default=65, metavar="Q")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default=DEFAULT_PROFILE,
    )
    parser.add_argument("--hw-decode", action="store_true")
    parser.add_argument("--no-vt-scale", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--telemetry",
        action="store_true",
        help="GPS overlay: mini-map, speed, compass, coordinates, G-force",
    )
    parser.add_argument(
        "--gps-dir",
        type=Path,
        help="Directory with GPSData*.txt (default: auto-detect SD card / video-dir)",
    )
    parser.add_argument(
        "--telemetry-map-size",
        type=int,
        default=280,
        metavar="PX",
        help="Mini-map width in pixels (default: 280)",
    )
    parser.add_argument(
        "--gps-offset",
        type=float,
        default=None,
        metavar="SEC",
        help="GPS clock offset in seconds (default: auto from clip names)",
    )
    args = parser.parse_args()

    args.use_vt_scale = False
    hw_decode_explicit = args.hw_decode
    apply_profile(args)
    if hw_decode_explicit:
        args.hw_decode = True
        args.use_vt_scale = not args.no_vt_scale
    elif args.no_vt_scale:
        args.use_vt_scale = False

    if args.duration is not None:
        duration = args.duration
    elif args.wall_to is not None:
        duration = (args.wall_to - args.wall_from).total_seconds()
        if duration <= 0:
            parser.error("--to must be after --from")
    else:
        parser.error("One of --to or --duration is required")

    output = args.output or default_output_path(args.video_dir, args.wall_from, duration)

    try:
        run_compose_2cam(
            args.video_dir,
            output,
            wall_start=args.wall_from,
            duration=duration,
            sync_offset_front=args.sync_offset_front,
            sync_offset_back=args.sync_offset_back,
            width=args.width,
            crf=args.crf,
            preset=args.preset,
            fps=args.fps,
            hw=args.hw,
            hw_quality=args.hw_quality,
            hw_decode=args.hw_decode,
            use_vt_scale=args.use_vt_scale,
            audio_source=args.audio,
            telemetry=args.telemetry,
            gps_dir=args.gps_dir,
            telemetry_map_size=args.telemetry_map_size,
            gps_offset_sec=args.gps_offset,
            dry_run=args.dry_run,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
