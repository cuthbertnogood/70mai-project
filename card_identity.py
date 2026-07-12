#!/usr/bin/env python3
"""SD card identity: stable card_id + clip signature (new card vs same card, new footage)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from card_storage_stats import CAMERAS, VIDEO_RECORD_TYPES
from import_70mai import log
from publish_state import read_card_id, sd_root_dir

CARD_META_JSON = "card_meta.json"
CARD_LABEL_FILENAME = "card_label.txt"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_mp4_inventory(path: Path) -> tuple[int, str | None, str | None]:
    """Count MP4s and return first/last filename (70mai names sort chronologically)."""
    names: list[str] = []
    if not path.is_dir():
        return 0, None, None
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.lower().endswith(".mp4"):
                    names.append(entry.name)
    except OSError:
        return 0, None, None
    if not names:
        return 0, None, None
    names.sort()
    return len(names), names[0], names[-1]


def collect_clip_signature(source: Path) -> dict:
    """Lightweight clip inventory fingerprint (no ffprobe)."""
    source = source.resolve()
    types: dict[str, dict] = {}
    parts: list[str] = []
    for record_type in VIDEO_RECORD_TYPES:
        block: dict[str, object] = {"cameras": {}, "total_files": 0}
        for camera in CAMERAS:
            count, first, last = _dir_mp4_inventory(source / record_type / camera)
            block["cameras"][camera] = {
                "files": count,
                "first": first,
                "last": last,
            }
            block["total_files"] = int(block["total_files"]) + count
        types[record_type] = block
        front = block["cameras"]["Front"]
        back = block["cameras"]["Back"]
        parts.append(
            f"{record_type}:{front['files']}+{back['files']}"
            f"@{front.get('first') or '-'}/{front.get('last') or '-'}"
        )
    return {
        "types": types,
        "fingerprint": "|".join(parts),
        "total_clips": sum(int(t["total_files"]) for t in types.values()),
    }


def sd_card_meta_path(source: Path) -> Path:
    return sd_root_dir(source) / CARD_META_JSON


def sd_card_label_path(source: Path) -> Path:
    return sd_root_dir(source) / CARD_LABEL_FILENAME


def read_card_label(source: Path) -> str | None:
    path = sd_card_label_path(source)
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def read_card_meta(source: Path) -> dict | None:
    path = sd_card_meta_path(source)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clip_signature_delta(old: dict | None, new: dict) -> dict[str, dict]:
    """Per-type clip count change (positive = new footage on same card)."""
    if not old:
        return {}
    changes: dict[str, dict] = {}
    for record_type in VIDEO_RECORD_TYPES:
        old_files = int(
            old.get("types", {}).get(record_type, {}).get("total_files", 0)
        )
        new_files = int(
            new.get("types", {}).get(record_type, {}).get("total_files", 0)
        )
        if new_files != old_files:
            changes[record_type] = {
                "old_files": old_files,
                "new_files": new_files,
                "delta": new_files - old_files,
            }
    return changes


def describe_card_status(
    *,
    card_id: str | None,
    label: str | None,
    previous: dict | None,
    signature: dict,
) -> list[str]:
    """Human-readable lines for autopilot log."""
    short = f"{card_id[:8]}…" if card_id else "?"
    name = f' "{label}"' if label else ""
    lines: list[str] = []

    if not previous or previous.get("card_id") != card_id:
        lines.append(f"SD card: NEW (ID {short}{name})")
        lines.append(
            f"  Clips on card: {signature.get('total_clips', 0)} "
            f"({signature.get('fingerprint', '')})"
        )
        return lines

    lines.append(f"SD card: known (ID {short}{name})")
    prev_sig = previous.get("clip_signature") or {}
    if prev_sig.get("fingerprint") == signature.get("fingerprint"):
        lines.append("  Clip inventory: unchanged since last run")
        return lines

    delta = clip_signature_delta(prev_sig, signature)
    if not delta:
        lines.append("  Clip inventory: changed (period range update)")
        return lines

    parts: list[str] = []
    for record_type, change in sorted(delta.items()):
        d = change["delta"]
        if d > 0:
            parts.append(f"{record_type} +{d} clips")
        elif d < 0:
            parts.append(f"{record_type} {d} clips")
        else:
            parts.append(f"{record_type} unchanged")
    lines.append(f"  New footage on same card: {', '.join(parts)}")
    uploaded = int(previous.get("uploaded_trips", 0))
    if uploaded:
        lines.append(
            f"  Publish state: {uploaded} trip(s) already on YouTube — "
            "only pending trips will upload"
        )
    return lines


def count_uploaded_trips(source: Path) -> int:
    from publish_state import sd_publish_dir

    pub = sd_publish_dir(source)
    if not pub.is_dir():
        return 0
    total = 0
    for path in pub.glob("publish_*.state.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        total += sum(1 for p in data.get("trip_parts", []) if p.get("uploaded"))
        total += sum(1 for p in data.get("parts", []) if p.get("uploaded"))
    return total


def host_session_stale(source: Path, card_id: str | None, temp_dir: Path) -> bool:
    """True when host dashboard cache should be dropped (new card / stale done)."""
    from publish_state import (
        HOST_STATUS_FILENAME,
        is_uploaded_on_sd,
        read_host_session_card_id,
    )

    previous = read_card_meta(source)
    has_session_files = (
        (temp_dir / HOST_STATUS_FILENAME).is_file()
        or (temp_dir / "autopilot_trip_reasons.json").is_file()
        or read_host_session_card_id(temp_dir) is not None
    )
    if card_id and (not previous or previous.get("card_id") != card_id):
        return has_session_files

    session_id = read_host_session_card_id(temp_dir)
    if card_id and session_id and session_id != card_id:
        return True

    status_path = temp_dir / HOST_STATUS_FILENAME
    if not status_path.is_file():
        return False
    try:
        st = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(st, dict):
        return False

    if card_id and st.get("card_id") and st["card_id"] != card_id:
        return True

    if st.get("phase") == "done" and st.get("youtube_url"):
        record_type = str(st.get("record_type") or "")
        chunk_index = int(st.get("chunk_index") or 0)
        trip_index = int(st.get("trip_index") or 0)
        if record_type and not is_uploaded_on_sd(
            source, record_type, chunk_index, trip_index
        ):
            return True
    return False


def refresh_card_identity(source: Path, card_id: str | None) -> dict | None:
    """Update /.70mai/card_meta.json and log new-card vs same-card+new-clips."""
    if not card_id:
        return None
    try:
        previous = read_card_meta(source)
        signature = collect_clip_signature(source)
        label = read_card_label(source)
        uploaded = count_uploaded_trips(source)

        for line in describe_card_status(
            card_id=card_id,
            label=label,
            previous=previous,
            signature=signature,
        ):
            log(line)

        meta = {
            "updated_at": _utc_now(),
            "card_id": card_id,
            "label": label,
            "clip_signature": signature,
            "uploaded_trips": uploaded,
        }
        sd_root_dir(source).mkdir(parents=True, exist_ok=True)
        sd_card_meta_path(source).write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return meta
    except OSError as exc:
        log(f"Warning: cannot update card identity on SD ({exc})")
        return None
