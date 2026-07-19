#!/usr/bin/env python3
"""Publish 2-cam dashcam video: trip-based chunks → compose → YouTube → delete."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from compose_2cam_70mai import run_compose_2cam
from compose_70mai import probe_duration
from import_70mai import format_duration, log, parse_datetime
from publish_paths import compose_chunk_dir, compose_part_path, compose_trip_path
from plan_estimate import (
    DEFAULT_CHUNK_MINUTES,
    DEFAULT_PLAN_FILE,
    DEFAULT_SESSION_GAP,
    SINGLE_VIDEO_TYPES,
    ChunkPlan,
    append_plan_file,
    build_plan,
    print_stdout_summary,
    render_markdown,
)
from publish_state import AuthStore, StateStore, youtube_watch_url
from project_env import cli_python
from youtube_metadata import build_youtube_metadata
from youtube_upload import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    OAuthReauthRequired,
    UploadProgressReporter,
    YouTubeUploadError,
    add_to_playlist,
    clear_upload_session,
    ensure_playlist,
    format_file_size,
    log_oauth_reauth_help,
    oauth_needs_reauth,
    post_video_comment,
    upload_session_path_for_file,
    upload_video,
)
from youtube_upload_diagnostics import DEFAULT_DIAG_LOG


@dataclass
class UploadSummary:
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)


MERGED_PRUNE_MARGIN = timedelta(seconds=120)  # < session gap, > clip length


class UploadPipeline:
    """Single-worker background uploader: compose N+1 overlaps upload N."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending: Future | None = None
        self.state_lock = threading.Lock()

    def wait(self) -> None:
        """Block until the in-flight upload (if any) finishes; re-raise its errors."""
        if self.pending is not None:
            fut, self.pending = self.pending, None
            fut.result()

    def submit(self, fn) -> None:
        self.wait()
        self.pending = self.pool.submit(fn)

    def shutdown(self) -> None:
        try:
            self.wait()
        finally:
            self.pool.shutdown(wait=True)


def free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except OSError:
        return float("inf")


def prune_merged_for_trip(
    video_dir: Path,
    record_type: str,
    trip_start: datetime,
    trip_end: datetime,
) -> int:
    """Delete merged source files fully inside the trip range. Returns freed bytes.

    Safe: source clips stay on the SD card, so merged files can be rebuilt
    by rerunning import.
    """
    from compose_70mai import scan_merged_clips

    lo = trip_start - MERGED_PRUNE_MARGIN
    hi = trip_end + MERGED_PRUNE_MARGIN
    freed = 0
    count = 0
    for camera in ("Front", "Back"):
        for clip in scan_merged_clips(
            video_dir, camera, record_type=record_type, probe=False
        ):
            if clip.start >= lo and clip.end <= hi:
                try:
                    size = clip.path.stat().st_size
                    clip.path.unlink()
                except OSError:
                    continue
                try:
                    from clip_timeline import manifest_path_for

                    manifest_path_for(clip.path).unlink(missing_ok=True)
                except Exception:
                    pass
                freed += size
                count += 1
    if freed:
        log(
            f"  Pruned {count} merged source file(s): freed {format_file_size(freed)}"
        )
    return freed


def prune_uploaded_trips(
    state: dict,
    chunks: list,
    video_dir: Path,
) -> int:
    """Delete merged files for every trip already marked uploaded in state."""
    total = 0
    for chunk in chunks:
        for trip_idx, trip in enumerate(chunk.trips, start=1):
            if not trip_uploaded(state, chunk.record_type, chunk.index, trip_idx):
                continue
            freed = prune_merged_for_trip(
                video_dir, chunk.record_type, trip.start, trip.end
            )
            if freed:
                log(
                    f"  Pruned uploaded [{chunk.record_type}] "
                    f"chunk {chunk.index} trip {trip_idx}: "
                    f"freed {format_file_size(freed)}"
                )
            total += freed
    return total


