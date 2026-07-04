#!/usr/bin/env python3
"""Publish 2-cam dashcam video: trip-based chunks → compose → YouTube → delete."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from compose_2cam_70mai import run_compose_2cam
from import_70mai import format_duration, log, parse_datetime
from plan_estimate import (
    DEFAULT_CHUNK_MINUTES,
    DEFAULT_PLAN_FILE,
    DEFAULT_SESSION_GAP,
    ChunkPlan,
    append_plan_file,
    build_plan,
    print_stdout_summary,
    render_markdown,
)
from youtube_upload import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    YouTubeUploadError,
    add_to_playlist,
    ensure_playlist,
    load_state_playlist,
    save_state_playlist,
    upload_video,
)


def escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def concat_videos(parts: list[Path], output: Path, *, ffmpeg: str) -> None:
    if len(parts) == 1:
        shutil.copy2(parts[0], output)
        return

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        for part in parts:
            handle.write(f"file '{escape_concat_path(part)}'\n")
        list_path = Path(handle.name)

    try:
        subprocess.run(
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
                str(output),
            ],
            check=True,
        )
    finally:
        list_path.unlink(missing_ok=True)


def state_path(temp_dir: Path, label: str) -> Path:
    safe = label.replace(" ", "_").replace("/", "-")
    return temp_dir / f"publish_{safe}.state.json"


def load_state(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def chunk_uploaded(state: dict, record_type: str, chunk_index: int) -> bool:
    for part in state.get("parts", []):
        if part.get("record_type") == record_type and part.get("index") == chunk_index:
            return bool(part.get("uploaded"))
    return False


def mark_chunk_state(
    state: dict,
    *,
    record_type: str,
    chunk: ChunkPlan,
    video_id: str | None,
    uploaded: bool,
    output_path: Path | None,
) -> None:
    parts = state.setdefault("parts", [])
    entry = {
        "record_type": record_type,
        "index": chunk.index,
        "duration_sec": chunk.duration_sec,
        "wall_start": chunk.start.isoformat(),
        "trip_indices": [t.index for t in chunk.trips],
        "video_id": video_id,
        "uploaded": uploaded,
        "output_path": str(output_path) if output_path else None,
    }
    replaced = False
    for idx, part in enumerate(parts):
        if part.get("record_type") == record_type and part.get("index") == chunk.index:
            parts[idx] = entry
            replaced = True
            break
    if not replaced:
        parts.append(entry)


def run_estimate(args: argparse.Namespace, ffprobe: str) -> tuple[list, list[ChunkPlan], dict]:
    log(f"Source: {args.source}")
    log(
        f"Mode: {args.chunk_mode}, target {args.chunk_minutes:g} min, "
        f"gap {args.session_gap:g}s"
    )
    return build_plan(
        args.source,
        args.types,
        chunk_minutes=args.chunk_minutes,
        chunk_mode=args.chunk_mode,
        session_gap=args.session_gap,
        ffprobe=ffprobe,
    )


def publish_chunk(
    chunk: ChunkPlan,
    *,
    video_dir: Path,
    temp_dir: Path,
    ffmpeg: str,
    profile_args: dict,
    audio_source: str,
    telemetry: bool = False,
    gps_dir: Path | None = None,
    telemetry_map_size: int = 280,
    dry_run: bool,
) -> Path:
    trip_parts: list[Path] = []
    chunk_dir = temp_dir / f"chunk_{chunk.index:02d}"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    for trip_idx, trip in enumerate(chunk.trips, start=1):
        part_path = chunk_dir / f"trip_{trip_idx:02d}.mp4"
        log(
            f"  Trip {trip.index}: {trip.start:%Y-%m-%d %H:%M:%S} "
            f"({format_duration(trip.duration_sec)})"
        )
        if dry_run:
            trip_parts.append(part_path)
            continue
        run_compose_2cam(
            video_dir,
            part_path,
            wall_start=trip.start,
            duration=trip.duration_sec,
            audio_source=audio_source,
            telemetry=telemetry,
            gps_dir=gps_dir,
            telemetry_map_size=telemetry_map_size,
            dry_run=False,
            **profile_args,
        )
        trip_parts.append(part_path)

    output = temp_dir / f"part_{chunk.index:02d}.mp4"
    if dry_run:
        log(f"  Would concat {len(trip_parts)} trip(s) -> {output.name}")
        return output

    log(f"  Concat {len(trip_parts)} trip(s) -> {output.name}")
    concat_videos(trip_parts, output, ffmpeg=ffmpeg)

    for part in trip_parts:
        part.unlink(missing_ok=True)
    try:
        chunk_dir.rmdir()
    except OSError:
        pass

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish 2-cam video in trip-based chunks to YouTube"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/Volumes/Untitled"),
        help="SD card or clip tree for trip detection",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=Path("video/Output"),
        help="Merged clips for compose (Normal/Front, Normal/Back)",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=["Normal"],
        choices=["Normal", "Event", "Parking"],
    )
    parser.add_argument("--chunk-minutes", type=float, default=DEFAULT_CHUNK_MINUTES)
    parser.add_argument("--chunk-mode", choices=("trips", "fixed"), default="trips")
    parser.add_argument("--session-gap", type=float, default=DEFAULT_SESSION_GAP)
    parser.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_FILE)
    parser.add_argument("--temp-dir", type=Path, default=Path("video/Output/.publish_tmp"))
    parser.add_argument("--title", default="", help="Base YouTube title")
    parser.add_argument("--playlist", default="", help="Playlist title (optional)")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private")
    parser.add_argument("--audio", choices=("front", "back"), default="front")
    parser.add_argument(
        "--telemetry",
        action="store_true",
        help="GPS overlay in compose (map, speed, compass, G-force)",
    )
    parser.add_argument(
        "--gps-dir",
        type=Path,
        help="GPSData*.txt directory (default: --source)",
    )
    parser.add_argument("--telemetry-map-size", type=int, default=280, metavar="PX")
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--hw", action="store_true")
    parser.add_argument("--hw-decode", action="store_true")
    parser.add_argument("--no-vt-scale", action="store_true")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--compose-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--keep", action="store_true", help="Keep local MP4 after upload")
    parser.add_argument(
        "--chunk",
        type=int,
        metavar="N",
        help="Process only chunk index N (1-based, per record type order in plan)",
    )
    parser.add_argument("--check-disk", type=Path, default=Path("."))
    args = parser.parse_args()

    if not args.source.is_dir():
        parser.error(f"Source not found: {args.source}")

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe:
        parser.error("ffprobe not found")
    if not ffmpeg and not args.dry_run and not args.estimate_only:
        parser.error("ffmpeg not found")

    trips, chunks, dur_by_type = run_estimate(args, ffprobe)
    if not chunks:
        log("No chunks to publish.")
        raise SystemExit(1)

    print_stdout_summary(chunks, dur_by_type)

    disk = shutil.disk_usage(args.check_disk)
    md = render_markdown(
        command=" ".join(sys.argv),
        source=args.source,
        chunk_minutes=args.chunk_minutes,
        chunk_mode=args.chunk_mode,
        session_gap=args.session_gap,
        dur_by_type=dur_by_type,
        trips=trips,
        chunks=chunks,
        disk_free_gb=disk.free / (1024**3),
    )
    append_plan_file(args.plan_file, md)
    log(f"Plan appended: {args.plan_file}")

    if args.estimate_only:
        return

    from compose_70mai import apply_profile

    ns = argparse.Namespace(
        profile=args.profile,
        hw=args.hw,
        hw_quality=65,
        width=1206,
        crf=20,
        preset="medium",
        fps=25,
        hw_decode=args.hw_decode,
        use_vt_scale=False,
        no_vt_scale=args.no_vt_scale,
    )
    apply_profile(ns)
    if args.hw_decode:
        ns.hw_decode = True
        ns.use_vt_scale = not args.no_vt_scale

    profile_args = dict(
        width=ns.width,
        crf=ns.crf,
        preset=ns.preset,
        fps=ns.fps,
        hw=ns.hw,
        hw_quality=ns.hw_quality,
        hw_decode=ns.hw_decode,
        use_vt_scale=ns.use_vt_scale,
    )

    label = "_".join(args.types)
    st_path = state_path(args.temp_dir, label)
    state = load_state(st_path) if args.resume else {}
    if args.resume and state.get("chunk_minutes") not in (None, args.chunk_minutes):
        log(
            f"Warning: state chunk_minutes={state.get('chunk_minutes')} "
            f"!= current {args.chunk_minutes}"
        )

    state.update(
        {
            "source": str(args.source),
            "types": args.types,
            "chunk_minutes": args.chunk_minutes,
            "chunk_mode": args.chunk_mode,
        }
    )

    base_title = args.title or f"70mai {datetime.now():%Y-%m-%d}"
    total_by_type: dict[str, int] = {}
    for chunk in chunks:
        total_by_type[chunk.record_type] = total_by_type.get(chunk.record_type, 0) + 1

    playlist_id = load_state_playlist(st_path) if args.resume else None

    for chunk in chunks:
        record_type = chunk.record_type
        total = total_by_type[record_type]
        if args.chunk is not None and chunk.index != args.chunk:
            continue
        log("")
        log(
            f"=== Chunk {chunk.index}/{total} [{record_type}] "
            f"{format_duration(chunk.duration_sec)} | {chunk.trip_labels} ==="
        )

        if args.resume and chunk_uploaded(state, record_type, chunk.index):
            log("  Skip (already uploaded per state)")
            continue

        output = publish_chunk(
            chunk,
            video_dir=args.video_dir,
            temp_dir=args.temp_dir,
            ffmpeg=ffmpeg or "ffmpeg",
            profile_args=profile_args,
            audio_source=args.audio,
            telemetry=args.telemetry,
            gps_dir=args.gps_dir or args.source,
            telemetry_map_size=args.telemetry_map_size,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            continue

        video_id = None
        uploaded = False

        if not args.compose_only:
            part_title = f"{base_title} {record_type} — часть {chunk.index}/{total}"
            try:
                log(f"  Uploading to YouTube: {part_title}")

                def progress(pct: int) -> None:
                    if pct % 10 == 0:
                        log(f"    upload {pct}%")

                video_id = upload_video(
                    output,
                    title=part_title,
                    privacy=args.privacy,
                    credentials_path=args.credentials,
                    token_path=args.token,
                    on_progress=progress,
                )
                uploaded = True
                log(f"  Uploaded: https://youtu.be/{video_id}")

                if args.playlist or args.title:
                    if not playlist_id:
                        pl_title = args.playlist or f"{base_title} {record_type}"
                        playlist_id = ensure_playlist(
                            pl_title,
                            credentials_path=args.credentials,
                            token_path=args.token,
                        )
                        save_state_playlist(st_path, playlist_id)
                        log(f"  Playlist created: {pl_title}")
                    add_to_playlist(
                        playlist_id,
                        video_id,
                        credentials_path=args.credentials,
                        token_path=args.token,
                    )
            except YouTubeUploadError as exc:
                log(f"  Upload failed: {exc}")
                mark_chunk_state(
                    state,
                    record_type=record_type,
                    chunk=chunk,
                    video_id=None,
                    uploaded=False,
                    output_path=output,
                )
                save_state(st_path, state)
                raise SystemExit(1) from exc
        else:
            uploaded = False
            log(f"  Compose-only: {output}")

        mark_chunk_state(
            state,
            record_type=record_type,
            chunk=chunk,
            video_id=video_id,
            uploaded=uploaded,
            output_path=output if args.compose_only or args.keep else None,
        )
        save_state(st_path, state)

        if uploaded and not args.keep:
            output.unlink(missing_ok=True)
            log("  Deleted local chunk after upload")
        elif args.compose_only and not args.keep:
            log(f"  Kept: {output}")

    log("\nDone.")


if __name__ == "__main__":
    main()
