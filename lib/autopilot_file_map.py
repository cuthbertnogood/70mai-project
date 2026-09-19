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


def _load_dashboard_rows(
    temp_dir: Path | None,
    video_dir: Path | None,
    source: Path | None,
    types: list[str],
) -> list[Any]:
    if temp_dir is None:
        return []
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
        return list(dash.rows or [])
    except Exception:
        return []


def _composed_windows(
    temp_dir: Path | None,
    video_dir: Path | None,
    source: Path | None,
    types: list[str],
    *,
    rows: list[Any] | None = None,
) -> dict[str, list[tuple[datetime, datetime]]]:
    if rows is None:
        rows = _load_dashboard_rows(temp_dir, video_dir, source, types)
    # Only after compose finished — active "compose" stays "merged" so the
    # block map matches the SD table's "imported" while encoding runs.
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in rows:
        if getattr(row, "status", "") not in ("upload", "stall", "done"):
            continue
        start = getattr(row, "trip_start", None)
        end = getattr(row, "trip_end", None)
        if start is None or end is None:
            continue
        windows.setdefault(str(row.record_type), []).append((start, end))
    return windows


def _uploaded_windows(rows: list[Any]) -> dict[str, list[tuple[datetime, datetime]]]:
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in rows:
        if getattr(row, "status", "") != "done":
            continue
        start = getattr(row, "trip_start", None)
        end = getattr(row, "trip_end", None)
        if start is None or end is None:
            continue
        windows.setdefault(str(row.record_type), []).append((start, end))
    return windows


