#!/usr/bin/env python3
"""Import/merge state and card inventory on SD card (.70mai/import/)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from import_70mai import Clip, format_duration, log, output_name, split_chunks, split_sessions
from plan_estimate import Trip, build_plan
from publish_state import ensure_sd_readme

SD_IMPORT_DIR = ".70mai/import"
INVENTORY_FILENAME = "card_inventory.json"
SUMMARY_FILENAME = "CARD_SUMMARY.txt"


def sd_import_dir(source: Path) -> Path:
    return source.resolve() / SD_IMPORT_DIR


def import_state_filename(label: str) -> str:
    safe = label.replace(" ", "_").replace("/", "-")
    return f"import_{safe}.state.json"


def sd_inventory_path(source: Path) -> Path:
    return sd_import_dir(source) / INVENTORY_FILENAME


def sd_summary_path(source: Path) -> Path:
    return sd_import_dir(source) / SUMMARY_FILENAME


def sd_import_state_path(source: Path, label: str) -> Path:
    return sd_import_dir(source) / import_state_filename(label)


def local_import_state_path(temp_dir: Path, label: str) -> Path:
    return temp_dir / import_state_filename(label)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trip_dict(trip: Trip) -> dict:
    return {
        "index": trip.index,
        "record_type": trip.record_type,
        "start": trip.start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": trip.end.strftime("%Y-%m-%d %H:%M:%S"),
        "clip_count": trip.clip_count,
        "duration_sec": round(trip.duration_sec, 1),
        "duration": format_duration(trip.duration_sec),
    }


def _merge_key(record_type: str, camera: str, filename: str) -> str:
    return f"{record_type}/{camera}/{filename}"


def render_card_summary(data: dict) -> str:
    lines = [
        "70mai SD card inventory",
        "=======================",
        f"Updated: {data.get('updated_at', '—')}",
        f"Card:    {data.get('source', '—')}",
        "",
    ]
    merge_stats = data.get("merge_stats", {})
    if merge_stats:
        lines.extend(
            [
                "Import merge (host video/Output/):",
                (
                    f"  merged {merge_stats.get('merged', 0)}, "
                    f"skipped {merge_stats.get('skipped', 0)}, "
                    f"failed {merge_stats.get('failed', 0)}, "
                    f"pending {merge_stats.get('pending', 0)}"
                ),
                f"  last run: {merge_stats.get('last_run', '—')}",
                "",
            ]
        )

    for record_type, block in data.get("record_types", {}).items():
        clips = block.get("clips", {})
        period = block.get("period", {})
        lines.append(f"=== {record_type} ===")
        lines.append(
            f"Clips: Front {clips.get('Front', 0)}, Back {clips.get('Back', 0)}"
        )
        if period.get("from") and period.get("to"):
            lines.append(f"Period: {period['from']} -> {period['to']}")
        trip_count = block.get("trip_count")
        if trip_count is not None:
            lines.append(f"Trips (sessions): {trip_count}")
        dur = block.get("duration_2cam")
        if dur:
            lines.append(f"2-cam duration: {dur}")
        pub = block.get("publish_chunks")
        if pub is not None:
            lines.append(f"YouTube chunks (~120 min): {pub}")
        lines.append("")

        trips = block.get("trips", [])
        if trips:
            lines.append("Trips:")
            for trip in trips:
                dur_s = trip.get("duration") or "?"
                lines.append(
                    f"  {trip['index']:2d}. {trip['start']} -> {trip['end']}  "
                    f"({dur_s}, {trip.get('clip_count', '?')} clips)"
                )
            lines.append("")

        merge_plan = block.get("merge_outputs", {})
        for camera in ("Front", "Back"):
            outputs = merge_plan.get(camera, {})
            if not outputs:
                continue
            merged = sum(1 for v in outputs.values() if v.get("status") == "merged")
            skipped = sum(1 for v in outputs.values() if v.get("status") == "skipped")
            pending = sum(
                1
                for v in outputs.values()
                if v.get("status") in (None, "pending", "planned")
            )
            lines.append(
                f"Merge {camera}: {len(outputs)} file(s) — "
                f"done {merged + skipped}, pending {pending}"
            )
        lines.append("")

    lines.append(
        "Portable data: .70mai/auth (OAuth), .70mai/publish (YouTube state), "
        ".70mai/import (this file)."
    )
    return "\n".join(lines)


class ImportStateStore:
    """Card inventory + per-file merge status on SD (and local cache)."""

    def __init__(
        self,
        source: Path,
        label: str,
        *,
        state_on_sd: bool,
        local_dir: Path,
        chunk_minutes: float,
        gap_seconds: float,
    ) -> None:
        self.source = source.resolve()
        self.label = label
        self.state_on_sd = state_on_sd
        self.chunk_minutes = chunk_minutes
        self.gap_seconds = gap_seconds
        self.local_dir = local_dir
        self.local_path = local_import_state_path(local_dir, label)
        self.sd_path = sd_import_state_path(source, label) if state_on_sd else None
        self.inventory_path = sd_inventory_path(source) if state_on_sd else None
        self.summary_path = sd_summary_path(source) if state_on_sd else None
        self._data = self._load()

    def _load(self) -> dict:
        for path in (self.sd_path, self.local_path):
            if path and path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        return {
            "source": str(self.source),
            "label": self.label,
            "chunk_minutes": self.chunk_minutes,
            "gap_seconds": self.gap_seconds,
            "files": {},
            "merge_stats": {},
        }

    def _save(self) -> None:
        self._data["updated_at"] = _utc_now()
        self._data["merge_stats"] = self._recompute_merge_stats()
        payload = json.dumps(self._data, indent=2, ensure_ascii=False)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text(payload, encoding="utf-8")
        if not self.state_on_sd or self.sd_path is None:
            return
        try:
            sd_import_dir(self.source).mkdir(parents=True, exist_ok=True)
            ensure_sd_readme(self.source)
            self.sd_path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            log(f"Warning: cannot write import state to SD ({exc})")

    def _recompute_merge_stats(self) -> dict:
        files = self._data.get("files", {})
        stats = {"merged": 0, "skipped": 0, "failed": 0, "planned": 0, "pending": 0}
        for entry in files.values():
            status = entry.get("status", "pending")
            if status in stats:
                stats[status] += 1
            else:
                stats["pending"] += 1
        stats["last_run"] = self._data.get("updated_at")
        return stats

    def refresh_inventory(
        self,
        *,
        types: list[str],
        ffprobe: str,
        publish_chunk_minutes: float = 120.0,
    ) -> None:
        """Full trip/chunk inventory from SD (uses ffprobe). Writes JSON + CARD_SUMMARY.txt."""
        if not self.state_on_sd or self.inventory_path is None:
            return

        log("Building card inventory on SD...")
        trips, chunks, dur_by_type = build_plan(
            self.source,
            types,
            chunk_minutes=publish_chunk_minutes,
            chunk_mode="trips",
            session_gap=self.gap_seconds,
            ffprobe=ffprobe,
        )
        self.save_inventory_from_plan(
            types=types,
            trips=trips,
            chunks=chunks,
            dur_by_type=dur_by_type,
        )

    def save_inventory_from_plan(
        self,
        *,
        types: list[str],
        trips: list[Trip],
        chunks: list,
        dur_by_type: dict[str, float],
    ) -> None:
        """Write card inventory from build_plan result (no extra ffprobe)."""
        if not self.state_on_sd or self.inventory_path is None:
            return

        inventory: dict = {
            "updated_at": _utc_now(),
            "source": str(self.source),
            "types": types,
            "record_types": {},
            "merge_stats": self._recompute_merge_stats(),
        }

        for record_type in types:
            type_trips = [t for t in trips if t.record_type == record_type]
            type_chunks = [c for c in chunks if c.record_type == record_type]
            front_clips = self._scan_type_camera(record_type, "Front")
            back_clips = self._scan_type_camera(record_type, "Back")
            period_from = None
            period_to = None
            all_clips = front_clips + back_clips
            if all_clips:
                period_from = min(c.timestamp for c in all_clips).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                period_to = max(c.timestamp for c in all_clips).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            inventory["record_types"][record_type] = {
                "clips": {"Front": len(front_clips), "Back": len(back_clips)},
                "period": {"from": period_from, "to": period_to},
                "trip_count": len(type_trips),
                "duration_2cam": format_duration(dur_by_type.get(record_type, 0.0)),
                "publish_chunks": len(type_chunks),
                "trips": [_trip_dict(t) for t in type_trips],
                "merge_outputs": {},
            }

        self._write_inventory(inventory)
        log(f"Card inventory: {self.inventory_path}")
        log(f"Card summary:   {self.summary_path}")

    def _scan_type_camera(self, record_type: str, camera: str) -> list[Clip]:
        from import_70mai import scan_clips

        return scan_clips(self.source, [record_type], [camera], warn=False)

    def _write_inventory(self, inventory: dict) -> None:
        if not self.inventory_path or not self.summary_path:
            return
        inventory["merge_stats"] = self._recompute_merge_stats()
        try:
            sd_import_dir(self.source).mkdir(parents=True, exist_ok=True)
            ensure_sd_readme(self.source)
            self.inventory_path.write_text(
                json.dumps(inventory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.summary_path.write_text(
                render_card_summary(inventory), encoding="utf-8"
            )
        except OSError as exc:
            log(f"Warning: cannot write card inventory to SD ({exc})")

    def sync_inventory_merge_status(self) -> None:
        """Push per-file merge status from import state into card_inventory.json."""
        if not self.state_on_sd or not self.inventory_path or not self.inventory_path.is_file():
            return
        try:
            inventory = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        files = self._data.get("files", {})
        for record_type, block in inventory.get("record_types", {}).items():
            merge_outputs = block.get("merge_outputs", {})
            for camera, outputs in merge_outputs.items():
                for name in list(outputs.keys()):
                    key = _merge_key(record_type, camera, name)
                    if key in files:
                        outputs[name]["status"] = files[key].get("status", "pending")
                        if files[key].get("size_mb") is not None:
                            outputs[name]["size_mb"] = files[key]["size_mb"]
        inventory["merge_stats"] = self._recompute_merge_stats()
        inventory["updated_at"] = _utc_now()
        self._write_inventory(inventory)

    def update_merge_plan(
        self,
        groups: list[tuple[str, str, list[Clip]]],
        duration_cache: dict[Path, float],
    ) -> None:
        """Register expected merge output files (after ffprobe)."""
        chunk_seconds = self.chunk_minutes * 60.0
        merge_by_type: dict[str, dict[str, dict[str, dict]]] = {}

        for record_type, camera, clips in groups:
            sessions = split_sessions(clips, self.gap_seconds)
            cam_merge: dict[str, dict] = {}
            for session_idx, session in enumerate(sessions, start=1):
                session_with_duration = [
                    Clip(
                        path=c.path,
                        record_type=c.record_type,
                        camera=c.camera,
                        timestamp=c.timestamp,
                        sequence=c.sequence,
                        duration=duration_cache.get(c.path, 60.0),
                    )
                    for c in session
                ]
                for chunk in split_chunks(session_with_duration, chunk_seconds):
                    name = output_name(chunk)
                    key = _merge_key(record_type, camera, name)
                    existing = self._data.setdefault("files", {}).get(key, {})
                    cam_merge[name] = {
                        "status": existing.get("status", "pending"),
                        "session_index": session_idx,
                        "clip_count": len(chunk),
                    }
                    if key not in self._data["files"]:
                        self._data["files"][key] = {
                            "status": "pending",
                            "session_index": session_idx,
                            "clip_count": len(chunk),
                            "updated_at": _utc_now(),
                        }
            merge_by_type.setdefault(record_type, {})[camera] = cam_merge

        if self.inventory_path and self.inventory_path.is_file():
            try:
                inventory = json.loads(self.inventory_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                inventory = {"record_types": {}}
            for record_type, cameras in merge_by_type.items():
                block = inventory.setdefault("record_types", {}).setdefault(record_type, {})
                block["merge_outputs"] = cameras
            self._write_inventory(inventory)

    def record_merge(
        self,
        *,
        record_type: str,
        camera: str,
        filename: str,
        status: str,
        session_idx: int = 0,
        clip_count: int = 0,
        size_mb: float | None = None,
        elapsed_sec: float | None = None,
    ) -> None:
        key = _merge_key(record_type, camera, filename)
        entry = {
            "status": status,
            "session_index": session_idx,
            "clip_count": clip_count,
            "updated_at": _utc_now(),
        }
        if size_mb is not None:
            entry["size_mb"] = round(size_mb, 1)
        if elapsed_sec is not None:
            entry["elapsed_sec"] = round(elapsed_sec, 1)
        self._data.setdefault("files", {})[key] = entry
        if status != "skipped":
            self._save()

    def finalize(self) -> None:
        self._save()
        self.sync_inventory_merge_status()
        if self.state_on_sd and self.summary_path and self.summary_path.is_file():
            log(f"Import state on SD: {self.sd_path}")