def guard_free_disk(
    check_path: Path,
    min_free_gb: float,
    pipeline: UploadPipeline | None,
    *,
    state: dict | None = None,
    chunks: list | None = None,
    video_dir: Path | None = None,
    prune_merged: str = "off",
) -> None:
    """Before compose: enforce disk reserve; wait/upload-prune; hard-fail if still low."""
    if min_free_gb <= 0:
        return
    free = free_disk_gb(check_path)
    if free >= min_free_gb:
        return
    if pipeline is not None and pipeline.pending is not None:
        log(
            f"  Disk low ({free:.1f} GB < {min_free_gb:g} GB reserve) — "
            "waiting for background upload to free space"
        )
        pipeline.wait()
        free = free_disk_gb(check_path)
    if free < min_free_gb and prune_merged != "off" and state and chunks and video_dir:
        log(
            f"  Disk low ({free:.1f} GB) — pruning merged files for uploaded trips"
        )
        prune_uploaded_trips(state, chunks, video_dir)
        free = free_disk_gb(check_path)
    if free < min_free_gb:
        raise RuntimeError(
            f"Disk free {free:.1f} GB < {min_free_gb:g} GB reserve — "
            "cannot compose. Free space or lower --min-free-gb."
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


def trip_uploaded(
    state: dict, record_type: str, chunk_index: int, trip_index: int
) -> bool:
    for part in state.get("trip_parts", []):
        if (
            part.get("record_type") == record_type
            and part.get("chunk_index") == chunk_index
            and part.get("trip_index") == trip_index
        ):
            return bool(part.get("uploaded"))
    return False


def get_trip_state(
    state: dict, record_type: str, chunk_index: int, trip_index: int
) -> dict | None:
    for part in state.get("trip_parts", []):
        if (
            part.get("record_type") == record_type
            and part.get("chunk_index") == chunk_index
            and part.get("trip_index") == trip_index
        ):
            return part
    return None


def get_chunk_state(
    state: dict, record_type: str, chunk_index: int
) -> dict | None:
    for part in state.get("parts", []):
        if part.get("record_type") == record_type and part.get("index") == chunk_index:
            return part
    return None


def is_row_uploaded(
    state: dict, record_type: str, chunk_index: int, trip_index: int
) -> bool:
    """True when trip or whole chunk (Event/Parking) is uploaded."""
    return trip_uploaded(
        state, record_type, chunk_index, trip_index
    ) or chunk_uploaded(state, record_type, chunk_index)


def get_upload_entry(
    state: dict, record_type: str, chunk_index: int, trip_index: int
) -> dict | None:
    trip = get_trip_state(state, record_type, chunk_index, trip_index)
    if trip and trip.get("uploaded"):
        return trip
    chunk = get_chunk_state(state, record_type, chunk_index)
    if chunk and chunk.get("uploaded"):
        return chunk
    return trip or chunk


def parse_mark_uploaded(value: str) -> tuple[int, int, str]:
    parts = value.split(":", 2)
    if len(parts) != 3:
        raise ValueError(
            f"Invalid --mark-uploaded {value!r}; expected CHUNK:TRIP:VIDEO_ID"
        )
    chunk_index, trip_index, video_id = int(parts[0]), int(parts[1]), parts[2].strip()
    if not video_id:
        raise ValueError(f"Empty video_id in --mark-uploaded {value!r}")
    return chunk_index, trip_index, video_id


def apply_mark_uploaded(
    state: dict,
    marks: list[str],
    *,
    record_type: str,
) -> None:
    for mark in marks:
        chunk_index, trip_index, video_id = parse_mark_uploaded(mark)
        mark_trip_state(
            state,
            record_type=record_type,
            chunk_index=chunk_index,
            trip_index=trip_index,
            video_id=video_id,
            uploaded=True,
            output_path=None,
        )
        log(
            f"Marked uploaded: chunk {chunk_index} trip {trip_index} "
            f"-> https://youtu.be/{video_id}"
        )


def build_trip_tasks(
    chunks: list[ChunkPlan],
    *,
    chunk_filter: int | None,
    trip_filter: int | None,
) -> list[tuple[ChunkPlan, int, object, str]]:
    tasks: list[tuple[ChunkPlan, int, object, str]] = []
    for chunk in chunks:
        if chunk_filter is not None and chunk.index != chunk_filter:
            continue
        for trip_idx, trip in enumerate(chunk.trips, start=1):
            if trip_filter is not None and trip_idx != trip_filter:
                continue
            tasks.append((chunk, trip_idx, trip, chunk.record_type))
    return tasks


def cleanup_uploaded_file(
    path: Path,
    session_path: Path,
    *,
    keep: bool,
) -> int:
    if keep or not path.is_file():
        return 0
    size = path.stat().st_size
    path.unlink(missing_ok=True)
    clear_upload_session(session_path)
    rel = path.name
    if path.parent.name.startswith("chunk_"):
        rel = f"{path.parent.name}/{path.name}"
    log(f"  Deleted: {rel} (freed {format_file_size(size)})")
    return size


def upload_and_cleanup(
    path: Path,
    title: str,
    *,
    description: str = "",
    comment: str | None = None,
    post_comment: bool = True,
    privacy: str,
    credentials: Path,
    token: Path,
    session_dir: Path,
    resume_upload: bool,
    diag_log: Path | None,
    keep: bool,
    playlist_id: str | None,
    playlist_title: str,
    upload_chunk_bytes: int | None = None,
    status_hook: Callable[[int, int, int], None] | None = None,
) -> tuple[str | None, str | None, int, float]:
    """Upload, add to playlist, delete local file. Returns (video_id, playlist_id, freed_bytes, elapsed_sec)."""
    if not path.is_file():
        raise YouTubeUploadError(f"Video not found: {path}")

    file_size = path.stat().st_size
    session_file = upload_session_path_for_file(path, session_dir)
    reporter = UploadProgressReporter(path.name, file_size)
    started = time.monotonic()

    def progress(pct: int, offset: int = 0, size: int = 0) -> None:
        reporter.update(pct, offset or None)
        if status_hook is not None:
            status_hook(pct, offset, size or file_size)

    log(f"  Uploading to YouTube: {title}")
    video_id = upload_video(
        path,
        title=title,
        description=description,
        privacy=privacy,
        credentials_path=credentials,
        token_path=token,
        session_path=session_file,
        resume=resume_upload,
        diag_log=diag_log,
        on_progress=progress,
        chunk_bytes=upload_chunk_bytes,
    )
    reporter.finish()
    elapsed = time.monotonic() - started

    if post_comment:
        comment_text = comment if comment is not None else description
        if comment_text:
            try:
                post_video_comment(
                    video_id,
                    comment_text,
                    credentials_path=credentials,
                    token_path=token,
                )
                log("  YouTube comment posted")
            except YouTubeUploadError as exc:
                log(f"  Warning: YouTube comment skipped ({exc})")

    current_playlist = playlist_id
    if current_playlist or playlist_title:
        try:
            if not current_playlist:
                current_playlist = ensure_playlist(
                    playlist_title,
                    credentials_path=credentials,
                    token_path=token,
                )
                log(f"  Playlist created: {playlist_title}")
            add_to_playlist(
                current_playlist,
                video_id,
                credentials_path=credentials,
                token_path=token,
            )
        except Exception as exc:
            log(f"  Warning: playlist skipped ({exc})")

    log(
        f"  Uploaded: https://youtu.be/{video_id} "
        f"({format_file_size(file_size)}, {format_duration(elapsed)})"
    )
    freed = cleanup_uploaded_file(path, session_file, keep=keep)
    return video_id, current_playlist, freed, elapsed


def print_upload_summary(summary: UploadSummary) -> None:
    log("")
    log("=== Upload summary ===")
    log(f"  Uploaded: {summary.uploaded}")
    log(f"  Skipped:  {summary.skipped}")
    log(f"  Failed:   {summary.failed}")
    if summary.freed_bytes:
        log(f"  Freed:    {format_file_size(summary.freed_bytes)}")
    for err in summary.errors:
        log(f"  Error: {err}")


def mark_trip_state(
    state: dict,
    *,
    record_type: str,
    chunk_index: int,
    trip_index: int,
    video_id: str | None,
    uploaded: bool,
    output_path: Path | None,
) -> None:
    parts = state.setdefault("trip_parts", [])
    entry = {
        "record_type": record_type,
        "chunk_index": chunk_index,
        "trip_index": trip_index,
        "video_id": video_id,
        "youtube_url": youtube_watch_url(video_id),
        "uploaded": uploaded,
        "output_path": str(output_path) if output_path else None,
    }
    replaced = False
    for idx, part in enumerate(parts):
        if (
            part.get("record_type") == record_type
            and part.get("chunk_index") == chunk_index
            and part.get("trip_index") == trip_index
        ):
            parts[idx] = entry
            replaced = True
            break
    if not replaced:
        parts.append(entry)


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
        "youtube_url": youtube_watch_url(video_id),
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


def trip_part_complete(
    path: Path,
    expected_sec: float,
    *,
    tolerance: float = 0.9,
    video_dir: Path | None = None,
    record_type: str = "",
    trip_start=None,
) -> bool:
    if not path.is_file() or path.stat().st_size < 1_000_000:
        return False
    try:
        actual = probe_duration(path)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return False
    if actual < expected_sec * tolerance:
        return False
    if video_dir is not None and trip_start is not None:
        from compose_70mai import plan_segments, scan_merged_clips

        try:
            front = scan_merged_clips(
                video_dir, "Front", record_type=record_type, probe=True
            )
            back = scan_merged_clips(
                video_dir, "Back", record_type=record_type, probe=True
            )
            # Slot-aligned compose fills gaps with black, so per-camera
            # plan_segments coverage is expected to be short — the part-file
            # duration check above already validates the timeline length.
            if front and back and _both_merges_have_manifest(front, back):
                return True
            if record_type not in SINGLE_VIDEO_TYPES:
                return False
            for camera, clips in (("Front", front), ("Back", back)):
                segs = plan_segments(clips, trip_start, expected_sec, 0.0)
                covered = sum(s.duration for s in segs)
                if covered < expected_sec * 0.98:
                    return False
        except (ValueError, OSError, RuntimeError):
            return False
    return True


def _both_merges_have_manifest(front_clips: list, back_clips: list) -> bool:
    try:
        from clip_timeline import manifest_matches_merge

        for clips in (front_clips, back_clips):
            if not clips:
                return False
            for clip in clips:
                if not manifest_matches_merge(clip.path):
                    return False
        return True
    except Exception:
        return False


def _compose_duration_for_trip(
    video_dir: Path,
    record_type: str,
    trip,
    *,
    repair: str = "auto",
) -> float:
    """Pick compose duration; cap to actual merges when repair cannot rebuild."""
    from pipeline_repair import capped_compose_duration

    if record_type not in SINGLE_VIDEO_TYPES or repair == "off":
        return trip.duration_sec
    from compose_70mai import scan_merged_clips

    front = scan_merged_clips(video_dir, "Front", record_type=record_type, probe=True)
    back = scan_merged_clips(video_dir, "Back", record_type=record_type, probe=True)
    # Slot-aligned compose (both merges carry a timeline manifest) computes the
    # true union-slot length itself and black-fills gaps — never cap it down.
    if front and back and _both_merges_have_manifest(front, back):
        return trip.duration_sec
    front_dur = front[0].duration if front else None
    back_dur = back[0].duration if back else None
    capped = capped_compose_duration(trip.duration_sec, front_dur, back_dur)
    if capped + 0.5 < trip.duration_sec:
        log(
            f"  [repair] capped compose duration "
            f"{trip.duration_sec:.0f}→{capped:.0f}s "
            f"(merges shorter than plan)"
        )
    return capped


def _precompose_repair(
    *,
    source: Path | None,
    video_dir: Path,
    temp_dir: Path,
    chunk: ChunkPlan,
    repair: str,
) -> None:
    """Diagnose short merges; delete + one import when SD is present.

    If import is impossible, leave files for duration-cap fallback (do not delete).
    """
    if repair == "off":
        return
    from pipeline_repair import diagnose_and_repair, diagnose_chunk

    issues = diagnose_chunk(
        source, video_dir, chunk, import_store=None, uploaded=False
    )
    blockers = [i for i in issues if i.severity == "blocker"]
    if not blockers:
        return
    for issue in blockers:
        log(f"  [repair] {issue.code}: {issue.message}")
    if repair == "diagnose":
        return

    can_reimport = bool(source and source.is_dir())
    if not can_reimport:
        log(
            "  [repair] blockers found but SD source unavailable — "
            "will cap compose duration instead of deleting merges"
        )
        return

    ok, _issues, actions = diagnose_and_repair(
        source,
        video_dir,
        chunk,
        temp_dir=temp_dir,
        import_store=None,
        uploaded=False,
        mode="auto",
    )
    if ok:
        return
    log(
        f"  [repair] pre-compose: {len(actions)} action(s) — "
        "reimport then compose"
    )
    python = cli_python()
    import_cmd = [
        python,
        "lib/import_70mai.py",
        "--source",
        str(source),
        "--types",
        chunk.record_type,
        "--output",
        str(video_dir),
        "--state-on-sd",
        "--skip-inventory-refresh",
    ]
    log(f"  [repair] retry import: {' '.join(import_cmd)}")
    result = subprocess.run(import_cmd, cwd=Path.cwd())
    if result.returncode != 0:
        log(
            f"  [repair] import exit {result.returncode} — "
            "compose will use capped duration if merges still short"
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
    trip_only: int | None = None,
    repair: str = "auto",
    source: Path | None = None,
) -> Path:
    trip_parts: list[Path] = []
    chunk_dir = compose_chunk_dir(temp_dir, chunk.record_type, chunk.index)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        _precompose_repair(
            source=source,
            video_dir=video_dir,
            temp_dir=temp_dir,
            chunk=chunk,
            repair=repair,
        )

    for trip_idx, trip in enumerate(chunk.trips, start=1):
        if trip_only is not None and trip_idx != trip_only:
            continue
        part_path = compose_trip_path(
            temp_dir, chunk.record_type, chunk.index, trip_idx
        )
        log(
            f"  Trip {trip.index}: {trip.start:%Y-%m-%d %H:%M:%S} "
            f"({format_duration(trip.duration_sec)})"
        )
        if dry_run:
            trip_parts.append(part_path)
            continue
        compose_dur = _compose_duration_for_trip(
            video_dir, chunk.record_type, trip, repair=repair
        )
        if trip_part_complete(
            part_path,
            compose_dur,
            video_dir=video_dir,
            record_type=chunk.record_type,
            trip_start=trip.start,
        ):
            log(f"  Skip compose (exists, {format_duration(probe_duration(part_path))})")
            trip_parts.append(part_path)
            continue
        run_compose_2cam(
            video_dir,
            part_path,
            wall_start=trip.start,
            duration=compose_dur,
            wall_end=trip.end,
            audio_source=audio_source,
            telemetry=telemetry,
            gps_dir=gps_dir,
            telemetry_map_size=telemetry_map_size,
            record_type=chunk.record_type,
            dry_run=False,
            **profile_args,
        )
        trip_parts.append(part_path)

    if trip_only is not None:
        if not trip_parts:
            raise ValueError(f"Trip {trip_only} not in chunk {chunk.index}")
        return trip_parts[0]

    output = compose_part_path(temp_dir, chunk.record_type, chunk.index)
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


def sync_card_youtube_inventory(
    publish_state: dict,
    state_store: StateStore,
    *,
    source: Path,
    types: list[str],
    session_gap: float,
    trips: list,
    chunks: list,
    dur_by_type: dict[str, float],
    label: str,
    temp_dir: Path,
) -> None:
    """Update SD card_inventory with per-clip YouTube URLs after upload."""
    if not state_store.state_on_sd:
        return
    from import_state import ImportStateStore

    store = ImportStateStore(
        source,
        label,
        state_on_sd=True,
        local_dir=temp_dir,
        chunk_minutes=10.0,
        gap_seconds=session_gap,
    )
    store.sync_youtube_links(
        types=types,
        trips=trips,
        chunks=chunks,
        dur_by_type=dur_by_type,
        publish_state=publish_state,
    )


def publish_and_upload_trips(
    chunk: ChunkPlan,
    *,
    video_dir: Path,
    temp_dir: Path,
    ffmpeg: str,
    profile_args: dict,
    audio_source: str,
    telemetry: bool,
    gps_dir: Path | None,
    telemetry_map_size: int,
    dry_run: bool,
    trip_only: int | None,
    base_title: str,
    record_type: str,
    privacy: str,
    credentials: Path,
    token: Path,
    resume_upload: bool,
    compose_only: bool,
    upload_only: bool,
    keep: bool,
    continue_on_error: bool,
    playlist_id: str | None,
    playlist_title: str,
    state: dict,
    state_store: StateStore,
    diag_log: Path | None,
    summary: UploadSummary,
    queue_ctx: tuple[int, int, int, int] | None = None,
    youtube_sync: dict | None = None,
    upload_chunk_bytes: int | None = None,
    pipeline: UploadPipeline | None = None,
    playlist_holder: dict | None = None,
    prune_merged: str = "after-compose",
    min_free_gb: float = 20.0,
    check_disk: Path = Path("."),
    all_chunks: list | None = None,
    repair: str = "auto",
    source: Path | None = None,
) -> tuple[str | None, str | None]:
    """Compose and upload each trip separately; returns (last_video_id, playlist_id).

    With a pipeline, the upload of trip N runs in the background while trip N+1
    composes — wall time becomes max(encode, upload) instead of the sum.
    """
    from autopilot_dashboard import clear_trip_reason, write_status

    if not dry_run and not upload_only:
        _precompose_repair(
            source=source,
            video_dir=video_dir,
            temp_dir=temp_dir,
            chunk=chunk,
            repair=repair,
        )

    chunk_dir = compose_chunk_dir(temp_dir, record_type, chunk.index)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    last_video_id = None
    if playlist_holder is None:
        playlist_holder = {"id": playlist_id}
    state_lock = pipeline.state_lock if pipeline else threading.Lock()
    chunks_for_prune = all_chunks or [chunk]

    for trip_idx, trip in enumerate(chunk.trips, start=1):
        if trip_only is not None and trip_idx != trip_only:
            continue

        if queue_ctx:
            overall_i, overall_total, chunk_trip_i, chunk_trip_total = queue_ctx
            log(
                f"=== Upload queue: trip {chunk_trip_i}/{chunk_trip_total} "
                f"in chunk {chunk.index} (overall {overall_i}/{overall_total}) ==="
            )

        if trip_uploaded(state, record_type, chunk.index, trip_idx):
            entry = get_trip_state(state, record_type, chunk.index, trip_idx)
            vid = entry.get("video_id") if entry else None
            if vid:
                log(f"  Trip {trip.index}: skip (already uploaded): https://youtu.be/{vid}")
            else:
                log(f"  Trip {trip.index}: skip (already uploaded per state)")
            if prune_merged != "off":
                prune_merged_for_trip(video_dir, record_type, trip.start, trip.end)
            write_status(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
                phase="done",
                detail=f"youtu.be/{vid}" if vid else "uploaded",
                youtube_url=youtube_watch_url(vid) if vid else None,
            )
            summary.skipped += 1
            continue

        part_path = compose_trip_path(temp_dir, record_type, chunk.index, trip_idx)
        log(
            f"  Trip {trip.index}: {trip.start:%Y-%m-%d %H:%M:%S} "
            f"({format_duration(trip.duration_sec)})"
        )

        if dry_run:
            if part_path.is_file():
                log(f"  Would upload {part_path.name} ({format_file_size(part_path.stat().st_size)})")
            else:
                log(f"  Would upload {part_path.name} (missing — skip in dry-run)")
            continue

        if upload_only:
            if not part_path.is_file():
                log(f"  Warning: missing {part_path} — skip")
                summary.skipped += 1
                continue
            log(f"  Upload-only: {part_path.name} ({format_file_size(part_path.stat().st_size)})")
        elif not trip_part_complete(
            part_path,
            trip.duration_sec,
            video_dir=video_dir,
            record_type=record_type,
            trip_start=trip.start,
        ):
            compose_dur = _compose_duration_for_trip(
                video_dir, record_type, trip, repair=repair
            )
            guard_free_disk(
                check_disk,
                min_free_gb,
                pipeline,
                state=state,
                chunks=chunks_for_prune,
                video_dir=video_dir,
                prune_merged=prune_merged,
            )
            clear_trip_reason(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
            )
            write_status(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
                phase="compose",
                detail=(
                    f"Front↑+Back↓ → trip_{trip_idx:02d}.mp4 "
                    f"({format_duration(compose_dur)})"
                ),
                reason="",
            )
            try:
                run_compose_2cam(
                    video_dir,
                    part_path,
                    wall_start=trip.start,
                    duration=compose_dur,
                    wall_end=trip.end,
                    audio_source=audio_source,
                    telemetry=telemetry,
                    gps_dir=gps_dir,
                    telemetry_map_size=telemetry_map_size,
                    record_type=record_type,
                    dry_run=False,
                    **profile_args,
                )
            except subprocess.CalledProcessError as exc:
                reason = f"ffmpeg exit {exc.returncode}"
                write_status(
                    temp_dir,
                    record_type=record_type,
                    chunk_index=chunk.index,
                    trip_index=trip_idx,
                    phase="fail",
                    detail=reason,
                    reason=reason,
                )
                raise
            except RuntimeError as exc:
                reason = str(exc)[:120]
                write_status(
                    temp_dir,
                    record_type=record_type,
                    chunk_index=chunk.index,
                    trip_index=trip_idx,
                    phase="fail",
                    detail=reason[:80],
                    reason=reason,
                )
                raise
            if prune_merged == "after-compose":
                prune_merged_for_trip(video_dir, record_type, trip.start, trip.end)
        else:
            log(f"  Skip compose (exists, {format_duration(probe_duration(part_path))})")

        if compose_only:
            log(f"  Compose-only: {part_path}")
            mark_trip_state(
                state,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
                video_id=None,
                uploaded=False,
                output_path=part_path,
            )
            state_store.save(state)
            continue

        trip_title, trip_description, trip_comment, clip_ranges = build_youtube_metadata(
            base_title=base_title,
            record_type=record_type,
            video_dir=video_dir,
            wall_start=None if record_type in SINGLE_VIDEO_TYPES else trip.start,
            wall_end=None if record_type in SINGLE_VIDEO_TYPES else trip.end,
            source=source,
        )
        log(
            f"  YouTube: {trip_title} "
            f"({len(clip_ranges)} clip(s) in description/comment)"
        )

        def do_upload(
            part_path: Path = part_path,
            trip=trip,
            trip_idx: int = trip_idx,
            trip_title: str = trip_title,
            trip_description: str = trip_description,
        ) -> None:
            nonlocal last_video_id
            write_status(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
                phase="upload",
                detail=part_path.name,
            )

            def status_hook(pct: int, offset: int, size: int) -> None:
                elapsed = time.monotonic() - upload_t0
                rate = offset / elapsed if elapsed > 0 and offset > 0 else 0.0
                remaining = max(0, size - offset)
                eta_sec = remaining / rate if rate > 0 else 0.0
                speed_mb = rate / (1024 * 1024)
                detail = (
                    f"{format_file_size(offset)}/{format_file_size(size)}"
                    if size
                    else part_path.name
                )
                if speed_mb > 0:
                    detail = f"{detail} · {speed_mb:.1f} MB/s"
                write_status(
                    temp_dir,
                    record_type=record_type,
                    chunk_index=chunk.index,
                    trip_index=trip_idx,
                    phase="upload",
                    detail=detail,
                    percent=float(pct),
                    output_bytes=offset,
                    speed=speed_mb if speed_mb > 0 else None,
                    speed_unit="MB/s" if speed_mb > 0 else None,
                    eta=format_duration(eta_sec) if rate > 0 else None,
                    elapsed=format_duration(elapsed),
                )

            upload_t0 = time.monotonic()
            try:
                video_id, new_playlist, freed, _elapsed = upload_and_cleanup(
                    part_path,
                    trip_title,
                    description=trip_description,
                    comment=trip_comment,
                    privacy=privacy,
                    credentials=credentials,
                    token=token,
                    session_dir=state_store.session_dir,
                    resume_upload=resume_upload,
                    diag_log=diag_log,
                    keep=keep,
                    playlist_id=playlist_holder["id"],
                    playlist_title=playlist_title,
                    upload_chunk_bytes=upload_chunk_bytes,
                    status_hook=status_hook,
                )
            except YouTubeUploadError as exc:
                if isinstance(exc, OAuthReauthRequired) or oauth_needs_reauth(exc):
                    log_oauth_reauth_help(
                        token_path=token,
                        credentials_path=credentials,
                        reason=str(exc),
                    )
                    write_status(
                        temp_dir,
                        record_type=record_type,
                        chunk_index=chunk.index,
                        trip_index=trip_idx,
                        phase="oauth",
                        detail="YouTube OAuth",
                        reason="oauth: invalid_grant — нужен повторный вход (см. лог)",
                    )
                msg = f"chunk {chunk.index} trip {trip_idx}: {exc}"
                log(f"  Upload failed: {exc}")
                if not isinstance(exc, OAuthReauthRequired) and not oauth_needs_reauth(exc):
                    write_status(
                        temp_dir,
                        record_type=record_type,
                        chunk_index=chunk.index,
                        trip_index=trip_idx,
                        phase="fail",
                        detail=str(exc)[:80],
                        reason=f"upload: {str(exc)[:100]}",
                    )
                if diag_log:
                    log(f"  Diagnostics: {diag_log}")
                    log(f"  Analyze: {cli_python()} scripts/analyze_youtube_upload.py")
                with state_lock:
                    mark_trip_state(
                        state,
                        record_type=record_type,
                        chunk_index=chunk.index,
                        trip_index=trip_idx,
                        video_id=None,
                        uploaded=False,
                        output_path=part_path,
                    )
                    state_store.save(state)
                    summary.failed += 1
                    summary.errors.append(msg)
                if continue_on_error:
                    return
                raise SystemExit(1) from exc

            with state_lock:
                playlist_holder["id"] = new_playlist
                last_video_id = video_id
                mark_trip_state(
                    state,
                    record_type=record_type,
                    chunk_index=chunk.index,
                    trip_index=trip_idx,
                    video_id=video_id,
                    uploaded=True,
                    output_path=part_path if keep else None,
                )
                if new_playlist:
                    state["playlist_id"] = new_playlist
                state_store.save(state)
                if youtube_sync:
                    sync_card_youtube_inventory(state, state_store, **youtube_sync)
                summary.uploaded += 1
                summary.freed_bytes += freed
            clear_trip_reason(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
            )
            write_status(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk.index,
                trip_index=trip_idx,
                phase="done",
                detail=f"youtu.be/{video_id}",
                youtube_url=youtube_watch_url(video_id),
                reason="",
            )
            if prune_merged == "after-upload":
                prune_merged_for_trip(video_dir, record_type, trip.start, trip.end)

        if pipeline is not None:
            pipeline.submit(do_upload)
        else:
            do_upload()

    return last_video_id, playlist_holder["id"]


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
        help="GPS overlay in compose (disabled — backlog; see GOALS.md)",
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
    parser.add_argument(
        "--codec",
        choices=("h264", "hevc"),
        default=None,
        help="HW encoder codec (default: from profile)",
    )
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
    parser.add_argument(
        "--trip",
        type=int,
        metavar="N",
        help="Within chunk: compose/upload only trip N (1-based order in chunk)",
    )
    parser.add_argument(
        "--per-trip-upload",
        action="store_true",
        help="Upload each trip as separate YouTube video (no concat)",
    )
    parser.add_argument(
        "--resume-upload",
        action="store_true",
        help="Resume YouTube upload from saved session URI (.upload.json)",
    )
    parser.add_argument(
        "--upload-only",
        action="store_true",
        help="Skip compose; upload existing MP4 from temp-dir/chunk_NN/trip_NN.mp4",
    )
    parser.add_argument(
        "--mark-uploaded",
        action="append",
        metavar="CHUNK:TRIP:VIDEO_ID",
        help="Mark trip as already uploaded in state (repeatable)",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="On upload failure, log and continue to next trip",
    )
    parser.add_argument(
        "--upload-chunk-mb",
        type=int,
        default=None,
        metavar="MB",
        help="YouTube upload chunk in MB (default: 256; 0 = whole file in one PUT)",
    )
    parser.add_argument(
        "--prune-merged",
        choices=("off", "after-compose", "after-upload"),
        default="after-compose",
        help=(
            "Delete merged source files once used: after-compose (default, min disk) "
            "or after-upload. Sources stay on SD."
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
        "--no-overlap",
        action="store_true",
        help="Disable compose/upload overlap (sequential like before)",
    )
    parser.add_argument(
        "--diag-log",
        type=Path,
        default=DEFAULT_DIAG_LOG,
        help="Append YouTube upload diagnostics (JSONL)",
    )
    parser.add_argument(
        "--no-diag",
        action="store_true",
        help="Disable YouTube upload diagnostic logging",
    )
    parser.add_argument(
        "--no-state-on-sd",
        action="store_true",
        help="Do not read/write publish state on SD card (.70mai/publish/)",
    )
    parser.add_argument(
        "--state-on-sd",
        action="store_true",
        help="Store upload progress on SD card (portable across hosts; enables auto-resume)",
    )
    parser.add_argument(
        "--no-auth-on-sd",
        action="store_true",
        help="Keep YouTube OAuth only on host (~/.config/70mai/)",
    )
    parser.add_argument(
        "--auth-on-sd",
        action="store_true",
        help="Store YouTube OAuth on SD card (.70mai/auth/; portable across hosts)",
    )
    parser.add_argument("--check-disk", type=Path, default=Path("."))
    parser.add_argument(
        "--repair",
        choices=("auto", "diagnose", "off"),
        default="auto",
        help=(
            "Self-heal short/stale Parking/Event merges before compose "
            "(default: auto)"
        ),
    )
    args = parser.parse_args()
    from telemetry_overlay import telemetry_requested

    args.telemetry = telemetry_requested(args.telemetry)

    if args.upload_only:
        args.per_trip_upload = True
        if not args.resume_upload:
            args.resume_upload = True

    if args.upload_only and args.compose_only:
        parser.error("--upload-only and --compose-only are mutually exclusive")

    state_on_sd = args.state_on_sd and not args.no_state_on_sd
    if state_on_sd:
        args.resume = True
        if not args.resume_upload:
            args.resume_upload = True

    auth_on_sd = args.auth_on_sd and not args.no_auth_on_sd

    if not args.source.is_dir():
        parser.error(f"Source not found: {args.source}")

    if auth_on_sd:
        try:
            args.credentials, args.token = AuthStore.resolve(
                args.source, auth_on_sd=True
            )
        except FileNotFoundError as exc:
            parser.error(str(exc))
        except RuntimeError as exc:
            parser.error(str(exc))

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe:
        parser.error("ffprobe not found")
    if not ffmpeg and not args.dry_run and not args.estimate_only and not args.upload_only:
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
        codec="h264",
    )
    apply_profile(ns)
    if args.hw_decode:
        ns.hw_decode = True
        ns.use_vt_scale = not args.no_vt_scale
    if args.codec:
        ns.codec = args.codec

    profile_args = dict(
        width=ns.width,
        crf=ns.crf,
        preset=ns.preset,
        fps=ns.fps,
        hw=ns.hw,
        hw_quality=ns.hw_quality,
        hw_decode=ns.hw_decode,
        use_vt_scale=ns.use_vt_scale,
        codec=ns.codec,
    )

    label = "_".join(args.types)
    state_store = StateStore(
        args.source, args.temp_dir, label, state_on_sd=state_on_sd
    )
    state = state_store.load(resume=args.resume)
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

    playlist_id = state.get("playlist_id") if args.resume else None
    diag_log = None if args.no_diag else args.diag_log
    summary = UploadSummary()
    upload_chunk_bytes = (
        None if args.upload_chunk_mb is None else args.upload_chunk_mb * 1024 * 1024
    )
    overlap_enabled = not (
        args.no_overlap or args.dry_run or args.compose_only or args.estimate_only
    )

    youtube_sync = None
    if state_on_sd and not args.dry_run:
        youtube_sync = {
            "source": args.source,
            "types": args.types,
            "session_gap": args.session_gap,
            "trips": trips,
            "chunks": chunks,
            "dur_by_type": dur_by_type,
            "label": label,
            "temp_dir": args.temp_dir,
        }

    record_type_for_marks = args.types[0] if len(args.types) == 1 else None
    if args.mark_uploaded:
        if record_type_for_marks is None:
            parser.error("--mark-uploaded requires a single --types value")
        apply_mark_uploaded(state, args.mark_uploaded, record_type=record_type_for_marks)
        state_store.save(state)

    if args.per_trip_upload:
        trip_tasks = build_trip_tasks(
            chunks, chunk_filter=args.chunk, trip_filter=args.trip
        )
        if not trip_tasks:
            log("No trips to upload.")
            raise SystemExit(1)

        pipeline = UploadPipeline() if overlap_enabled else None
        playlist_holder = {"id": playlist_id}
        if pipeline is not None:
            log("Pipeline: compose of the next trip overlaps the current upload")
        if args.prune_merged != "off" and not args.dry_run:
            prune_uploaded_trips(state, chunks, args.video_dir)

        try:
            last_chunk_key: tuple[int, str] | None = None
            for overall_i, (chunk, trip_idx, _trip, record_type) in enumerate(
                trip_tasks, start=1
            ):
                chunk_key = (chunk.index, record_type)
                if chunk_key != last_chunk_key:
                    total = total_by_type[record_type]
                    log("")
                    log(
                        f"=== Chunk {chunk.index}/{total} [{record_type}] "
                        f"{format_duration(chunk.duration_sec)} | {chunk.trip_labels} ==="
                    )
                    last_chunk_key = chunk_key

                pl_title = args.playlist or ""
                publish_and_upload_trips(
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
                    trip_only=trip_idx,
                    base_title=base_title,
                    record_type=record_type,
                    privacy=args.privacy,
                    credentials=args.credentials,
                    token=args.token,
                    resume_upload=args.resume_upload,
                    compose_only=args.compose_only,
                    upload_only=args.upload_only,
                    keep=args.keep,
                    continue_on_error=args.continue_on_error,
                    playlist_id=playlist_holder["id"],
                    playlist_title=pl_title,
                    state=state,
                    state_store=state_store,
                    diag_log=diag_log,
                    summary=summary,
                    queue_ctx=(overall_i, len(trip_tasks), trip_idx, len(chunk.trips)),
                    youtube_sync=youtube_sync,
                    upload_chunk_bytes=upload_chunk_bytes,
                    pipeline=pipeline,
                    playlist_holder=playlist_holder,
                    prune_merged=args.prune_merged,
                    min_free_gb=args.min_free_gb,
                    check_disk=args.check_disk,
                    all_chunks=chunks,
                    repair=args.repair,
                    source=args.source,
                )
        finally:
            if pipeline is not None:
                pipeline.shutdown()

        print_upload_summary(summary)
        if summary.failed:
            raise SystemExit(1)
        log("\nDone.")
        return

    pipeline = UploadPipeline() if overlap_enabled else None
    playlist_holder = {"id": playlist_id}
    state_lock = pipeline.state_lock if pipeline else threading.Lock()
    if pipeline is not None:
        log("Pipeline: compose of the next chunk overlaps the current upload")
    if args.prune_merged != "off" and not args.dry_run:
        prune_uploaded_trips(state, chunks, args.video_dir)

    try:
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

            pl_title = args.playlist or ""

            if not args.dry_run:
                guard_free_disk(
                    args.check_disk,
                    args.min_free_gb,
                    pipeline,
                    state=state,
                    chunks=chunks,
                    video_dir=args.video_dir,
                    prune_merged=args.prune_merged,
                )
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
                trip_only=args.trip,
                repair=args.repair,
                source=args.source,
            )

            if args.dry_run:
                continue

            if args.prune_merged == "after-compose":
                prune_merged_for_trip(
                    args.video_dir, record_type, chunk.start, chunk.end
                )

            if args.compose_only:
                log(f"  Compose-only: {output}")
                if chunk_uploaded(state, record_type, chunk.index):
                    # Local rebuild for verification — do not reset the
                    # already-uploaded chunk's state or drop its video_id.
                    log(
                        "  Compose-only: chunk already uploaded — "
                        "state left unchanged"
                    )
                else:
                    mark_chunk_state(
                        state,
                        record_type=record_type,
                        chunk=chunk,
                        video_id=None,
                        uploaded=False,
                        output_path=output,
                    )
                    state_store.save(state)
                if youtube_sync:
                    sync_card_youtube_inventory(state, state_store, **youtube_sync)
                if not args.keep:
                    log(f"  Kept: {output}")
                continue

            part_title, part_description, part_comment, clip_ranges = build_youtube_metadata(
                base_title=base_title,
                record_type=record_type,
                video_dir=args.video_dir,
                wall_start=None if record_type in SINGLE_VIDEO_TYPES else chunk.start,
                wall_end=None if record_type in SINGLE_VIDEO_TYPES else chunk.end,
                source=args.source,
            )
            log(
                f"  YouTube: {part_title} "
                f"({len(clip_ranges)} clip(s) in description/comment)"
            )

            def do_upload_chunk(
                chunk=chunk,
                output=output,
                record_type=record_type,
                part_title=part_title,
                part_description=part_description,
                pl_title=pl_title,
            ) -> None:
                try:
                    video_id, new_playlist, freed, _elapsed = upload_and_cleanup(
                        output,
                        part_title,
                        description=part_description,
                        comment=part_comment,
                        privacy=args.privacy,
                        credentials=args.credentials,
                        token=args.token,
                        session_dir=state_store.session_dir,
                        resume_upload=args.resume_upload,
                        diag_log=diag_log,
                        keep=args.keep,
                        playlist_id=playlist_holder["id"],
                        playlist_title=pl_title,
                        upload_chunk_bytes=upload_chunk_bytes,
                    )
                except YouTubeUploadError as exc:
                    log(f"  Upload failed: {exc}")
                    if diag_log:
                        log(f"  Diagnostics: {diag_log}")
                        log(f"  Analyze: {cli_python()} scripts/analyze_youtube_upload.py")
                    with state_lock:
                        mark_chunk_state(
                            state,
                            record_type=record_type,
                            chunk=chunk,
                            video_id=None,
                            uploaded=False,
                            output_path=output,
                        )
                        state_store.save(state)
                        summary.failed += 1
                        summary.errors.append(f"chunk {chunk.index}: {exc}")
                    if args.continue_on_error:
                        return
                    raise SystemExit(1) from exc

                with state_lock:
                    playlist_holder["id"] = new_playlist
                    if new_playlist:
                        state["playlist_id"] = new_playlist
                    mark_chunk_state(
                        state,
                        record_type=record_type,
                        chunk=chunk,
                        video_id=video_id,
                        uploaded=True,
                        output_path=output if args.keep else None,
                    )
                    state_store.save(state)
                    if youtube_sync:
                        sync_card_youtube_inventory(state, state_store, **youtube_sync)
                    summary.uploaded += 1
                    summary.freed_bytes += freed
                if args.prune_merged == "after-upload":
                    prune_merged_for_trip(
                        args.video_dir, record_type, chunk.start, chunk.end
                    )

            if pipeline is not None:
                pipeline.submit(do_upload_chunk)
            else:
                do_upload_chunk()
    finally:
        if pipeline is not None:
            pipeline.shutdown()

    print_upload_summary(summary)
    if summary.failed:
        raise SystemExit(1)
    log("\nDone.")


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