def _youtube_chunk_progress(rows: list[Any]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    try:
        from autopilot_dashboard import chunk_summary_counts

        done, total, _ = chunk_summary_counts(rows)
        return int(done), int(total)
    except Exception:
        return 0, 0


def _host_merged_groups(
    video_dir: Path | None,
    types: list[str],
    *,
    composed: dict[str, list[tuple[datetime, datetime]]],
    uploaded: dict[str, list[tuple[datetime, datetime]]],
) -> tuple[int, list[dict[str, Any]]]:
    """Block map of merge MP4s currently on the host video_dir."""
    if video_dir is None or not video_dir.is_dir():
        return 0, []

    from compose_70mai import parse_event_export_file, parse_merged_file

    groups: list[dict[str, Any]] = []
    total_files = 0
    for record_type in types:
        parse = parse_event_export_file if record_type == "Event" else parse_merged_file
        pattern = {
            "Event": "EV_*.mp4",
            "Parking": "PA_*.mp4",
        }.get(record_type, "NO_*.mp4")
        for camera in ("Front", "Back"):
            folder = video_dir / record_type / camera
            if not folder.is_dir():
                continue
            try:
                paths = sorted(folder.glob(pattern))
            except OSError:
                continue
            blocks: list[dict[str, Any]] = []
            for path in paths:
                try:
                    clip = parse(path)
                except ValueError:
                    clip = None
                if clip is None:
                    continue
                mid = clip.start + (clip.end - clip.start) / 2
                if _in_window(mid, uploaded.get(record_type, [])):
                    status = "uploaded"
                elif _in_window(mid, composed.get(record_type, [])):
                    status = "composed"
                else:
                    status = "merged"
                total_files += 1
                blocks.append(
                    {
                        "n": path.name,
                        "t": clip.start.strftime("%m-%d %H:%M"),
                        "s": format_file_size(_clip_bytes(path)),
                        "st": status,
                    }
                )
            if blocks:
                groups.append(
                    {
                        "record_type": record_type,
                        "camera": camera,
                        "count": len(blocks),
                        "blocks": blocks,
                    }
                )
    return total_files, groups


def _host_compose_groups(
    temp_dir: Path | None,
    types: list[str],
    *,
    rows: list[Any],
    live: dict[str, Any] | None = None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Block map of compose trip_*.mp4 under .publish_tmp."""
    if temp_dir is None or not temp_dir.is_dir():
        return 0, 0, []

    from publish_paths import iter_compose_video_roots, parse_compose_output_path

    live = live if isinstance(live, dict) else {}
    live_phase = str(live.get("phase") or "")
    live_rt = str(live.get("record_type") or "")
    live_chunk = int(live.get("chunk_index") or 0)
    live_trip = int(live.get("trip_index") or 0)

    row_by_key: dict[tuple[str, int, int], Any] = {}
    for row in rows:
        rt = str(getattr(row, "record_type", "") or "")
        ck = int(getattr(row, "chunk_index", 0) or 0)
        ti = int(getattr(row, "trip_index", 0) or 0)
        if rt and ck and ti:
            row_by_key[(rt, ck, ti)] = row

    by_type: dict[str, list[dict[str, Any]]] = {}
    total_files = 0
    total_bytes = 0
    seen: set[Path] = set()

    for root in iter_compose_video_roots(temp_dir):
        try:
            paths = sorted(root.rglob("trip_*.mp4"))
        except OSError:
            continue
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen or not path.is_file():
                continue
            # Skip partial leftovers named *.mp4.partial already filtered by glob.
            if path.name.endswith(".partial"):
                continue
            parsed = parse_compose_output_path(path)
            if parsed is None:
                continue
            record_type, chunk_index, trip_index = parsed
            if record_type is None:
                record_type = live_rt or (types[0] if types else "Normal")
            if types and record_type not in types:
                continue
            seen.add(resolved)
            size = _clip_bytes(path)
            total_files += 1
            total_bytes += size

            row = row_by_key.get((record_type, chunk_index, trip_index))
            row_status = str(getattr(row, "status", "") or "") if row else ""
            if row_status == "done":
                status = "uploaded"
            elif row_status in ("upload", "oauth", "stall") or (
                live_phase in ("upload", "oauth", "stall")
                and live_rt == record_type
                and live_chunk == chunk_index
                and (not live_trip or live_trip == trip_index)
            ):
                status = "composed"
            else:
                status = "composed"

            active = False
            proc = ""
            if live_phase == "compose" and live_rt == record_type and (
                live_chunk == chunk_index
            ) and (not live_trip or live_trip == trip_index):
                active = True
                proc = "compose"
            elif live_phase in ("upload", "oauth", "stall") and live_rt == record_type and (
                live_chunk == chunk_index
            ) and (not live_trip or live_trip == trip_index):
                active = True
                proc = "upload"

            trip_start = getattr(row, "trip_start", None) if row else None
            t_label = (
                trip_start.strftime("%m-%d %H:%M")
                if trip_start is not None
                else f"р{chunk_index} t{trip_index}"
            )
            by_type.setdefault(record_type, []).append(
                {
                    "n": path.name,
                    "t": t_label,
                    "s": format_file_size(size),
                    "st": status,
                    "active": active,
                    "proc": proc,
                    "chunk": chunk_index,
                    "trip": trip_index,
                }
            )

    groups: list[dict[str, Any]] = []
    for record_type in types:
        blocks = by_type.get(record_type) or []
        if not blocks:
            continue
        blocks.sort(key=lambda b: (int(b.get("chunk") or 0), int(b.get("trip") or 0)))
        groups.append(
            {
                "record_type": record_type,
                "camera": "compose",
                "count": len(blocks),
                "blocks": blocks,
            }
        )
    return total_files, total_bytes, groups


def _build_host_payload(
    *,
    counts: dict[str, int],
    total: int,
    video_dir: Path | None,
    types: list[str],
    composed: dict[str, list[tuple[datetime, datetime]]],
    uploaded_windows: dict[str, list[tuple[datetime, datetime]]],
    youtube_done: int,
    youtube_total: int,
    temp_dir: Path | None = None,
    rows: list[Any] | None = None,
    live: dict[str, Any] | None = None,
) -> dict[str, Any]:
    copied_done = sum(
        int(counts.get(status) or 0) for status in ("merged", "composed", "uploaded")
    )
    merged_clips = sum(
        int(counts.get(status) or 0) for status in ("merged", "composed", "uploaded")
    )
    merged_files, groups = _host_merged_groups(
        video_dir, types, composed=composed, uploaded=uploaded_windows
    )
    compose_files, compose_bytes, compose_groups = _host_compose_groups(
        temp_dir, types, rows=rows or [], live=live
    )
    # Prefer plan/chunk progress; fall back to source-clip uploaded/total.
    yt_done = youtube_done
    yt_total = youtube_total
    if yt_total <= 0:
        yt_done = int(counts.get("uploaded") or 0)
        yt_total = max(total - int(counts.get("error") or 0), yt_done)
    return {
        "present": True,
        "copied": {"done": copied_done, "total": total},
        "merged": {"files": merged_files, "clips": merged_clips},
        "compose": {
            "files": compose_files,
            "bytes": compose_bytes,
            "size": format_file_size(compose_bytes) if compose_bytes else "—",
        },
        "youtube": {"done": yt_done, "total": yt_total},
        "processing": {"count": 0, "phase": "", "label": "", "files": []},
        "groups": groups,
        "compose_groups": compose_groups,
        "total": merged_files + compose_files,
    }


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


def _clip_key_from_name(
    name: str, record_type: str | None = None
) -> tuple[str, str, str] | None:
    file_name = Path(name).name
    if not file_name.lower().endswith(".mp4"):
        return None
    rt_map = {"NO": "Normal", "EV": "Event", "PA": "Parking"}
    rt = record_type or rt_map.get(file_name[:2], "Normal")
    upper = file_name.upper()
    if upper.endswith("F.MP4"):
        camera = "Front"
    elif upper.endswith("B.MP4"):
        camera = "Back"
    else:
        return None
    return (rt, camera, file_name)


def _processing_clip_keys(
    source: Path,
    types: list[str],
    *,
    live: dict[str, Any] | None,
    rows: list[Any],
) -> set[tuple[str, str, str]]:
    """SD clips currently in copy / merge / compose / upload for the active roll."""
    keys: set[tuple[str, str, str]] = set()
    if not live:
        return keys
    phase = str(live.get("phase") or "")
    record_type = str(live.get("record_type") or "")
    conveyors = live.get("conveyors") if isinstance(live.get("conveyors"), dict) else {}
    copy = conveyors.get("copy") if isinstance(conveyors.get("copy"), dict) else {}
    merge = conveyors.get("merge") if isinstance(conveyors.get("merge"), dict) else {}

    if copy.get("active"):
        detail = str(copy.get("detail") or "")
        if "SD→SSD " in detail:
            clip_name = detail.split("SD→SSD ", 1)[1].strip().split()[0]
            key = _clip_key_from_name(clip_name, record_type or None)
            if key:
                keys.add(key)

    if merge.get("active"):
        merge_file = Path(str(merge.get("file") or "")).name
        if merge_file:
            rt = record_type or "Normal"
            window = _merge_window(rt, merge_file)
            if window:
                start, end = window
                for camera in ("Front", "Back"):
                    for clip in scan_clips(source, [rt], [camera], warn=False):
                        if start <= clip.timestamp <= end:
                            keys.add((rt, camera, clip.path.name))

    chunk_index = int(live.get("chunk_index") or 0)
    if phase in ("import", "compose", "upload", "stall", "oauth") and record_type and chunk_index:
        chunk_rows = [
            row
            for row in rows
            if str(getattr(row, "record_type", "")) == record_type
            and int(getattr(row, "chunk_index", 0) or 0) == chunk_index
        ]
        for row in chunk_rows:
            row_status = str(getattr(row, "status", "") or "")
            if phase in ("upload", "stall", "oauth") and row_status not in (
                "upload",
                "oauth",
                "stall",
            ):
                continue
            if phase == "compose" and row_status not in ("compose", "import", "stall"):
                continue
            start = getattr(row, "trip_start", None)
            end = getattr(row, "trip_end", None)
            if start is None or end is None:
                continue
            if record_type in SINGLE_VIDEO_TYPES:
                for camera in ("Front", "Back"):
                    for clip in scan_clips(source, [record_type], [camera], warn=False):
                        keys.add((record_type, camera, clip.path.name))
            else:
                for camera in ("Front", "Back"):
                    for clip in scan_clips(source, [record_type], [camera], warn=False):
                        if start <= clip.timestamp <= end:
                            keys.add((record_type, camera, clip.path.name))
    return keys


def _active_phase(live: dict[str, Any] | None) -> str:
    if not live:
        return ""
    phase = str(live.get("phase") or "")
    if phase in ("import", "compose", "upload"):
        return phase
    if phase in ("stall", "oauth"):
        return "upload"
    conveyors = live.get("conveyors") if isinstance(live.get("conveyors"), dict) else {}
    for lane in ("copy", "merge"):
        info = conveyors.get(lane)
        if isinstance(info, dict) and info.get("active"):
            return "import"
    return ""


def _processing_host_info(
    live: dict[str, Any] | None,
    rows: list[Any],
    host_groups: list[dict[str, Any]] | None = None,
    *,
    compose_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Host merge/compose files currently in copy/merge/compose/upload + phase + count."""
    names: set[str] = set()
    phase = _active_phase(live)
    if not live:
        return {"files": [], "count": 0, "phase": "", "label": ""}

    conveyors = live.get("conveyors") if isinstance(live.get("conveyors"), dict) else {}
    for lane in ("copy", "merge"):
        info = conveyors.get(lane)
        if not isinstance(info, dict) or not info.get("active"):
            continue
        file_name = Path(str(info.get("file") or "")).name
        if file_name:
            names.add(file_name)
        if lane == "copy":
            phase = phase or "import"
        elif lane == "merge":
            phase = phase or "import"

    # During compose/upload: highlight host merges that overlap the active roll.
    if phase in ("compose", "upload", "stall", "oauth") or (
        phase == "import" and not names
    ):
        record_type = str(live.get("record_type") or "")
        chunk_index = int(live.get("chunk_index") or 0)
        windows: list[tuple[datetime, datetime]] = []
        for row in rows:
            if record_type and str(getattr(row, "record_type", "")) != record_type:
                continue
            if chunk_index and int(getattr(row, "chunk_index", 0) or 0) != chunk_index:
                continue
            row_status = str(getattr(row, "status", "") or "")
            if phase in ("compose",) and row_status not in (
                "compose",
                "import",
                "stall",
            ):
                continue
            if phase in ("upload", "stall", "oauth") and row_status not in (
                "upload",
                "oauth",
                "stall",
                "compose",
            ):
                continue
            start = getattr(row, "trip_start", None)
            end = getattr(row, "trip_end", None)
            if start is not None and end is not None:
                windows.append((start, end))
        if not windows and record_type and chunk_index:
            for row in rows:
                if (
                    str(getattr(row, "record_type", "")) == record_type
                    and int(getattr(row, "chunk_index", 0) or 0) == chunk_index
                ):
                    start = getattr(row, "trip_start", None)
                    end = getattr(row, "trip_end", None)
                    if start is not None and end is not None:
                        windows.append((start, end))
        for group in host_groups or []:
            if not isinstance(group, dict):
                continue
            rt = str(group.get("record_type") or "")
            if record_type and rt != record_type:
                continue
            for block in group.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                name = str(block.get("n") or "")
                if not name:
                    continue
                window = _merge_window(rt, name)
                if not window:
                    continue
                mid = window[0] + (window[1] - window[0]) / 2
                if _in_window(mid, windows):
                    names.add(name)

    # Compose / upload-ready trip_*.mp4 on host.
    live_trip = int(live.get("trip_index") or 0) if live else 0
    live_rt = str(live.get("record_type") or "") if live else ""
    live_chunk = int(live.get("chunk_index") or 0) if live else 0
    for group in compose_groups or []:
        if not isinstance(group, dict):
            continue
        rt = str(group.get("record_type") or "")
        for block in group.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            name = str(block.get("n") or "")
            if not name:
                continue
            if block.get("active"):
                names.add(name)
                continue
            if phase in ("compose", "upload", "stall", "oauth") and live_rt and (
                rt == live_rt
                and int(block.get("chunk") or 0) == live_chunk
                and (not live_trip or int(block.get("trip") or 0) == live_trip)
            ):
                names.add(name)

    phase_labels = {
        "import": "copy/merge",
        "compose": "compose",
        "upload": "upload",
        "stall": "upload",
        "oauth": "upload",
    }
    label = phase_labels.get(phase, phase)
    return {
        "files": sorted(names),
        "count": len(names),
        "phase": phase,
        "label": label,
    }


def processing_snapshot(
    source: Path | None,
    types: list[str],
    *,
    temp_dir: Path | None,
    video_dir: Path | None,
    live: dict[str, Any] | None = None,
    rows: list[Any] | None = None,
    host_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Active SD clips + host merge files for live dashboard highlighting."""
    if rows is None:
        rows = _load_dashboard_rows(temp_dir, video_dir, source, types)
    if live is None:
        try:
            from autopilot_dashboard import resolve_live_status

            live = resolve_live_status(temp_dir, rows=rows) or {}
        except Exception:
            live = {}
    clip_keys: set[tuple[str, str, str]] = set()
    if source is not None and source.is_dir():
        clip_keys = _processing_clip_keys(source, types, live=live, rows=rows)
    if host_groups is None and video_dir is not None:
        composed = _composed_windows(
            temp_dir, video_dir, source, types, rows=rows
        )
        _, host_groups = _host_merged_groups(
            video_dir,
            types,
            composed=composed,
            uploaded=_uploaded_windows(rows),
        )
    host_info = _processing_host_info(live, rows, host_groups)
    return {
        "clips": [list(key) for key in sorted(clip_keys)],
        "host": host_info["files"],
        "host_count": host_info["count"],
        "host_phase": host_info["phase"],
        "host_label": host_info["label"],
    }


def _stamp_processing(
    payload: dict[str, Any],
    source: Path | None,
    types: list[str],
    *,
    temp_dir: Path | None,
    video_dir: Path | None,
) -> None:
    """Mark blocks currently being processed (always fresh, even on cache hit)."""
    live: dict[str, Any] = {}
    rows: list[Any] = []
    try:
        from autopilot_dashboard import resolve_live_status

        rows = _load_dashboard_rows(temp_dir, video_dir, source, types)
        live = resolve_live_status(temp_dir, rows=rows) or {}
    except Exception:
        pass

    clip_keys: set[tuple[str, str, str]] = set()
    if source is not None and source.is_dir():
        clip_keys = _processing_clip_keys(
            source, types, live=live, rows=rows
        )

    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        record_type = str(group.get("record_type") or "")
        camera = str(group.get("camera") or "")
        for block in group.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            key = (record_type, camera, str(block.get("n") or ""))
            block["active"] = key in clip_keys

    host = payload.get("host")
    if isinstance(host, dict):
        # Refresh compose trip_*.mp4 list every tick — new files appear mid-run.
        compose_files, compose_bytes, compose_groups = _host_compose_groups(
            temp_dir, types, rows=rows, live=live
        )
        host["compose"] = {
            "files": compose_files,
            "bytes": compose_bytes,
            "size": format_file_size(compose_bytes) if compose_bytes else "—",
        }
        host["compose_groups"] = compose_groups
        host["total"] = int(host.get("merged", {}).get("files") or 0) + compose_files

        host_info = _processing_host_info(
            live,
            rows,
            host.get("groups") if isinstance(host.get("groups"), list) else [],
            compose_groups=compose_groups,
        )
        host_files = set(host_info["files"])
        host["processing"] = {
            "count": host_info["count"],
            "phase": host_info["phase"],
            "label": host_info["label"],
            "files": host_info["files"],
        }
        for group in host.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for block in group.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                active = str(block.get("n") or "") in host_files
                block["active"] = active
                if active and host_info["label"]:
                    block["proc"] = host_info["label"]
                else:
                    block.pop("proc", None)
        for group in host.get("compose_groups") or []:
            if not isinstance(group, dict):
                continue
            for block in group.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                # Keep status/active from _host_compose_groups; also force-active
                # if name is in the processing set (upload-ready trip mp4).
                if str(block.get("n") or "") in host_files and not block.get("active"):
                    block["active"] = True
                    block["proc"] = host_info["label"] or block.get("proc") or ""


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
    empty_host: dict[str, Any] = {
        "present": False,
        "copied": {"done": 0, "total": 0},
        "merged": {"files": 0, "clips": 0},
        "compose": {"files": 0, "bytes": 0, "size": "—"},
        "youtube": {"done": 0, "total": 0},
        "processing": {"count": 0, "phase": "", "label": "", "files": []},
        "groups": [],
        "compose_groups": [],
        "total": 0,
    }
    empty: dict[str, Any] = {
        "present": False,
        "total": 0,
        "counts": {},
        "groups": [],
        "host": empty_host,
    }
    if source is None or not source.is_dir():
        # Host merge map can still show files already on SSD.
        dash_rows = _load_dashboard_rows(temp_dir, video_dir, source, types)
        composed = _composed_windows(
            temp_dir, video_dir, source, types, rows=dash_rows
        )
        yt_done, yt_total = _youtube_chunk_progress(dash_rows)
        try:
            from autopilot_dashboard import resolve_live_status

            live_now = resolve_live_status(temp_dir, rows=dash_rows) or {}
        except Exception:
            live_now = {}
        host = _build_host_payload(
            counts={},
            total=0,
            video_dir=video_dir,
            types=types,
            composed=composed,
            uploaded_windows=_uploaded_windows(dash_rows),
            youtube_done=yt_done,
            youtube_total=yt_total,
            temp_dir=temp_dir,
            rows=dash_rows,
            live=live_now,
        )
        if host["merged"]["files"] or host["compose"]["files"] or yt_total:
            empty = {**empty, "host": host}
        _stamp_processing(empty, source, types, temp_dir=temp_dir, video_dir=video_dir)
        return empty

    key = f"{source.resolve()}:{','.join(types)}:{video_dir or ''}:{temp_dir or ''}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < ttl_sec:
        payload = cached[1]
        _stamp_processing(
            payload, source, types, temp_dir=temp_dir, video_dir=video_dir
        )
        return payload

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
    dash_rows = _load_dashboard_rows(temp_dir, video_dir, source, types)
    composed = _composed_windows(
        temp_dir, video_dir, source, types, rows=dash_rows
    )
    uploaded_windows = _uploaded_windows(dash_rows)
    yt_done, yt_total = _youtube_chunk_progress(dash_rows)
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

    host = _build_host_payload(
        counts=counts,
        total=total,
        video_dir=video_dir,
        types=types,
        composed=composed,
        uploaded_windows=uploaded_windows,
        youtube_done=yt_done,
        youtube_total=yt_total,
        temp_dir=temp_dir,
        rows=dash_rows,
        live=None,
    )
    payload: dict[str, Any] = {
        "present": True,
        "total": total,
        "counts": {k: v for k, v in counts.items() if v},
        "groups": groups,
        "host": host,
    }
    _cache[key] = (now, payload)
    _stamp_processing(payload, source, types, temp_dir=temp_dir, video_dir=video_dir)
    return payload
