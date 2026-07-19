#!/usr/bin/env python3
"""Aligned Front/Back timeline: pair source clips into slots so a missing or
short clip in one camera becomes black/silence instead of shifting the whole
stream. Pure data + planning helpers (no ffmpeg), shared by import and compose.

Two modes:

- ``slot`` (Parking/Event): a compressed timeline built from the union of
  source-clip slots. Real multi-month gaps between parking events are NOT
  inserted; each slot's length is the longer of the paired Front/Back clip.
- ``wall`` (Normal): a wall-clock timeline for a trip; leading/internal/
  trailing gaps of one camera are filled with black without compressing time.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

MANIFEST_VERSION = 1
MANIFEST_SUFFIX = ".timeline.json"
# Slots inside one Front/Back capture pair should differ by well under a clip.
PAIR_DRIFT_TOLERANCE_SEC = 2.0


@dataclass(frozen=True)
class ClipEntry:
    """One source clip inside a merged file."""

    key: str  # pairing identity shared by Front/Back, e.g. "20250810085033-000001"
    wall: datetime  # source wall-clock start
    duration: float  # ffprobe duration of the source clip
    offset: float  # media offset of this clip inside the merged file
    source: str  # raw source filename
    merge: str = ""  # merged file name this clip lives in (filled on load)


@dataclass(frozen=True)
class TimelineManifest:
    version: int
    record_type: str
    camera: str
    merge: str
    clips: tuple[ClipEntry, ...]


@dataclass(frozen=True)
class Slot:
    key: str
    wall: datetime
    duration: float
    output_start: float
    front: ClipEntry | None
    back: ClipEntry | None


@dataclass(frozen=True)
class Span:
    """One piece of a single camera lane on the output timeline."""

    kind: str  # "video" | "black"
    output_start: float
    duration: float
    merge: str | None = None  # merged file to read for video spans
    source_ss: float = 0.0  # seek inside the merged file for video spans


def clip_key(timestamp: datetime, sequence: int) -> str:
    return f"{timestamp:%Y%m%d%H%M%S}-{sequence:06d}"


def manifest_path_for(merge_path: Path) -> Path:
    return merge_path.with_name(merge_path.name + MANIFEST_SUFFIX)


def build_manifest(
    *,
    record_type: str,
    camera: str,
    merge_name: str,
    clips: list,
) -> dict:
    """Build a manifest dict from surviving source clips (in merge order).

    ``clips`` is a list of objects with ``timestamp``, ``sequence``,
    ``duration`` and ``path`` attributes (import ``Clip``). Cumulative media
    offset is the running sum of durations (concat -c copy preserves them).
    """
    entries: list[dict] = []
    offset = 0.0
    for clip in clips:
        dur = float(getattr(clip, "duration", 0.0) or 0.0)
        entries.append(
            {
                "key": clip_key(clip.timestamp, clip.sequence),
                "wall": clip.timestamp.isoformat(),
                "dur": round(dur, 3),
                "offset": round(offset, 3),
                "src": clip.path.name,
            }
        )
        offset += dur
    return {
        "version": MANIFEST_VERSION,
        "record_type": record_type,
        "camera": camera,
        "merge": merge_name,
        "clip_count": len(entries),
        "last_clip": entries[-1]["src"] if entries else None,
        "duration_sec": round(offset, 3),
        "clips": entries,
    }


def write_manifest_atomic(merge_path: Path, manifest: dict) -> Path:
    """Write the sidecar manifest atomically next to ``merge_path``."""
    target = manifest_path_for(merge_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    return target


def load_manifest(merge_path: Path) -> TimelineManifest | None:
    path = manifest_path_for(merge_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if int(data.get("version") or 0) != MANIFEST_VERSION:
        return None
    merge_name = data.get("merge") or merge_path.name
    clips: list[ClipEntry] = []
    for raw in data.get("clips", []):
        try:
            clips.append(
                ClipEntry(
                    key=str(raw["key"]),
                    wall=datetime.fromisoformat(raw["wall"]),
                    duration=float(raw["dur"]),
                    offset=float(raw["offset"]),
                    source=str(raw.get("src", "")),
                    merge=merge_name,
                )
            )
        except (KeyError, ValueError, TypeError):
            return None
    return TimelineManifest(
        version=MANIFEST_VERSION,
        record_type=str(data.get("record_type", "")),
        camera=str(data.get("camera", "")),
        merge=merge_name,
        clips=tuple(clips),
    )


def manifest_is_fresh(
    manifest: TimelineManifest | None,
    *,
    expected_clip_count: int | None = None,
    expected_last_clip: str | None = None,
) -> bool:
    """True when the manifest matches the current merge fingerprint."""
    if manifest is None or not manifest.clips:
        return False
    if expected_clip_count is not None and len(manifest.clips) != expected_clip_count:
        return False
    if expected_last_clip:
        if manifest.clips[-1].source != expected_last_clip:
            return False
    return True


def filter_entries_to_window(
    entries: list[ClipEntry],
    wall_start: datetime,
    wall_end: datetime,
    *,
    margin_sec: float = PAIR_DRIFT_TOLERANCE_SEC,
) -> list[ClipEntry]:
    """Keep manifest clips overlapping a compose window (drop other trips/chunks)."""
    lo = wall_start - timedelta(seconds=margin_sec)
    hi = wall_end + timedelta(seconds=margin_sec)
    kept: list[ClipEntry] = []
    for entry in entries:
        entry_end = entry.wall + timedelta(seconds=entry.duration)
        if entry.wall < hi and entry_end > lo:
            kept.append(entry)
    return kept


def build_slots(
    front_clips: list[ClipEntry],
    back_clips: list[ClipEntry],
    *,
    mode: str,
    timeline_start: datetime | None = None,
) -> list[Slot]:
    """Pair Front/Back source clips into output slots.

    ``slot`` mode packs slots back-to-back (Parking/Event); ``wall`` mode places
    each slot at its wall-clock offset from ``timeline_start`` (Normal).
    """
    front_by_key = {c.key: c for c in front_clips}
    back_by_key = {c.key: c for c in back_clips}
    all_keys = set(front_by_key) | set(back_by_key)

    def _wall(key: str) -> datetime:
        entry = front_by_key.get(key) or back_by_key.get(key)
        assert entry is not None
        return entry.wall

    ordered = sorted(all_keys, key=lambda k: (_wall(k), k))
    slots: list[Slot] = []
    cursor = 0.0
    for key in ordered:
        front = front_by_key.get(key)
        back = back_by_key.get(key)
        wall = _wall(key)
        durations = [c.duration for c in (front, back) if c is not None]
        slot_dur = max(durations) if durations else 0.0
        if slot_dur <= 0:
            continue
        if mode == "wall":
            if timeline_start is None:
                timeline_start = wall
            output_start = max(0.0, (wall - timeline_start).total_seconds())
        else:
            output_start = cursor
        slots.append(
            Slot(
                key=key,
                wall=wall,
                duration=slot_dur,
                output_start=output_start,
                front=front,
                back=back,
            )
        )
        cursor = output_start + slot_dur
    return slots


def timeline_duration(slots: list[Slot]) -> float:
    if not slots:
        return 0.0
    last = slots[-1]
    return last.output_start + last.duration


def build_camera_lane(
    slots: list[Slot],
    camera: str,
    *,
    total_duration: float | None = None,
) -> list[Span]:
    """Full output lane for one camera: real video where present, black where
    the camera is missing or short, so every lane has identical length."""
    key = "front" if camera.lower().startswith("f") else "back"
    if total_duration is None:
        total_duration = timeline_duration(slots)
    spans: list[Span] = []
    cursor = 0.0

    def add_black(start: float, dur: float) -> None:
        if dur <= 1e-6:
            return
        if spans and spans[-1].kind == "black" and abs(
            spans[-1].output_start + spans[-1].duration - start
        ) < 1e-6:
            prev = spans.pop()
            spans.append(
                Span(kind="black", output_start=prev.output_start,
                     duration=prev.duration + dur)
            )
        else:
            spans.append(Span(kind="black", output_start=start, duration=dur))

    for slot in slots:
        if slot.output_start > cursor + 1e-6:
            add_black(cursor, slot.output_start - cursor)
            cursor = slot.output_start
        entry = slot.front if key == "front" else slot.back
        if entry is None:
            add_black(cursor, slot.duration)
            cursor += slot.duration
            continue
        vid_dur = min(entry.duration, slot.duration)
        if vid_dur > 1e-6:
            spans.append(
                Span(
                    kind="video",
                    output_start=cursor,
                    duration=vid_dur,
                    merge=entry.merge,
                    source_ss=entry.offset,
                )
            )
            cursor += vid_dur
        if slot.duration - vid_dur > 1e-6:
            add_black(cursor, slot.duration - vid_dur)
            cursor += slot.duration - vid_dur

    if total_duration - cursor > 1e-6:
        add_black(cursor, total_duration - cursor)
    return _coalesce_spans(spans)


def _coalesce_spans(spans: list[Span]) -> list[Span]:
    """Merge adjacent spans that are contiguous in both output and source, so a
    fully-covered lane collapses to a single video span (one ffmpeg input)."""
    out: list[Span] = []
    for span in spans:
        if not out:
            out.append(span)
            continue
        prev = out[-1]
        contiguous_out = abs(prev.output_start + prev.duration - span.output_start) < 1e-6
        if prev.kind == "black" and span.kind == "black" and contiguous_out:
            out[-1] = Span(
                kind="black",
                output_start=prev.output_start,
                duration=prev.duration + span.duration,
            )
            continue
        if (
            prev.kind == "video"
            and span.kind == "video"
            and prev.merge == span.merge
            and contiguous_out
            and abs(prev.source_ss + prev.duration - span.source_ss) < 1e-6
        ):
            out[-1] = Span(
                kind="video",
                output_start=prev.output_start,
                duration=prev.duration + span.duration,
                merge=prev.merge,
                source_ss=prev.source_ss,
            )
            continue
        out.append(span)
    return out


def pair_drift_report(slots: list[Slot]) -> dict:
    """Diagnostics: how many slots miss a camera and max within-pair spread."""
    missing_front = sum(1 for s in slots if s.front is None)
    missing_back = sum(1 for s in slots if s.back is None)
    max_pair_spread = 0.0
    for slot in slots:
        if slot.front is not None and slot.back is not None:
            spread = abs(slot.front.duration - slot.back.duration)
            max_pair_spread = max(max_pair_spread, spread)
    return {
        "slots": len(slots),
        "missing_front": missing_front,
        "missing_back": missing_back,
        "max_pair_spread": round(max_pair_spread, 3),
        "duration": round(timeline_duration(slots), 3),
    }


def lane_black_seconds(spans: list[Span]) -> float:
    return round(sum(s.duration for s in spans if s.kind == "black"), 3)


def lane_duration(spans: list[Span]) -> float:
    if not spans:
        return 0.0
    last = spans[-1]
    return round(last.output_start + last.duration, 3)


def max_contiguous_black(spans: list[Span]) -> float:
    return round(
        max((s.duration for s in spans if s.kind == "black"), default=0.0), 3
    )


MANIFEST_DURATION_TOLERANCE_RATIO = 0.02


def manifest_matches_merge(merge_path: Path) -> bool:
    """True when a fresh timeline sidecar matches the merge file duration."""
    if not merge_path.is_file():
        return False
    try:
        from compose_70mai import probe_duration

        manifest = load_manifest(merge_path)
        if manifest is None or not manifest.clips:
            return False
        declared = sum(c.duration for c in manifest.clips)
        actual = probe_duration(merge_path)
        if actual <= 0:
            return False
        return abs(declared - actual) <= max(
            2.0, actual * MANIFEST_DURATION_TOLERANCE_RATIO
        )
    except (OSError, RuntimeError, ValueError):
        return False


def merges_timeline_ready(video_dir: Path, record_type: str) -> tuple[bool, str]:
    """True when every Front/Back merge has a loadable timeline manifest."""
    from compose_70mai import scan_merged_clips

    for camera in ("Front", "Back"):
        clips = scan_merged_clips(
            video_dir, camera, record_type=record_type, probe=False
        )
        if not clips:
            return False, f"missing {record_type}/{camera} merges"
        for clip in clips:
            if not manifest_matches_merge(clip.path):
                return (
                    False,
                    f"{clip.path.name} missing or stale timeline manifest",
                )
    return True, ""
