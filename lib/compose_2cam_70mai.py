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
    probe_duration,
    resolve_codec,
    run_ffmpeg_with_progress,
    scan_merged_clips,
)
from import_70mai import format_duration, parse_datetime
from telemetry_overlay import render_telemetry_video, resolve_gps_sources, telemetry_requested

from clip_timeline import (
    Span,
    build_camera_lane,
    build_slots,
    lane_black_seconds,
    lane_duration,
    load_manifest,
    max_contiguous_black,
    merges_timeline_ready,
    pair_drift_report,
    timeline_duration,
)


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
    codec: str = "h264",
    audio_source: str,
    telemetry_path: Path | None = None,
) -> list[str]:
    cmd: list[str] = ["ffmpeg", "-y"]

    for seg in front_segments:
        if hw_decode:
            append_hwaccel_args(cmd, keep_hw_frames=use_vt_scale)
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])

    for seg in back_segments:
        if hw_decode:
            append_hwaccel_args(cmd, keep_hw_frames=use_vt_scale)
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
        codec=codec,
    )
    cmd.append(str(output))
    return cmd


SINGLE_VIDEO_TYPES = ("Event", "Parking")


def probe_stream_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    raw = result.stdout.strip().split("x")
    if len(raw) < 2:
        return None
    try:
        return int(raw[0]), int(raw[1])
    except ValueError:
        return None


def _even(value: float) -> int:
    n = int(round(value))
    return n - (n % 2)


def _scaled_height(width: int, path: Path, fallback: int) -> int:
    dims = probe_stream_dimensions(path)
    if not dims or dims[0] <= 0:
        return fallback
    src_w, src_h = dims
    return max(2, _even(width * src_h / src_w))


def build_aligned_lanes(
    video_dir: Path,
    *,
    wall_start: datetime,
    duration: float,
    record_type: str,
    sync_offset_front: float = 0.0,
    sync_offset_back: float = 0.0,
) -> tuple[list[Span], list[Span], float, Path, Path, dict] | None:
    """Build equal-length Front/Back lanes from timeline manifests.

    Returns None when manifests are unavailable (caller falls back to the
    legacy independent-stack path).
    """
    front_clips = scan_merged_clips(
        video_dir, "Front", record_type=record_type, probe=False
    )
    back_clips = scan_merged_clips(
        video_dir, "Back", record_type=record_type, probe=False
    )
    if not front_clips or not back_clips:
        return None

    def _entries(clips: list) -> list | None:
        combined: list = []
        for clip in clips:
            manifest = load_manifest(clip.path)
            if manifest is None:
                return None
            combined.extend(manifest.clips)
        combined.sort(key=lambda e: (e.wall, e.key))
        return combined

    front_entries = _entries(front_clips)
    back_entries = _entries(back_clips)
    if not front_entries or not back_entries:
        return None

    mode = "slot" if record_type in SINGLE_VIDEO_TYPES else "wall"
    slots = build_slots(
        front_entries,
        back_entries,
        mode=mode,
        timeline_start=wall_start if mode == "wall" else None,
    )
    if not slots:
        return None

    if mode == "wall":
        total = max(duration, timeline_duration(slots))
    else:
        total = timeline_duration(slots)

    front_lane = build_camera_lane(slots, "Front", total_duration=total)
    back_lane = build_camera_lane(slots, "Back", total_duration=total)

    front_lane = _apply_offset(front_lane, sync_offset_front)
    back_lane = _apply_offset(back_lane, sync_offset_back)

    report = {
        **pair_drift_report(slots),
        "front_black": lane_black_seconds(front_lane),
        "back_black": lane_black_seconds(back_lane),
        "front_max_black": max_contiguous_black(front_lane),
        "back_max_black": max_contiguous_black(back_lane),
    }
    return (
        front_lane,
        back_lane,
        total,
        front_clips[0].path,
        back_clips[0].path,
        report,
    )


def _apply_offset(lane: list[Span], offset: float) -> list[Span]:
    if abs(offset) < 1e-6:
        return lane
    out: list[Span] = []
    for span in lane:
        if span.kind == "video":
            out.append(
                Span(
                    kind="video",
                    output_start=span.output_start,
                    duration=span.duration,
                    merge=span.merge,
                    source_ss=max(0.0, span.source_ss + offset),
                )
            )
        else:
            out.append(span)
    return out


