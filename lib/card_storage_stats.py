#!/usr/bin/env python3
"""SD card storage stats — video by type, non-video, free space → .70mai/import/."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from import_70mai import format_file_size, log
from import_state import sd_import_dir

VIDEO_RECORD_TYPES = ("Normal", "Event", "Parking")
CAMERAS = ("Front", "Back")
CARD_STORAGE_TXT = "CARD_STORAGE.txt"
CARD_STORAGE_JSON = "card_storage.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_mp4_stats(path: Path) -> tuple[int, int]:
    count = 0
    total = 0
    if not path.is_dir():
        return count, total
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith(".mp4"):
                    continue
                count += 1
                try:
                    total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return count, total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _tree_bytes(path: Path) -> int:
    total = 0
    try:
        if path.is_file():
            return _file_size(path)
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    total += (Path(root) / name).stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def collect_card_storage_stats(source: Path) -> dict:
    """Scan SD layout (fast: typed video dirs + root non-video only)."""
    source = source.resolve()
    video: dict[str, dict] = {}
    video_total = 0

    for record_type in VIDEO_RECORD_TYPES:
        block: dict[str, object] = {"cameras": {}, "total_bytes": 0, "total_files": 0}
        for camera in CAMERAS:
            count, size = _dir_mp4_stats(source / record_type / camera)
            block["cameras"][camera] = {"files": count, "bytes": size}
            block["total_bytes"] = int(block["total_bytes"]) + size
            block["total_files"] = int(block["total_files"]) + count
        video[record_type] = block
        video_total += int(block["total_bytes"])

    non_video: list[dict[str, object]] = []
    skip_names = set(VIDEO_RECORD_TYPES) | {
        "DCIM",
        "Android",
        "LOST.DIR",
        "Alarms",
        "Audiobooks",
        "Documents",
        "Download",
        "Movies",
        "Music",
        "Notifications",
        "Pictures",
        "Podcasts",
        "Ringtones",
    }

    for path in sorted(source.iterdir(), key=lambda p: p.name.lower()):
        name = path.name
        if name in skip_names:
            continue
        if name.startswith("GPSData") and path.is_file():
            size = _file_size(path)
            if size:
                non_video.append({"name": name, "bytes": size, "kind": "gps"})
            continue
        if name == "Lapse" or name == "Photo":
            size = _tree_bytes(path)
            if size:
                non_video.append(
                    {"name": name, "bytes": size, "kind": "dashcam-other"}
                )
            continue
        if name.startswith("."):
            size = _tree_bytes(path)
            if size:
                kind = "meta" if name == ".70mai" else "meta"
                non_video.append({"name": name, "bytes": size, "kind": kind})
            continue
        if path.is_file():
            size = _file_size(path)
            if size:
                non_video.append({"name": name, "bytes": size, "kind": "file"})

    non_video_total = sum(int(item["bytes"]) for item in non_video)
    usage = shutil.disk_usage(source)

    return {
        "updated_at": _utc_now(),
        "source": str(source),
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "capacity_pct": round(100 * usage.used / usage.total) if usage.total else 0,
        },
        "video": video,
        "video_total_bytes": video_total,
        "non_video": non_video,
        "non_video_total_bytes": non_video_total,
    }


def render_card_storage_text(data: dict) -> str:
    disk = data["disk"]
    lines = [
        "70mai SD card storage",
        "=====================",
        f"Updated: {data.get('updated_at', '—')}",
        f"Card:    {data.get('source', '—')}",
        "",
        (
            f"Disk: {format_file_size(disk['total_bytes'])} total, "
            f"{format_file_size(disk['used_bytes'])} used, "
            f"{format_file_size(disk['free_bytes'])} free "
            f"({disk.get('capacity_pct', 0)}%)"
        ),
        "",
        "Video on card (source MP4):",
    ]

    for record_type in VIDEO_RECORD_TYPES:
        block = data.get("video", {}).get(record_type, {})
        cameras = block.get("cameras", {})
        lines.append(f"=== {record_type} ===")
        for camera in CAMERAS:
            cam = cameras.get(camera, {})
            lines.append(
                f"  {camera:5} {format_file_size(cam.get('bytes', 0)):>8}  "
                f"({cam.get('files', 0)} files)"
            )
        lines.append(
            f"  Total {format_file_size(block.get('total_bytes', 0)):>8}  "
            f"({block.get('total_files', 0)} files)"
        )
        lines.append("")

    lines.append(
        f"All video types: {format_file_size(data.get('video_total_bytes', 0))}"
    )
    lines.append("")
    lines.append("Non-video on card:")
    for item in data.get("non_video", []):
        lines.append(
            f"  {item['name']:24} {format_file_size(item['bytes']):>8}  "
            f"({item.get('kind', '?')})"
        )
    lines.append(
        f"  Total non-video: {format_file_size(data.get('non_video_total_bytes', 0))}"
    )
    lines.append("")
    lines.append(
        "Machine-readable: .70mai/import/card_storage.json "
        "(refreshed each autopilot run)."
    )
    return "\n".join(lines) + "\n"


def write_card_storage_stats(source: Path) -> Path | None:
    """Write CARD_STORAGE.txt + card_storage.json on SD. Returns txt path."""
    try:
        from publish_state import ensure_sd_readme

        data = collect_card_storage_stats(source)
        text = render_card_storage_text(data)
        out_dir = sd_import_dir(source)
        out_dir.mkdir(parents=True, exist_ok=True)
        ensure_sd_readme(source)
        txt_path = out_dir / CARD_STORAGE_TXT
        json_path = out_dir / CARD_STORAGE_JSON
        txt_path.write_text(text, encoding="utf-8")
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return txt_path
    except OSError as exc:
        log(f"Warning: cannot write card storage stats on SD ({exc})")
        return None
