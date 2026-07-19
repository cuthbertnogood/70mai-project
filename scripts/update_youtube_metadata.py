#!/usr/bin/env python3
"""Update title, description, and comment on already-uploaded YouTube videos."""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LIB = _ROOT / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from import_70mai import log
from plan_estimate import build_plan, load_autopilot_plan
from publish_state import StateStore
from youtube_metadata import build_youtube_metadata
from youtube_upload import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    YouTubeUploadError,
    apply_youtube_metadata,
    ensure_youtube_oauth_for_upload,
)


def _load_chunks(source: Path, record_types: list[str], temp_dir: Path, ffprobe: str):
    cached = load_autopilot_plan(temp_dir)
    if cached:
        return cached
    _, chunks, _ = build_plan(
        source,
        record_types,
        chunk_minutes=120.0,
        chunk_mode="trips",
        session_gap=120.0,
        ffprobe=ffprobe,
    )
    return chunks


def _trip_window(chunks, record_type: str, chunk_index: int, trip_index: int):
    for chunk in chunks:
        if chunk.record_type != record_type or chunk.index != chunk_index:
            continue
        for trip in chunk.trips:
            if trip.index == trip_index:
                return trip.start, trip.end
    return None, None


def _part_window(part: dict, chunks):
    record_type = str(part.get("record_type") or "")
    index = int(part.get("index") or 0)
    for chunk in chunks:
        if chunk.record_type == record_type and chunk.index == index:
            return chunk.start, chunk.end
    wall_raw = part.get("wall_start")
    dur = float(part.get("duration_sec") or 0.0)
    if wall_raw and dur > 0:
        start = datetime.fromisoformat(str(wall_raw))
        return start, start + timedelta(seconds=dur)
    return None, None


def _iter_uploaded_jobs(state: dict, record_types: list[str], video_id_filter: str | None):
    seen: set[str] = set()
    for part in state.get("parts") or []:
        record_type = str(part.get("record_type") or "")
        if record_types and record_type not in record_types:
            continue
        vid = str(part.get("video_id") or "").strip()
        if not part.get("uploaded") or not vid:
            continue
        if video_id_filter and vid != video_id_filter:
            continue
        if vid in seen:
            continue
        seen.add(vid)
        yield {
            "video_id": vid,
            "record_type": record_type,
            "kind": "chunk",
            "chunk_index": int(part.get("index") or 0),
            "trip_index": None,
            "part": part,
        }

    for part in state.get("trip_parts") or []:
        record_type = str(part.get("record_type") or "")
        if record_types and record_type not in record_types:
            continue
        vid = str(part.get("video_id") or "").strip()
        if not part.get("uploaded") or not vid:
            continue
        if video_id_filter and vid != video_id_filter:
            continue
        if vid in seen:
            continue
        seen.add(vid)
        yield {
            "video_id": vid,
            "record_type": record_type,
            "kind": "trip",
            "chunk_index": int(part.get("chunk_index") or 0),
            "trip_index": int(part.get("trip_index") or 0),
            "part": part,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update YouTube title/description/comment for uploaded videos"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/Volumes/Untitled"),
        help="SD card path (clip fallback + state on SD)",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=Path("video/Output"),
        help="Merged clips directory",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=Path("video/Output/.publish_tmp"),
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=["Normal", "Event", "Parking"],
        choices=["Normal", "Event", "Parking"],
    )
    parser.add_argument("--video-id", help="Update only this YouTube video id")
    parser.add_argument(
        "--record-type",
        choices=["Normal", "Event", "Parking"],
        help="With --video-id: record type for clip lookup",
    )
    parser.add_argument("--title", default="", help="Base title prefix (default: 70mai YYYY-MM-DD)")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument(
        "--state-on-sd",
        action="store_true",
        help="Read publish state from SD card",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default: dry-run only)",
    )
    parser.add_argument(
        "--skip-comment",
        action="store_true",
        help="Update title/description only",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-apply even if metadata_updated_at is set",
    )
    args = parser.parse_args(argv)

    ffprobe = shutil.which("ffprobe") or "ffprobe"
    chunks = _load_chunks(args.source, args.types, args.temp_dir, ffprobe)

    jobs: list[dict] = []
    if args.video_id:
        record_type = args.record_type
        if not record_type:
            parser.error("--record-type is required with --video-id")
        jobs.append(
            {
                "video_id": args.video_id.strip(),
                "record_type": record_type,
                "kind": "manual",
                "chunk_index": 1,
                "trip_index": 1,
                "part": {},
            }
        )
    else:
        for record_type in args.types:
            store = StateStore(
                args.source,
                args.temp_dir,
                record_type,
                state_on_sd=args.state_on_sd,
            )
            state = store.load(resume=True, quiet=True)
            jobs.extend(
                _iter_uploaded_jobs(state, [record_type], args.video_id)
            )

    if not jobs:
        log("No uploaded videos found in publish state.")
        return 1

    oauth_checked = False
    updated = 0
    for job in jobs:
        part = job["part"]
        if part.get("metadata_updated_at") and not args.force:
            log(
                f"Skip {job['video_id']}: metadata already updated "
                f"({part.get('metadata_updated_at')}) — use --force"
            )
            continue

        if job["kind"] == "manual":
            wall_start = wall_end = None
            for chunk in chunks:
                if chunk.record_type == job["record_type"]:
                    break
        elif job["kind"] == "chunk":
            if job["record_type"] in ("Event", "Parking"):
                wall_start = wall_end = None
            else:
                wall_start, wall_end = _part_window(part, chunks)
        else:
            wall_start, wall_end = _trip_window(
                chunks,
                job["record_type"],
                job["chunk_index"],
                job["trip_index"],
            )

        base_title = args.title
        if not base_title and wall_start is not None:
            base_title = f"70mai {wall_start:%Y-%m-%d}"
        elif not base_title:
            base_title = f"70mai {datetime.now():%Y-%m-%d}"

        title, body, comment, clip_ranges = build_youtube_metadata(
            base_title=base_title,
            record_type=job["record_type"],
            video_dir=args.video_dir,
            wall_start=wall_start,
            wall_end=wall_end,
            source=args.source,
            ffprobe=ffprobe,
        )

        log("")
        log(f"=== {job['video_id']} [{job['record_type']}] ===")
        log(f"  Title: {title}")
        log(f"  Clips: {len(clip_ranges)}")
        preview = body.splitlines()[:4]
        for line in preview:
            log(f"  {line}")
        if len(body.splitlines()) > 4:
            log("  …")

        if not args.apply:
            continue

        if not oauth_checked:
            ok, detail = ensure_youtube_oauth_for_upload(
                args.credentials,
                args.token,
                interactive=True,
                auto_reauth=True,
            )
            oauth_checked = True
            if not ok:
                log(f"YouTube OAuth not ready: {detail}")
                log(
                    "  Запустите в интерактивном терминале — откроется браузер для входа."
                )
                return 1

        try:
            apply_youtube_metadata(
                job["video_id"],
                title=title,
                description=body,
                post_comment=not args.skip_comment,
                credentials_path=args.credentials,
                token_path=args.token,
            )
        except YouTubeUploadError as exc:
            log(f"  Failed: {exc}")
            continue
        updated += 1

    if not args.apply:
        log("")
        log("Dry-run only — re-run with --apply to update YouTube.")
        return 0

    log("")
    log(f"Updated {updated} video(s).")
    return 0 if updated else 1


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    raise SystemExit(main())
