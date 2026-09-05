#!/usr/bin/env python3
"""SD card trip/file table for Autopilot web Dashboard."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from import_70mai import (
    format_duration,
    format_file_size,
    parse_datetime,
    scan_clips,
    split_sessions,
)
from import_state import sd_inventory_path
from plan_estimate import SINGLE_VIDEO_TYPES

DEFAULT_CLIP_SEC = 60.0
_CACHE_TTL_SEC = 45.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _clip_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _session_wall_end(session: list) -> datetime:
    last = session[-1]
    extra = float(last.duration or DEFAULT_CLIP_SEC)
    return last.timestamp + timedelta(seconds=extra)


def _clips_for_window(
    clips: list,
    start: datetime,
    end: datetime,
) -> list:
    return [c for c in clips if start <= c.timestamp <= end]


def _load_inventory(source: Path) -> dict[str, Any] | None:
    path = sd_inventory_path(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _trip_rows_from_inventory(
    source: Path,
    record_type: str,
    *,
    session_gap: float,
) -> list[dict[str, Any]]:
    inv = _load_inventory(source)
    if not inv:
        return []
    block = (inv.get("record_types") or {}).get(record_type)
    if not isinstance(block, dict):
        return []
    trips = block.get("trips")
    if not isinstance(trips, list) or not trips:
        return []

    front = scan_clips(source, [record_type], ["Front"], warn=False)
    back = scan_clips(source, [record_type], ["Back"], warn=False)
    all_clips = front + back
    rows: list[dict[str, Any]] = []

    for trip in trips:
        if not isinstance(trip, dict):
            continue
        idx = int(trip.get("index") or len(rows) + 1)
        start_s = str(trip.get("start") or "")
        end_s = str(trip.get("end") or "")
        try:
            start = parse_datetime(start_s)
            end = parse_datetime(end_s)
        except ValueError:
            continue
        # Event/Parking: trip end in inventory is timeline length (start + duration),
        # not the last clip timestamp — clips can span weeks on the card.
        if record_type in SINGLE_VIDEO_TYPES:
            matched = all_clips
        else:
            matched = _clips_for_window(all_clips, start, end)
        size_bytes = sum(_clip_bytes(c.path) for c in matched)
        dur = float(trip.get("duration_sec") or 0.0)
        if record_type in SINGLE_VIDEO_TYPES:
            label = "все клипы"
        else:
            label = f"trip {idx} · {start:%m-%d %H:%M}"
        clip_count = len(matched)
        if record_type in SINGLE_VIDEO_TYPES and not clip_count:
            clip_count = int(trip.get("clip_count") or 0)
        rows.append(
            {
                "record_type": record_type,
                "trip_index": idx,
                "label": label,
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(dur, 1),
                "duration": trip.get("duration") or format_duration(dur),
                "clip_count": clip_count,
                "size_bytes": size_bytes,
                "size": format_file_size(size_bytes),
            }
        )
    return rows


def _trip_rows_from_scan(
    source: Path,
    record_type: str,
    *,
    session_gap: float,
) -> list[dict[str, Any]]:
    front = scan_clips(source, [record_type], ["Front"], warn=False)
    back = scan_clips(source, [record_type], ["Back"], warn=False)
    rows: list[dict[str, Any]] = []

    if record_type in SINGLE_VIDEO_TYPES:
        all_clips = front + back
        if not all_clips:
            return []
        start = min(c.timestamp for c in all_clips)
        end = max(_session_wall_end([c]) for c in all_clips)
        dur = max(0.0, (end - start).total_seconds())
        size_bytes = sum(_clip_bytes(c.path) for c in all_clips)
        rows.append(
            {
                "record_type": record_type,
                "trip_index": 1,
                "label": "все клипы",
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(dur, 1),
                "duration": format_duration(dur),
                "clip_count": len(all_clips),
                "size_bytes": size_bytes,
                "size": format_file_size(size_bytes),
            }
        )
        return rows

    if not front:
        return []
    for idx, session in enumerate(split_sessions(front, session_gap), start=1):
        start = session[0].timestamp
        end = _session_wall_end(session)
        matched = list(session)
        matched.extend(_clips_for_window(back, start, end))
        dur = max(0.0, (end - start).total_seconds())
        size_bytes = sum(_clip_bytes(c.path) for c in matched)
        rows.append(
            {
                "record_type": record_type,
                "trip_index": idx,
                "label": f"trip {idx} · {start:%m-%d %H:%M}",
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(dur, 1),
                "duration": format_duration(dur),
                "clip_count": len(matched),
                "size_bytes": size_bytes,
                "size": format_file_size(size_bytes),
            }
        )
    return rows


def build_sd_card_payload(
    source: Path | None,
    types: list[str],
    *,
    session_gap: float = 120.0,
    ttl_sec: float = _CACHE_TTL_SEC,
) -> dict[str, Any]:
    """Trips on SD with duration and on-card file size (cached)."""
    empty: dict[str, Any] = {
        "present": False,
        "path": None,
        "updated_at": None,
        "disk": {},
        "video_total": "—",
        "trips": [],
    }
    if source is None or not source.is_dir():
        return empty

    key = f"{source.resolve()}:{','.join(types)}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < ttl_sec:
        return cached[1]

    trips: list[dict[str, Any]] = []
    for record_type in types:
        rows = _trip_rows_from_inventory(
            source, record_type, session_gap=session_gap
        )
        if not rows:
            rows = _trip_rows_from_scan(
                source, record_type, session_gap=session_gap
            )
        trips.extend(rows)

    disk: dict[str, Any] = {}
    video_total = 0
    try:
        from card_storage_stats import collect_card_storage_stats

        stats = collect_card_storage_stats(source)
        du = stats.get("disk") or {}
        disk = {
            "free": format_file_size(int(du.get("free_bytes") or 0)),
            "used": format_file_size(int(du.get("used_bytes") or 0)),
            "total": format_file_size(int(du.get("total_bytes") or 0)),
            "free_bytes": int(du.get("free_bytes") or 0),
        }
        video_total = int(stats.get("video_total_bytes") or 0)
    except Exception:
        pass

    inv = _load_inventory(source)
    payload: dict[str, Any] = {
        "present": True,
        "path": str(source),
        "updated_at": (inv or {}).get("updated_at"),
        "disk": disk,
        "video_total": format_file_size(video_total) if video_total else "—",
        "trips": trips,
    }
    _cache[key] = (now, payload)
    return payload
