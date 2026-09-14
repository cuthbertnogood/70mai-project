#!/usr/bin/env python3
"""Per-source-file block map for Autopilot web Dashboard."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from import_70mai import (
    bad_clips_log_path,
    format_file_size,
    parse_datetime,
    scan_clips,
    sd_bad_clips_log_path,
)
from import_state import sd_import_dir, sd_inventory_path
from plan_estimate import DEFAULT_SESSION_GAP, SINGLE_VIDEO_TYPES, load_autopilot_plan

DEFAULT_CLIP_SEC = 60.0
_CACHE_TTL_SEC = 45.0
STATUS_ORDER = ("error", "uploaded", "composed", "merged", "planned", "oncard")
_DONE_MERGE = frozenset({"merged", "skipped"})
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_file_map_cache() -> None:
    _cache.clear()


def _load_inventory(source: Path) -> dict[str, Any] | None:
    path = sd_inventory_path(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clip_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _bad_clip_names(source: Path | None, temp_dir: Path | None) -> set[str]:
    names: set[str] = set()
    paths: list[Path] = []
    if temp_dir is not None:
        paths.append(bad_clips_log_path(temp_dir))
    if source is not None:
        paths.append(sd_bad_clips_log_path(source))
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                raw = entry.get("path")
                if raw:
                    name = Path(str(raw)).name
            if name:
                names.add(str(name))
    return names


def _is_uploaded_meta(info: dict[str, Any]) -> bool:
    return bool(info.get("youtube_url") or info.get("video_id"))


def _uploaded_from_clip_youtube(inv: dict[str, Any]) -> set[tuple[str, str, str]]:
    uploaded: set[tuple[str, str, str]] = set()
    for record_type, block in (inv.get("record_types") or {}).items():
        if not isinstance(block, dict):
            continue
        for camera, clips in (block.get("clip_youtube") or {}).items():
            if not isinstance(clips, dict):
                continue
            for name, info in clips.items():
                if isinstance(info, dict) and _is_uploaded_meta(info):
                    uploaded.add((str(record_type), str(camera), str(name)))
    return uploaded


def _uploaded_from_inventory_trips(
    source: Path,
    types: list[str],
    inv: dict[str, Any],
) -> set[tuple[str, str, str]]:
    uploaded: set[tuple[str, str, str]] = set()
    for record_type in types:
        block = (inv.get("record_types") or {}).get(record_type)
        if not isinstance(block, dict):
            continue
        trips = block.get("trips") or []
        if not isinstance(trips, list):
            continue
        uploaded_trips = [
            t for t in trips if isinstance(t, dict) and _is_uploaded_meta(t)
        ]
        if not uploaded_trips:
            continue
        clips = scan_clips(source, [record_type], ["Front", "Back"], warn=False)
        if record_type in SINGLE_VIDEO_TYPES:
            for clip in clips:
                uploaded.add((record_type, clip.camera, clip.path.name))
            continue
        for trip in uploaded_trips:
            try:
                start = parse_datetime(str(trip.get("start") or ""))
                end = parse_datetime(str(trip.get("end") or ""))
            except ValueError:
                continue
            for clip in clips:
                if start <= clip.timestamp <= end:
                    uploaded.add((record_type, clip.camera, clip.path.name))
    return uploaded


def _uploaded_from_publish_state(
    source: Path,
    types: list[str],
    temp_dir: Path | None,
) -> set[tuple[str, str, str]]:
    if temp_dir is None:
        return set()
    chunks = load_autopilot_plan(temp_dir)
    if not chunks:
        return set()
    try:
        from publish_state import (
            build_clip_youtube_catalog,
            load_state_file,
            merge_publish_state,
            sd_state_path,
        )
    except Exception:
        return set()

    uploaded: set[tuple[str, str, str]] = set()
    for record_type in types:
        rt_chunks = [c for c in chunks if c.record_type == record_type]
        if not rt_chunks:
            continue
        try:
            merged = merge_publish_state(
                load_state_file(sd_state_path(source, record_type)),
                load_state_file(temp_dir / f"publish_{record_type}.state.json"),
            )
            catalog = build_clip_youtube_catalog(
                source,
                [record_type],
                session_gap=DEFAULT_SESSION_GAP,
                publish_state=merged,
                chunks=rt_chunks,
            )
        except Exception:
            continue
        for camera, clips in (catalog.get(record_type) or {}).items():
            if not isinstance(clips, dict):
                continue
            for name, info in clips.items():
                if isinstance(info, dict) and _is_uploaded_meta(info):
                    uploaded.add((record_type, str(camera), str(name)))
    return uploaded


def _uploaded_from_dashboard_rows(
    temp_dir: Path | None,
    video_dir: Path | None,
    source: Path,
    types: list[str],
) -> set[tuple[str, str, str]]:
    if temp_dir is None:
        return set()
    try:
        from autopilot_dashboard import Dashboard
    except Exception:
        return set()
    try:
        dash = Dashboard(
            temp_dir=temp_dir,
            video_dir=video_dir or Path("video/Output"),
            check_disk=Path("."),
            min_free_gb=20.0,
            source=source,
            types=types,
            enabled=False,
        )
        dash.reload_plan_if_changed()
        try:
            dash._refresh_from_publish_state()
            dash._refresh_from_status()
        except Exception:
            pass
    except Exception:
        return set()

    uploaded: set[tuple[str, str, str]] = set()
    for row in dash.rows:
        if row.status != "done" or not row.youtube_url:
            continue
        if row.trip_start is None or row.trip_end is None:
            continue
        clips = scan_clips(source, [row.record_type], ["Front", "Back"], warn=False)
        if row.record_type in SINGLE_VIDEO_TYPES:
            for clip in clips:
                uploaded.add((row.record_type, clip.camera, clip.path.name))
            continue
        for clip in clips:
            if row.trip_start <= clip.timestamp <= row.trip_end:
                uploaded.add((row.record_type, clip.camera, clip.path.name))
    return uploaded


def _uploaded_sources(
    source: Path,
    types: list[str],
    *,
    temp_dir: Path | None = None,
    video_dir: Path | None = None,
) -> set[tuple[str, str, str]]:
    inv = _load_inventory(source) or {}
    uploaded = _uploaded_from_clip_youtube(inv)
    uploaded |= _uploaded_from_inventory_trips(source, types, inv)
    uploaded |= _uploaded_from_publish_state(source, types, temp_dir)
    uploaded |= _uploaded_from_dashboard_rows(temp_dir, video_dir, source, types)
    return uploaded


def _merged_sources(video_dir: Path | None) -> set[tuple[str, str, str]]:
    merged: set[tuple[str, str, str]] = set()
    if video_dir is None or not video_dir.is_dir():
        return merged
    try:
        paths = video_dir.rglob("*.timeline.json")
    except OSError:
        return merged
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        record_type = str(data.get("record_type") or "")
        camera = str(data.get("camera") or "")
        for raw in data.get("clips") or []:
            if not isinstance(raw, dict):
                continue
            src = raw.get("src")
            if src:
                merged.add((record_type, camera, str(src)))
    return merged


def _merge_window(record_type: str, filename: str) -> tuple[datetime, datetime] | None:
    from compose_70mai import parse_event_export_file, parse_merged_file

    parse = parse_event_export_file if record_type == "Event" else parse_merged_file
    try:
        clip = parse(Path(filename))
    except ValueError:
        return None
    if clip is None:
        return None
    return clip.start, clip.end + timedelta(seconds=DEFAULT_CLIP_SEC)


def _merge_ledger(source: Path) -> dict[str, list[dict[str, Any]]]:
    """Merge status per record type from SD import state + inventory."""
    best: dict[tuple[str, str, str], tuple[str, int]] = {}
    status_rank = {"merged": 3, "skipped": 3, "failed": 2, "planned": 1, "pending": 0}

    def offer(
        record_type: str, camera: str, name: str, status: str, clip_count: int
    ) -> None:
        key = (record_type, camera, name)
        prev = best.get(key)
        if prev is None or status_rank.get(status, 0) > status_rank.get(prev[0], 0):
            best[key] = (status, max(clip_count, prev[1] if prev else 0))

    try:
        state_files = sorted(sd_import_dir(source).glob("import_*.state.json"))
    except OSError:
        state_files = []
    for path in state_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key, entry in (data.get("files") or {}).items():
            parts = str(key).split("/")
            if len(parts) != 3 or not isinstance(entry, dict):
                continue
            offer(
                parts[0],
                parts[1],
                parts[2],
                str(entry.get("status") or "pending"),
                int(entry.get("clip_count") or 0),
            )

    inv = _load_inventory(source) or {}
    for record_type, block in (inv.get("record_types") or {}).items():
        if not isinstance(block, dict):
            continue
        for camera, outputs in (block.get("merge_outputs") or {}).items():
            if not isinstance(outputs, dict):
                continue
            for name, info in outputs.items():
                if isinstance(info, dict):
                    offer(
                        record_type,
                        camera,
                        name,
                        str(info.get("status") or "pending"),
                        int(info.get("clip_count") or 0),
                    )

    ledger: dict[str, list[dict[str, Any]]] = {}
    for (record_type, camera, name), (status, clip_count) in best.items():
        window = _merge_window(record_type, name)
        ledger.setdefault(record_type, []).append(
            {
                "camera": camera,
                "status": status,
                "clip_count": clip_count,
                "start": window[0] if window else None,
                "end": window[1] if window else None,
            }
        )
    return ledger


def _planned_windows(temp_dir: Path | None, types: list[str]) -> dict[str, list[tuple[datetime, datetime]]]:
    if temp_dir is None:
        return {}
    chunks = load_autopilot_plan(temp_dir)
    if not chunks:
        return {}
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for chunk in chunks:
        if chunk.record_type not in types:
            continue
        for trip in chunk.trips:
            windows.setdefault(chunk.record_type, []).append((trip.start, trip.end))
    return windows


def _composed_windows(
    temp_dir: Path | None,
    video_dir: Path | None,
    source: Path | None,
    types: list[str],
) -> dict[str, list[tuple[datetime, datetime]]]:
    if temp_dir is None:
        return {}
    try:
        from autopilot_dashboard import Dashboard

        dash = Dashboard(
            temp_dir=temp_dir,
            video_dir=video_dir or Path("video/Output"),
            check_disk=Path("."),
            min_free_gb=20.0,
            source=source,
            types=types,
            enabled=False,
        )
        dash.reload_plan_if_changed()
        try:
            dash._refresh_from_publish_state()
            dash._refresh_from_status()
        except Exception:
            pass
    except Exception:
        return {}

    # Only after compose finished — active "compose" stays "merged" so the
    # block map matches the SD table's "imported" while encoding runs.
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in dash.rows:
        if row.status not in ("upload", "stall", "done"):
            continue
        if row.trip_start is None or row.trip_end is None:
            continue
        windows.setdefault(row.record_type, []).append((row.trip_start, row.trip_end))
    return windows


def _in_window(ts: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    for start, end in windows:
        if start <= ts <= end:
            return True
    return False


def _clip_status_from_sd_trips(
    source: Path,
    types: list[str],
    video_dir: Path | None,
    temp_dir: Path | None = None,
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """Trip-level uploaded/merged using the same rules as the SD sidebar table."""
    from autopilot_sd_table import build_sd_card_payload

    uploaded: set[tuple[str, str, str]] = set()
    merged: set[tuple[str, str, str]] = set()
    try:
        payload = build_sd_card_payload(
            source, types, video_dir=video_dir, temp_dir=temp_dir, ttl_sec=0
        )
    except Exception:
        return uploaded, merged

    for trip in payload.get("trips") or []:
        if not isinstance(trip, dict):
            continue
        record_type = str(trip.get("record_type") or "")
        imp = str(trip.get("import_status") or "none")
        # Ignore partial — only fully imported/uploaded trips paint every clip.
        if imp not in ("uploaded", "imported"):
            continue
        try:
            start = parse_datetime(str(trip.get("start") or ""))
            end = parse_datetime(str(trip.get("end") or ""))
        except ValueError:
            continue
        if record_type in SINGLE_VIDEO_TYPES:
            matched = scan_clips(source, [record_type], ["Front", "Back"], warn=False)
        else:
            all_clips = scan_clips(source, [record_type], ["Front", "Back"], warn=False)
            matched = [c for c in all_clips if start <= c.timestamp <= end]
        for clip in matched:
            key = (record_type, clip.camera, clip.path.name)
            if imp == "uploaded":
                uploaded.add(key)
            else:
                merged.add(key)
    return uploaded, merged


def _merge_status_for_clip(
    clip,
    entries: list[dict[str, Any]],
) -> str | None:
    for entry in entries:
        if entry.get("camera") != clip.camera:
            continue
        start = entry.get("start")
        end = entry.get("end")
        if not start or not end or end <= start:
            continue
        if start <= clip.timestamp <= end:
            return str(entry.get("status") or "pending")
    return None


def _resolve_status(
    clip,
    *,
    bad_names: set[str],
    uploaded: set[tuple[str, str, str]],
    merged_sources: set[tuple[str, str, str]],
    planned: dict[str, list[tuple[datetime, datetime]]],
    composed: dict[str, list[tuple[datetime, datetime]]],
    ledger: dict[str, list[dict[str, Any]]],
) -> str:
    name = clip.path.name
    key = (clip.record_type, clip.camera, name)
    entries = ledger.get(clip.record_type, [])

    if name in bad_names:
        return "error"
    merge_status = _merge_status_for_clip(clip, entries)
    if merge_status == "failed":
        return "error"
    if key in uploaded:
        return "uploaded"
    if _in_window(clip.timestamp, composed.get(clip.record_type, [])):
        return "composed"
    if key in merged_sources:
        return "merged"
    if merge_status in _DONE_MERGE:
        return "merged"
    if _in_window(clip.timestamp, planned.get(clip.record_type, [])):
        return "planned"
    return "oncard"


def build_file_map_payload(
    source: Path | None,
    types: list[str],
    *,
    video_dir: Path | None = None,
    temp_dir: Path | None = None,
    ttl_sec: float = _CACHE_TTL_SEC,
) -> dict[str, Any]:
    """One block per source MP4 on the card, grouped by record type and camera."""
    empty: dict[str, Any] = {
        "present": False,
        "total": 0,
        "counts": {},
        "groups": [],
    }
    if source is None or not source.is_dir():
        return empty

    key = f"{source.resolve()}:{','.join(types)}:{video_dir or ''}:{temp_dir or ''}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < ttl_sec:
        return cached[1]

    bad_names = _bad_clip_names(source, temp_dir)
    uploaded = _uploaded_sources(
        source, types, temp_dir=temp_dir, video_dir=video_dir
    )
    sd_uploaded, sd_merged = _clip_status_from_sd_trips(
        source, types, video_dir, temp_dir
    )
    uploaded |= sd_uploaded
    merged_sources = _merged_sources(video_dir) | sd_merged
    planned = _planned_windows(temp_dir, types)
    composed = _composed_windows(temp_dir, video_dir, source, types)
    ledger = _merge_ledger(source)

    groups: list[dict[str, Any]] = []
    counts: dict[str, int] = {status: 0 for status in STATUS_ORDER}
    total = 0

    for record_type in types:
        for camera in ("Front", "Back"):
            clips = scan_clips(source, [record_type], [camera], warn=False)
            if not clips:
                continue
            clips = sorted(clips, key=lambda c: (c.timestamp, c.sequence))
            blocks: list[dict[str, Any]] = []
            for clip in clips:
                status = _resolve_status(
                    clip,
                    bad_names=bad_names,
                    uploaded=uploaded,
                    merged_sources=merged_sources,
                    planned=planned,
                    composed=composed,
                    ledger=ledger,
                )
                counts[status] = counts.get(status, 0) + 1
                total += 1
                blocks.append(
                    {
                        "n": clip.path.name,
                        "t": clip.timestamp.strftime("%m-%d %H:%M"),
                        "s": format_file_size(_clip_bytes(clip.path)),
                        "st": status,
                    }
                )
            groups.append(
                {
                    "record_type": record_type,
                    "camera": camera,
                    "count": len(blocks),
                    "blocks": blocks,
                }
            )

    payload: dict[str, Any] = {
        "present": True,
        "total": total,
        "counts": {k: v for k, v in counts.items() if v},
        "groups": groups,
    }
    _cache[key] = (now, payload)
    return payload
