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
from import_state import sd_import_dir, sd_inventory_path
from plan_estimate import SINGLE_VIDEO_TYPES, load_autopilot_plan

DEFAULT_CLIP_SEC = 60.0
_CACHE_TTL_SEC = 45.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

DONE_MERGE_STATUSES = ("merged", "skipped")
_STATUS_RANK = {"merged": 3, "skipped": 3, "failed": 2, "planned": 1, "pending": 0}
_IMPORT_COVERAGE_MIN = 0.98
_IMPORT_LABELS = {
    "uploaded": "загружено на YouTube",
    "imported": "импортировано",
    "partial": "импорт не закончен",
    "failed": "ошибка импорта",
    "pending": "в плане (ещё не импортировано)",
    "none": "не импортировано",
}


def clear_sd_table_cache() -> None:
    _cache.clear()


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


def _merge_window(record_type: str, filename: str) -> tuple[datetime, datetime] | None:
    """Wall window of a merge output (filename end is the last clip's start)."""
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
    """Merge status per record type from SD import state files + inventory."""
    best: dict[tuple[str, str, str], tuple[str, int]] = {}

    def offer(
        record_type: str, camera: str, name: str, status: str, clip_count: int
    ) -> None:
        key = (record_type, camera, name)
        prev = best.get(key)
        if prev is None or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(prev[0], 0):
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


def _ssd_merge_windows(
    video_dir: Path | None, record_type: str
) -> dict[str, list[tuple[datetime, datetime]]]:
    """Merged files present on the host, per camera (no ffprobe)."""
    if video_dir is None or not video_dir.is_dir():
        return {}
    from compose_70mai import scan_merged_clips

    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for camera in ("Front", "Back"):
        try:
            clips = scan_merged_clips(
                video_dir, camera, record_type=record_type, probe=False
            )
        except OSError:
            continue
        if clips:
            windows[camera] = [
                (c.start, c.end + timedelta(seconds=DEFAULT_CLIP_SEC)) for c in clips
            ]
    return windows


def _covered_sec(
    windows: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> float:
    total = 0.0
    for w_start, w_end in windows:
        lo = max(start, w_start)
        hi = min(end, w_end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def _import_status(
    entries: list[dict[str, Any]],
    ssd: dict[str, list[tuple[datetime, datetime]]],
    *,
    start: datetime,
    end: datetime,
    duration_sec: float,
    single: bool,
) -> tuple[str, str]:
    """Import state of one trip: merge ledger first, SSD files as fallback."""
    if single:
        # Event/Parking: one mega-file per camera. Ignore leftover mega-merges
        # whose filename window does not overlap this card's clips / trip.
        overlapping = [
            e
            for e in entries
            if e.get("start")
            and e.get("end")
            and e["end"] > start
            and e["start"] < end
        ]
        done_cams = {
            e["camera"]
            for e in overlapping
            if e["status"] in DONE_MERGE_STATUSES and int(e["clip_count"] or 0) > 1
        }
        ssd_cams = {cam for cam, wins in ssd.items() if wins}
        done_cams |= ssd_cams
        expected = {"Front", "Back"}
        suffix = f" ({len(done_cams)}/{len(expected)} камер)"
        if expected <= done_cams:
            return "imported", _IMPORT_LABELS["imported"] + suffix
        if done_cams:
            return "partial", _IMPORT_LABELS["partial"] + suffix
        if any(e["status"] == "failed" for e in overlapping):
            return "failed", _IMPORT_LABELS["failed"]
        return "none", _IMPORT_LABELS["none"]

    relevant = [
        e
        for e in entries
        if e["start"] and e["end"] and e["end"] > start and e["start"] < end
    ]

    if relevant:
        done = sum(1 for e in relevant if e["status"] in DONE_MERGE_STATUSES)
        total = len(relevant)
        if done == total:
            return "imported", f"{_IMPORT_LABELS['imported']} ({done}/{total} файлов)"
        if any(e["status"] == "failed" for e in relevant):
            return "failed", f"{_IMPORT_LABELS['failed']} ({done}/{total} файлов)"
        if done:
            return "partial", f"{_IMPORT_LABELS['partial']} ({done}/{total} файлов)"
        return "pending", f"{_IMPORT_LABELS['pending']} (0/{total} файлов)"

    if not ssd or duration_sec <= 0:
        return "none", _IMPORT_LABELS["none"]

    pct = min(_covered_sec(w, start, end) for w in ssd.values()) / duration_sec
    if pct >= _IMPORT_COVERAGE_MIN:
        return "imported", f"{_IMPORT_LABELS['imported']} (merge на SSD)"
    if pct > 0.05:
        return "partial", f"{_IMPORT_LABELS['partial']} (на SSD ~{pct * 100:.0f}%)"
    return "none", _IMPORT_LABELS["none"]


def _planned_windows(
    temp_dir: Path | None, types: list[str]
) -> dict[str, list[tuple[datetime, datetime]]]:
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


def _overlaps(
    start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]
) -> bool:
    for w_start, w_end in windows:
        if w_start < end and w_end > start:
            return True
    return False


def _annotate_import_status(
    rows: list[dict[str, Any]],
    *,
    entries: list[dict[str, Any]],
    ssd: dict[str, list[tuple[datetime, datetime]]],
    single: bool,
    planned: list[tuple[datetime, datetime]] | None = None,
) -> None:
    plan_windows = planned or []
    for row in rows:
        if row.get("youtube_url"):
            row["import_status"] = "uploaded"
            row["import_label"] = _IMPORT_LABELS["uploaded"]
            continue
        try:
            start = parse_datetime(row["start"])
            end = parse_datetime(row["end"])
        except (KeyError, ValueError):
            row["import_status"] = "none"
            row["import_label"] = _IMPORT_LABELS["none"]
            continue
        status, label = _import_status(
            entries,
            ssd,
            start=start,
            end=end,
            duration_sec=float(row.get("duration_sec") or 0.0),
            single=single,
        )
        # Align with left panel + file map: in-plan but not imported → pending.
        if status == "none" and (
            (single and plan_windows) or _overlaps(start, end, plan_windows)
        ):
            status, label = "pending", _IMPORT_LABELS["pending"]
        row["import_status"] = status
        row["import_label"] = label


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
                "youtube_url": trip.get("youtube_url"),
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
    video_dir: Path | None = None,
    temp_dir: Path | None = None,
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

    key = f"{source.resolve()}:{','.join(types)}:{video_dir or ''}:{temp_dir or ''}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < ttl_sec:
        return cached[1]

    try:
        from import_state import drop_orphaned_mega_merges

        drop_orphaned_mega_merges(source)
    except Exception:
        pass
    ledger = _merge_ledger(source)
    planned = _planned_windows(temp_dir, types)
    trips: list[dict[str, Any]] = []
    for record_type in types:
        rows = _trip_rows_from_inventory(
            source, record_type, session_gap=session_gap
        )
        if not rows:
            rows = _trip_rows_from_scan(
                source, record_type, session_gap=session_gap
            )
        _annotate_import_status(
            rows,
            entries=ledger.get(record_type, []),
            ssd=_ssd_merge_windows(video_dir, record_type),
            single=record_type in SINGLE_VIDEO_TYPES,
            planned=planned.get(record_type, []),
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
