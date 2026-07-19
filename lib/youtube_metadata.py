#!/usr/bin/env python3
"""YouTube title/description/comment text from dashcam clip timelines."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from import_70mai import log
from plan_estimate import SINGLE_VIDEO_TYPES

RECORD_TYPE_RU: dict[str, str] = {
    "Normal": "простые записи",
    "Event": "запись события",
    "Parking": "запись парковки",
}

TITLE_MAX_LEN = 100
BODY_MAX_LEN = 9500
DATETIME_FMT = "%d.%m.%Y %H:%M:%S"
DATETIME_TITLE_FMT = "%d.%m.%Y %H:%M"
TIME_ONLY_FMT = "%H:%M"


def record_type_label(record_type: str) -> str:
    return RECORD_TYPE_RU.get(record_type, record_type)


def _format_dt(dt: datetime) -> str:
    return dt.strftime(DATETIME_FMT)


def _format_title_start(dt: datetime) -> str:
    return dt.strftime(DATETIME_TITLE_FMT)


def _format_title_end(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return end.strftime(TIME_ONLY_FMT)
    return end.strftime(DATETIME_TITLE_FMT)


def _title_prefix(base_title: str) -> str:
    text = (base_title or "70mai").strip()
    if not text:
        return "70mai"
    return text.split("|", 1)[0].strip()


def _clip_ranges_from_slots(slots) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    for slot in slots:
        start = slot.wall
        end = start + timedelta(seconds=slot.duration)
        ranges.append((start, end))
    return ranges


def _filter_ranges_by_window(
    ranges: list[tuple[datetime, datetime]],
    wall_start: datetime | None,
    wall_end: datetime | None,
) -> list[tuple[datetime, datetime]]:
    if wall_start is None or wall_end is None:
        return ranges
    kept: list[tuple[datetime, datetime]] = []
    for start, end in ranges:
        if start < wall_end and end > wall_start:
            kept.append((start, end))
    return kept


def _load_manifest_entries(video_dir: Path, record_type: str) -> tuple[list, list]:
    from clip_timeline import load_manifest
    from compose_70mai import scan_merged_clips

    front_entries: list = []
    back_entries: list = []
    for camera, bucket in (("Front", front_entries), ("Back", back_entries)):
        for merged in scan_merged_clips(
            video_dir, camera, record_type=record_type, probe=False
        ):
            manifest = load_manifest(merged.path)
            if manifest and manifest.clips:
                bucket.extend(manifest.clips)
    return front_entries, back_entries


def _ranges_from_sd(
    source: Path,
    record_type: str,
    wall_start: datetime | None,
    wall_end: datetime | None,
    ffprobe: str = "ffprobe",
) -> list[tuple[datetime, datetime]]:
    from import_70mai import scan_clips
    from plan_estimate import probe_clips

    if not source.is_dir():
        return []
    raw = scan_clips(source, [record_type], ["Front"], warn=False)
    if not raw:
        return []
    clips = probe_clips(raw, ffprobe)
    ranges: list[tuple[datetime, datetime]] = []
    for clip in clips:
        start = clip.timestamp
        dur = float(clip.duration or 0.0)
        if dur <= 0:
            continue
        end = start + timedelta(seconds=dur)
        ranges.append((start, end))
    ranges.sort(key=lambda pair: pair[0])
    return _filter_ranges_by_window(ranges, wall_start, wall_end)


def collect_clip_ranges(
    video_dir: Path,
    record_type: str,
    wall_start: datetime | None = None,
    wall_end: datetime | None = None,
    *,
    source: Path | None = None,
    ffprobe: str = "ffprobe",
) -> list[tuple[datetime, datetime]]:
    """Chronological (start, end) for each source slot in a publish window."""
    from clip_timeline import build_slots

    front_entries, back_entries = _load_manifest_entries(video_dir, record_type)
    if front_entries or back_entries:
        mode = "slot" if record_type in SINGLE_VIDEO_TYPES else "wall"
        slots = build_slots(
            front_entries,
            back_entries,
            mode=mode,
            timeline_start=wall_start if mode == "wall" else None,
        )
        ranges = _clip_ranges_from_slots(slots)
        if record_type in SINGLE_VIDEO_TYPES:
            return ranges
        filtered = _filter_ranges_by_window(ranges, wall_start, wall_end)
        if filtered:
            return filtered

    if source is not None:
        sd_ranges = _ranges_from_sd(source, record_type, wall_start, wall_end, ffprobe)
        if sd_ranges:
            return sd_ranges

    if wall_start is not None and wall_end is not None:
        log(
            "  Warning: clip list unavailable — using trip/chunk window only "
            f"({wall_start:%Y-%m-%d %H:%M:%S} … {wall_end:%Y-%m-%d %H:%M:%S})"
        )
        return [(wall_start, wall_end)]
    return []


def build_youtube_title(
    base_title: str,
    record_type: str,
    ranges: list[tuple[datetime, datetime]],
) -> str:
    type_ru = record_type_label(record_type)
    prefix = _title_prefix(base_title)
    if not ranges:
        return f"{prefix} | {type_ru}"[:TITLE_MAX_LEN]
    start = ranges[0][0]
    end = ranges[-1][1]
    span = f"{_format_title_start(start)} — {_format_title_end(start, end)}"
    title = f"{prefix} | {type_ru} | {span}"
    if len(title) <= TITLE_MAX_LEN:
        return title
    short_span = f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"
    title = f"{prefix} | {type_ru} | {short_span}"
    return title[:TITLE_MAX_LEN]


def build_youtube_body(
    record_type: str,
    ranges: list[tuple[datetime, datetime]],
    *,
    max_len: int = BODY_MAX_LEN,
) -> str:
    type_ru = record_type_label(record_type)
    header = f"Тип: {type_ru}\n"
    if not ranges:
        return header.strip()

    lines = [header, ""]
    used = len(header) + 1
    omitted = 0
    for idx, (start, end) in enumerate(ranges, start=1):
        line = f"Клип {idx}: {_format_dt(start)} — {_format_dt(end)}\n"
        if used + len(line) > max_len:
            omitted = len(ranges) - idx + 1
            break
        lines.append(line.rstrip("\n"))
        used += len(line)

    if omitted:
        lines.append(
            f"… и ещё {omitted} клипов (список обрезан из‑за лимита YouTube)"
        )
    return "\n".join(lines)


def build_youtube_metadata(
    *,
    base_title: str,
    record_type: str,
    video_dir: Path,
    wall_start: datetime | None = None,
    wall_end: datetime | None = None,
    source: Path | None = None,
    ffprobe: str = "ffprobe",
) -> tuple[str, str, list[tuple[datetime, datetime]]]:
    """Return (title, description/body, clip_ranges)."""
    ranges = collect_clip_ranges(
        video_dir,
        record_type,
        wall_start,
        wall_end,
        source=source,
        ffprobe=ffprobe,
    )
    title = build_youtube_title(base_title, record_type, ranges)
    body = build_youtube_body(record_type, ranges)
    return title, body, ranges