def _merge_path_map(video_dir: Path, record_type: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for camera in ("Front", "Back"):
        for clip in scan_merged_clips(
            video_dir, camera, record_type=record_type, probe=False
        ):
            paths[clip.path.name] = clip.path
    return paths


def build_compose_2cam_aligned_cmd(
    front_lane: list[Span],
    back_lane: list[Span],
    output: Path,
    *,
    merge_paths: dict[str, Path],
    width: int,
    front_height: int,
    back_height: int,
    crf: int,
    preset: str,
    fps: int,
    hw: bool,
    hw_quality: int,
    hw_decode: bool,
    codec: str = "h264",
    audio_source: str,
    total_duration: float,
) -> list[str]:
    cmd: list[str] = ["ffmpeg", "-y"]
    input_index: dict[int, int] = {}  # id(span) -> ffmpeg input index
    idx = 0

    def add_video_inputs(lane: list[Span]) -> None:
        nonlocal idx
        for span in lane:
            if span.kind != "video":
                continue
            path = merge_paths.get(span.merge or "")
            if path is None:
                raise ValueError(f"merge file not found for span: {span.merge}")
            if hw_decode:
                append_hwaccel_args(cmd, keep_hw_frames=False)
            cmd.extend(
                ["-ss", f"{span.source_ss:.3f}", "-t", f"{span.duration:.3f}",
                 "-i", str(path)]
            )
            input_index[id(span)] = idx
            idx += 1

    add_video_inputs(front_lane)
    add_video_inputs(back_lane)

    parts: list[str] = []

    def build_lane(lane: list[Span], height: int, prefix: str) -> str:
        piece_labels: list[str] = []
        for i, span in enumerate(lane):
            label = f"{prefix}{i}"
            if span.kind == "video":
                in_idx = input_index[id(span)]
                parts.append(
                    f"[{in_idx}:v]scale={width}:{height},setsar=1,"
                    f"fps={fps},format=yuv420p[{label}]"
                )
            else:
                parts.append(
                    f"color=c=black:s={width}x{height}:r={fps}:"
                    f"d={span.duration:.3f},setsar=1,format=yuv420p[{label}]"
                )
            piece_labels.append(f"[{label}]")
        lane_out = f"{prefix}lane"
        if len(piece_labels) == 1:
            parts.append(f"{piece_labels[0]}null[{lane_out}]")
        else:
            parts.append(
                "".join(piece_labels)
                + f"concat=n={len(piece_labels)}:v=1:a=0[{lane_out}]"
            )
        return lane_out

    front_out = build_lane(front_lane, front_height, "fp")
    back_out = build_lane(back_lane, back_height, "bp")
    parts.append(f"[{front_out}][{back_out}]vstack=inputs=2[vout]")

    audio_lane = front_lane if audio_source != "back" else back_lane
    audio_labels: list[str] = []
    for i, span in enumerate(audio_lane):
        label = f"ap{i}"
        if span.kind == "video":
            in_idx = input_index[id(span)]
            parts.append(
                f"[{in_idx}:a]aformat=sample_rates=44100:"
                f"channel_layouts=stereo,asetpts=PTS-STARTPTS[{label}]"
            )
        else:
            parts.append(
                f"anullsrc=r=44100:cl=stereo:d={span.duration:.3f}[{label}]"
            )
        audio_labels.append(f"[{label}]")
    if len(audio_labels) == 1:
        parts.append(f"{audio_labels[0]}anull[aout]")
    else:
        parts.append(
            "".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[aout]"
        )

    filter_complex = ";".join(parts)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", "[vout]", "-map", "[aout]"])
    append_video_encode_args(
        cmd,
        hw=hw,
        crf=crf,
        preset=preset,
        hw_quality=hw_quality,
        fps=fps,
        codec=codec,
    )
    cmd.extend(["-t", f"{total_duration:.3f}", str(output)])
    return cmd


def _run_aligned_compose(
    aligned: tuple[list[Span], list[Span], float, Path, Path, dict],
    output: Path,
    *,
    video_dir: Path,
    record_type: str,
    width: int,
    crf: int,
    preset: str,
    fps: int,
    hw: bool,
    hw_quality: int,
    hw_decode: bool,
    codec: str,
    audio_source: str,
    dry_run: bool,
) -> None:
    front_lane, back_lane, total, front_path, back_path, report = aligned
    merge_paths = _merge_path_map(video_dir, record_type)
    front_height = _scaled_height(width, front_path, fallback=_even(width * 9 / 16))
    back_height = _scaled_height(width, back_path, fallback=_even(width * 9 / 16))

    log("Aligned timeline compose (Front/Back slot-synced)")
    log(f"Duration:      {total:g} sec ({format_duration(total)})")
    log(
        f"Slots:         {report['slots']} "
        f"(missing front={report['missing_front']}, "
        f"back={report['missing_back']}, "
        f"max pair spread={report['max_pair_spread']:.2f}s)"
    )
    log(
        f"Black fill:    front={report['front_black']:.1f}s "
        f"(max {report['front_max_black']:.1f}s), "
        f"back={report['back_black']:.1f}s "
        f"(max {report['back_max_black']:.1f}s)"
    )
    log(f"Front lane:    {len(front_lane)} span(s), {lane_duration(front_lane):g}s")
    log(f"Back lane:     {len(back_lane)} span(s), {lane_duration(back_lane):g}s")
    log(f"Audio:         {audio_source}")

    if hw and hw_decode:
        attempts = [(True, "hw decode + CPU scale"), (False, "hw encode only")]
    elif hw:
        attempts = [(False, "hw encode only")]
    else:
        attempts = [(False, "software encode")]

    last_error: subprocess.CalledProcessError | None = None
    for attempt_hw_decode, label in attempts:
        cmd = build_compose_2cam_aligned_cmd(
            front_lane,
            back_lane,
            output,
            merge_paths=merge_paths,
            width=width,
            front_height=front_height,
            back_height=back_height,
            crf=crf,
            preset=preset,
            fps=fps,
            hw=hw,
            hw_quality=hw_quality,
            hw_decode=attempt_hw_decode,
            codec=codec,
            audio_source=audio_source,
            total_duration=total,
        )
        log("")
        log(f"Attempt: {label}")
        log("Command:")
        log(" ".join(f'"{a}"' if " " in a else a for a in cmd))
        if dry_run:
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_ffmpeg_with_progress(cmd, duration_sec=total, output_path=output)
            if label != attempts[0][1]:
                log(f"\nNote: fell back to {label}")
            _verify_output_duration(output, total)
            log(f"\nDone: {output}")
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if output.is_file():
                output.unlink()
            if label != attempts[-1][1]:
                log(f"\n{label} failed (exit {exc.returncode}), trying fallback...")
            else:
                raise
    if last_error is not None:
        raise last_error


def _verify_output_duration(output: Path, expected: float) -> None:
    try:
        actual = probe_duration(output)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return
    drift = abs(actual - expected)
    if drift > 1.0:
        log(
            f"  [sync] warning: output {actual:.1f}s vs target {expected:.1f}s "
            f"(drift {drift:.1f}s)"
        )
    else:
        log(f"  [sync] output duration OK: {actual:.1f}s (target {expected:.1f}s)")


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
    codec: str = "h264",
    audio_source: str = "front",
    telemetry: bool = False,
    gps_dir: Path | None = None,
    telemetry_map_size: int = 280,
    gps_offset_sec: float | None = None,
    record_type: str = "Normal",
    dry_run: bool = False,
) -> None:
    codec, hw_quality = resolve_codec(codec, hw_quality, hw=hw)
    wall_end = wall_start + timedelta(seconds=duration)

    if telemetry:
        raise SystemExit(
            "2-cam compose with --telemetry is not supported for slot-synced "
            "Front/Back output. Run without --telemetry."
        )

    aligned = build_aligned_lanes(
        video_dir,
        wall_start=wall_start,
        duration=duration,
        record_type=record_type,
        sync_offset_front=sync_offset_front,
        sync_offset_back=sync_offset_back,
    )
    if aligned is None:
        _, detail = merges_timeline_ready(video_dir, record_type)
        raise SystemExit(
            f"Cannot compose {record_type}: Front/Back timeline manifests are "
            f"required for slot-synced 2-cam output ({detail}). Re-import merges "
            f"(import writes <merge>.timeline.json sidecars)."
        )

    _run_aligned_compose(
        aligned,
        output,
        video_dir=video_dir,
        record_type=record_type,
        width=width,
        crf=crf,
        preset=preset,
        fps=fps,
        hw=hw,
        hw_quality=hw_quality,
        hw_decode=hw_decode,
        codec=codec,
        audio_source=audio_source,
        dry_run=dry_run,
    )
    return

    front_clips = scan_merged_clips(video_dir, "Front", record_type=record_type)
    back_clips = scan_merged_clips(video_dir, "Back", record_type=record_type)
    if not front_clips:
        raise SystemExit(
            f"No Front merged clips in {video_dir / record_type / 'Front'}"
        )
    if not back_clips:
        raise SystemExit(
            f"No Back merged clips in {video_dir / record_type / 'Back'}"
        )

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
        f"{'hevc' if codec == 'hevc' else 'h264'}_videotoolbox ({hw_quality * 100}k)"
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
        # Fastest pipeline first, degrade gracefully on failure.
        attempts: list[tuple[bool, bool, str]] = []
        if use_vt_scale:
            attempts.append((True, True, "full VT (hw decode + scale_vt)"))
        attempts.append((True, False, "hw decode + CPU scale"))
        attempts.append((False, False, "hw encode only"))
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
            codec=codec,
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
            run_ffmpeg_with_progress(cmd, duration_sec=duration, output_path=output)
            if label != attempts[0][2]:
                log(f"\nNote: fell back to {label}")
            log(f"\nDone: {output}")
            if telemetry_tmp is not None:
                telemetry_tmp.cleanup()
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if output.is_file():
                output.unlink()
            if label != attempts[-1][2]:
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
    parser.add_argument(
        "--codec",
        choices=("h264", "hevc"),
        default=None,
        help="HW encoder codec (default: from profile)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--telemetry",
        action="store_true",
        help="GPS overlay (disabled — backlog; see GOALS.md)",
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
    codec_explicit = args.codec
    apply_profile(args)
    if hw_decode_explicit:
        args.hw_decode = True
        args.use_vt_scale = not args.no_vt_scale
    elif args.no_vt_scale:
        args.use_vt_scale = False
    if codec_explicit:
        args.codec = codec_explicit

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
            codec=args.codec,
            audio_source=args.audio,
            telemetry=telemetry_requested(args.telemetry),
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
