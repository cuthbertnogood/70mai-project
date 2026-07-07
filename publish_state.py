#!/usr/bin/env python3
"""Publish state on SD card — portable upload progress across hosts."""

from __future__ import annotations

import json
from pathlib import Path

from import_70mai import log

SD_PUBLISH_DIR = ".70mai/publish"
SD_SESSIONS_SUBDIR = "sessions"
SD_README = """70mai publish state (auto-generated)
================================
publish_*.state.json  — which trips are uploaded to YouTube (video_id per trip)
sessions/*.upload.json — resume data for interrupted uploads (~few KB each)

Insert this SD card on any Mac with the project + run:
  ./scripts/publish_all_70mai.sh --wait

Raw clips stay on the card; composed MP4s are temporary on the host and deleted after upload.
"""


def state_filename(label: str) -> str:
    safe = label.replace(" ", "_").replace("/", "-")
    return f"publish_{safe}.state.json"


def sd_publish_dir(source: Path) -> Path:
    return source.resolve() / SD_PUBLISH_DIR


def sd_state_path(source: Path, label: str) -> Path:
    return sd_publish_dir(source) / state_filename(label)


def local_state_path(temp_dir: Path, label: str) -> Path:
    return temp_dir / state_filename(label)


def sd_session_dir(source: Path) -> Path:
    return sd_publish_dir(source) / SD_SESSIONS_SUBDIR


def _trip_key(part: dict) -> tuple:
    return (
        part.get("record_type"),
        part.get("chunk_index"),
        part.get("trip_index"),
    )


def _chunk_key(part: dict) -> tuple:
    return (part.get("record_type"), part.get("index"))


def _merge_parts(
    sd_parts: list[dict],
    local_parts: list[dict],
    key_fn,
) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    for part in local_parts + sd_parts:
        key = key_fn(part)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = part
            continue
        if part.get("uploaded") and not existing.get("uploaded"):
            by_key[key] = part
        elif part.get("uploaded") and existing.get("uploaded"):
            if part.get("video_id") and not existing.get("video_id"):
                by_key[key] = part
    return list(by_key.values())


def merge_publish_state(sd: dict, local: dict) -> dict:
    """Merge SD + local state; SD wins ties on uploaded trips."""
    merged = dict(local)
    for key in ("source", "types", "chunk_minutes", "chunk_mode", "updated_at"):
        if sd.get(key) is not None:
            merged[key] = sd[key]
    merged["trip_parts"] = _merge_parts(
        sd.get("trip_parts", []),
        local.get("trip_parts", []),
        _trip_key,
    )
    merged["parts"] = _merge_parts(
        sd.get("parts", []),
        local.get("parts", []),
        _chunk_key,
    )
    if sd.get("playlist_id"):
        merged["playlist_id"] = sd["playlist_id"]
    elif local.get("playlist_id"):
        merged["playlist_id"] = local["playlist_id"]
    return merged


def load_state_file(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_sd_readme(source: Path) -> None:
    readme = sd_publish_dir(source) / "README.txt"
    if not readme.is_file():
        try:
            readme.write_text(SD_README, encoding="utf-8")
        except OSError:
            pass


class StateStore:
    """Read/write publish state on host + SD card (SD is portable source of truth)."""

    def __init__(
        self,
        source: Path,
        temp_dir: Path,
        label: str,
        *,
        state_on_sd: bool,
    ) -> None:
        self.source = source.resolve()
        self.temp_dir = temp_dir
        self.label = label
        self.state_on_sd = state_on_sd
        self.local_path = local_state_path(temp_dir, label)
        self.sd_path = sd_state_path(source, label) if state_on_sd else None

    @property
    def primary_path(self) -> Path:
        if self.state_on_sd and self.sd_path is not None:
            return self.sd_path
        return self.local_path

    @property
    def session_dir(self) -> Path:
        if self.state_on_sd:
            return sd_session_dir(self.source)
        return self.temp_dir

    def load(self, *, resume: bool) -> dict:
        if not resume:
            return {}
        local = load_state_file(self.local_path)
        if not self.state_on_sd or self.sd_path is None:
            if local:
                log(f"State (local): {self.local_path}")
            return local

        sd = load_state_file(self.sd_path)
        if sd and local:
            merged = merge_publish_state(sd, local)
            log(
                f"State merged: SD {self.sd_path} + local "
                f"({len(merged.get('trip_parts', []))} trip record(s))"
            )
        elif sd:
            merged = sd
            log(f"State (SD): {self.sd_path} ({len(sd.get('trip_parts', []))} trip record(s))")
        elif local:
            merged = local
            log(f"Migrating local state → SD: {self.sd_path}")
        else:
            return {}

        return merged

    def save(self, data: dict) -> None:
        from datetime import datetime, timezone

        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state_file(self.local_path, data)
        if not self.state_on_sd or self.sd_path is None:
            return
        try:
            ensure_sd_readme(self.source)
            save_state_file(self.sd_path, data)
        except OSError as exc:
            log(f"Warning: cannot write state to SD ({exc})")

    def uploaded_count(self) -> int:
        data = merge_publish_state(
            load_state_file(self.sd_path) if self.sd_path else {},
            load_state_file(self.local_path),
        )
        return sum(1 for p in data.get("trip_parts", []) if p.get("uploaded"))
