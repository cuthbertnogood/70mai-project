#!/usr/bin/env python3
"""Import and merge 70mai dash cam clips from SD card into longer videos."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from queue import Queue

FILENAME_RE = re.compile(
    r"^(NO|EV|PA|LA)(\d{8})-(\d{6})-(\d+)([FB])\.MP4$",
    re.IGNORECASE,
)
PHOTO_RE = re.compile(
    r"^PH(\d{8})-(\d{6})-(\d+)([FB])\.JPG$",
    re.IGNORECASE,
)

RECORD_TYPES = ("Normal", "Event", "Parking")
ALL_SCAN_TYPES = ("Normal", "Event", "Parking", "Lapse")
CAMERAS = ("Front", "Back")
TYPE_PREFIX = {
    "Normal": "NO",
    "Event": "EV",
    "Parking": "PA",
    "Lapse": "LA",
}
RECORD_TYPE_INFO: dict[str, tuple[str, str, str]] = {
    "Normal": ("NO", "MP4", "Continuous loop recording (~1 min clips)"),
    "Event": ("EV", "MP4", "Impact / collision / manual save events"),
    "Parking": ("PA", "MP4", "Parking mode recordings"),
    "Lapse": ("LA", "MP4", "Timelapse recordings"),
    "Photo": ("PH", "JPG", "Snapshot photos"),
}
MIN_GPS_TIMESTAMP = 1577836800  # 2020-01-01 — ignore zero/invalid points
BAR_WIDTH = 36
PROBE_WORKERS = 8
SKIP_BATCH_LOG = 5  # batch "skip (exists)" lines in log files
MERGE_MAX_ATTEMPTS = 3
MERGE_RETRY_DELAY_SEC = 3.0
MIN_MERGE_BYTES = 10_000
MERGE_DURATION_TOLERANCE = 0.85  # merged file must be >= 85% of source clips sum
MERGE_WORKERS = 1  # default 1: USB/SD seeks worse with parallel concat
MERGE_HEARTBEAT_SEC = 30.0  # log while ffmpeg concat is silent
PREFETCH_BLOCK = 4 * 1024 * 1024  # sequential read block for page-cache warmup
# Prefetch only helps when a single merge worker is reading the SD sequentially.
LOG_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def format_log_line(msg: str) -> str:
    """Prefix a log message with a wall-clock timestamp (empty lines unchanged)."""
    if not msg:
        return ""
    return f"{datetime.now().strftime(LOG_TIME_FMT)} {msg}"


def log(msg: str) -> None:
    print(format_log_line(msg), flush=True)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def run_quiet_subprocess(
    cmd: list[str],
    *,
    heartbeat: str | None = None,
    heartbeat_sec: float = MERGE_HEARTBEAT_SEC,
    on_heartbeat: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with captured output; optional periodic heartbeat logs."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    start = time.monotonic()
    next_beat = start + heartbeat_sec if heartbeat else float("inf")
    while proc.poll() is None:
        now = time.monotonic()
        if heartbeat and now >= next_beat:
            elapsed = now - start
            log(f"       … {heartbeat} ({format_duration(elapsed)})")
            if on_heartbeat is not None:
                try:
                    on_heartbeat(elapsed)
                except Exception:
                    pass
            next_beat = now + heartbeat_sec
        time.sleep(1)
    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout, stderr)


def format_bar(ratio: float, width: int = BAR_WIDTH) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(width * ratio)
    return "█" * filled + "░" * (width - filled)


def is_tty() -> bool:
    return sys.stderr.isatty()


class ProgressTracker:
    def __init__(self, total: int, label: str) -> None:
        self.total = max(total, 1)
        self.label = label
        self.done = 0
        self.start = time.monotonic()
        self._last_logged_pct = -1
        self._lock = threading.Lock()
        self._render(force_log=True)

    def adjust_total(self, total: int) -> None:
        self.total = max(total, self.done, 1)

    def update(self, detail: str = "") -> None:
        with self._lock:
            self.done += 1
        self._render(detail=detail)

    def _render(self, detail: str = "", force_log: bool = False) -> None:
        elapsed = time.monotonic() - self.start
        pct = 100 * self.done / self.total
        rate = self.done / elapsed if elapsed > 0 else 0.0
        remaining = self.total - self.done
        eta = remaining / rate if rate > 0 else 0.0
        bar = format_bar(self.done / self.total)
        line = (
            f"{self.label}: [{bar}] {self.done}/{self.total} ({pct:.1f}%) "
            f"| {format_duration(elapsed)} elapsed | ETA {format_duration(eta)}"
        )
        if detail:
            short = detail if len(detail) <= 48 else "..." + detail[-45:]
            line += f" | {short}"

        if is_tty() and not force_log:
            sys.stderr.write("\r\033[K" + line)
            sys.stderr.flush()
            return

        pct_bucket = int(pct)
        if (
            force_log
            or self.done == self.total
            or pct_bucket > self._last_logged_pct
            or self.done == 1
        ):
            log(line)
            self._last_logged_pct = pct_bucket

    def finish(self) -> None:
        if is_tty():
            sys.stderr.write("\n")
            sys.stderr.flush()


class PipelineProgress:
    """Overall progress across probing and merging."""

    def __init__(self, probe_total: int, merge_total: int) -> None:
        self.probe_total = max(probe_total, 0)
        self.merge_total = max(merge_total, 1)
        self.probe_done = 0
        self.merge_done = 0
        self.phase = "starting"
        self.group_label = ""
        self.start = time.monotonic()
        self._last_logged_pct = -1
        self._lock = threading.Lock()
        self._render(force_log=True)

    @property
    def overall_total(self) -> int:
        return self.probe_total + self.merge_total

    @property
    def overall_done(self) -> int:
        return self.probe_done + self.merge_done

    def set_phase(self, phase: str, group_label: str = "") -> None:
        self.phase = phase
        if group_label:
            self.group_label = group_label
        self._render(force_log=True)

    def probe_step(self) -> None:
        with self._lock:
            self.probe_done += 1
        self._render()

    def merge_step(self) -> None:
        with self._lock:
            self.merge_done += 1
        self._render()

    def _render(self, force_log: bool = False) -> None:
        total = max(self.overall_total, 1)
        done = self.overall_done
        pct = 100 * done / total
        elapsed = time.monotonic() - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (total - done) / rate if rate > 0 else 0.0
        bar = format_bar(done / total)
        group = f" | {self.group_label}" if self.group_label else ""
        line = (
            f"TOTAL: [{bar}] {done}/{total} ({pct:.1f}%) "
            f"| {self.phase}{group} "
            f"| {format_duration(elapsed)} | ETA {format_duration(eta)}"
        )

        if is_tty() and not force_log:
            sys.stderr.write("\r\033[K" + line)
            sys.stderr.flush()
            return

        pct_bucket = int(pct / 2) * 2  # log every ~2%
        if force_log or done == total or pct_bucket > self._last_logged_pct or done <= 1:
            log(line)
            self._last_logged_pct = pct_bucket

    def finish(self) -> None:
        if is_tty():
            sys.stderr.write("\n")
            sys.stderr.flush()


class MergeReporter:
    """Merge-phase logging: batched skips, per-file detail, running totals."""

    def __init__(self, total: int, status_dir: Path | None = None) -> None:
        self.total = max(total, 1)
        self.done = 0
        self.merged = 0
        self.skipped = 0
        self.failed = 0
        self.planned = 0
        self.start = time.monotonic()
        self._skip_batch = 0
        self._skip_batch_mb = 0.0
        self._skip_first_name = ""
        self._last_summary_done = 0
        self._lock = threading.RLock()
        self.status_dir = status_dir
        self._current_record_type = ""
        self._current_name = ""
        self._session_start: datetime | None = None
        self._copy: dict = {
            "active": False,
            "file": "",
            "chunk": "",
            "clip": "",
            "detail": "",
        }
        self._merge: dict = {
            "active": False,
            "file": "",
            "chunk": "",
            "clip": "",
            "detail": "",
            "elapsed": "",
        }
        self._stage_ahead = ""

    def publish_status(self, *, elapsed_note: str = "") -> None:
        """Push copy∥merge progress into autopilot_status.json for the live dashboard."""
        if not self.status_dir:
            return
        with self._lock:
            merge_busy = bool(self._current_name) or bool(self._merge.get("active"))
            current = min(self.done + (1 if merge_busy else 0), self.total)
            pct = 100.0 * current / self.total
            detail = f"{current}/{self.total}"
            if self._current_name:
                detail += f" · {self._current_name}"
            if elapsed_note:
                detail += f" · {elapsed_note}"
                self._merge["elapsed"] = elapsed_note
            conveyors = {
                "copy": dict(self._copy),
                "merge": dict(self._merge),
            }
            stage_ahead = self._stage_ahead
            record_type = self._current_record_type or "Normal"
            session_start = self._session_start
        try:
            from autopilot_dashboard import write_import_status

            write_import_status(
                self.status_dir,
                record_type=record_type,
                percent=pct,
                detail=detail,
                session_start=session_start,
                conveyors=conveyors,
                stage_ahead=stage_ahead or None,
            )
        except Exception:
            pass

    def update_copy(
        self,
        *,
        active: bool,
        file: str = "",
        chunk: str = "",
        clip: str = "",
        detail: str = "",
        stage_ahead: str = "",
        record_type: str = "",
        session_start: datetime | None = None,
    ) -> None:
        with self._lock:
            self._copy = {
                "active": active,
                "file": file,
                "chunk": chunk,
                "clip": clip,
                "detail": detail,
            }
            if stage_ahead:
                self._stage_ahead = stage_ahead
            if record_type:
                self._current_record_type = record_type
            if session_start is not None:
                self._session_start = session_start
            self.publish_status()

    def update_merge(
        self,
        *,
        active: bool,
        file: str = "",
        chunk: str = "",
        detail: str = "",
        elapsed: str = "",
        record_type: str = "",
        session_start: datetime | None = None,
    ) -> None:
        with self._lock:
            self._merge = {
                "active": active,
                "file": file,
                "chunk": chunk,
                "clip": "",
                "detail": detail,
                "elapsed": elapsed,
            }
            if file:
                self._current_name = file if active else ""
            if record_type:
                self._current_record_type = record_type
            if session_start is not None:
                self._session_start = session_start
            self.publish_status(elapsed_note=elapsed)

    def _flush_skips(self, *, force: bool = False) -> None:
        if self._skip_batch == 0:
            return
        if not force and self._skip_batch < SKIP_BATCH_LOG:
            return
        if self._skip_batch == 1:
            log(f"  skip: {self._skip_first_name} ({self._skip_batch_mb:.0f} MB)")
        else:
            log(
                f"  skip ×{self._skip_batch} ({self._skip_batch_mb:.0f} MB total)"
                f" — e.g. {self._skip_first_name}"
            )
        self._skip_batch = 0
        self._skip_batch_mb = 0.0
        self._skip_first_name = ""

    def _summary(self, *, force: bool = False) -> None:
        if not force and self.done - self._last_summary_done < 10 and self.done != self.total:
            return
        self._last_summary_done = self.done
        elapsed = time.monotonic() - self.start
        pct = 100 * self.done / self.total
        rate = self.done / elapsed if elapsed > 0 else 0.0
        eta = (self.total - self.done) / rate if rate > 0 else 0.0
        bar = format_bar(self.done / self.total)
        log(
            f"Merge [{bar}] {self.done}/{self.total} ({pct:.1f}%) "
            f"| new {self.merged} skip {self.skipped} fail {self.failed} "
            f"| {format_duration(elapsed)} elapsed, ETA {format_duration(eta)}"
        )

    def skip(
        self,
        name: str,
        size_mb: float,
        pipeline: PipelineProgress | None,
    ) -> None:
        with self._lock:
            self.done += 1
            self.skipped += 1
            if self._skip_batch == 0:
                self._skip_first_name = name
            self._skip_batch += 1
            self._skip_batch_mb += size_mb
            self._flush_skips()
            self._summary()
        if pipeline:
            pipeline.merge_step()

    def begin_merge(
        self,
        *,
        session_idx: int,
        session_total: int,
        chunk: list[Clip],
        output_name: str,
    ) -> None:
        with self._lock:
            self._flush_skips(force=True)
            total_duration = sum(c.duration or 0.0 for c in chunk)
            first_ts = chunk[0].timestamp.strftime("%Y-%m-%d %H:%M")
            last_ts = chunk[-1].timestamp.strftime("%H:%M")
            log(
                f"  [{self.done + 1}/{self.total}] session {session_idx}/{session_total} "
                f"| {len(chunk)} clips, {total_duration / 60:.1f} min | {first_ts}→{last_ts}"
            )
            log(f"       → {output_name}")
            if len(chunk) <= 3:
                log(f"       clips: {', '.join(c.path.name for c in chunk)}")
            else:
                log(f"       clips: {chunk[0].path.name} … {chunk[-1].path.name}")
            self._current_name = output_name
            self._current_record_type = chunk[0].record_type if chunk else ""
            self._session_start = chunk[0].timestamp if chunk else None
            self._merge = {
                "active": True,
                "file": output_name,
                "chunk": f"{self.done + 1}/{self.total}",
                "clip": "",
                "detail": "concat from SSD",
                "elapsed": "",
            }
            self.publish_status()

    def finish_merge(
        self,
        *,
        size_mb: float,
        elapsed: float,
        pipeline: PipelineProgress | None,
        failed: bool = False,
        dry_run: bool = False,
    ) -> None:
        with self._lock:
            self.done += 1
            if failed:
                self.failed += 1
                log("       ✗ merge failed")
            elif dry_run:
                self.planned += 1
                log("       (dry-run — planned)")
            else:
                self.merged += 1
                log(f"       ✓ {size_mb:.0f} MB in {format_duration(elapsed)}")
            self._current_name = ""
            self._merge = {
                "active": False,
                "file": "",
                "chunk": f"{self.done}/{self.total}",
                "clip": "",
                "detail": "idle",
                "elapsed": "",
            }
            self._session_start = None
            self._summary(force=True)
            self.publish_status()
        if pipeline:
            pipeline.merge_step()

    def finish(self) -> None:
        with self._lock:
            self._flush_skips(force=True)
            self._summary(force=True)


@dataclass(frozen=True)
class Clip:
    path: Path
    record_type: str
    camera: str
    timestamp: datetime
    sequence: int
    duration: float | None = None

    @property
    def prefix(self) -> str:
        return TYPE_PREFIX[self.record_type]

    @property
    def camera_suffix(self) -> str:
        return "F" if self.camera == "Front" else "B"


def parse_filename(path: Path, record_type: str, camera: str) -> Clip | None:
    match = FILENAME_RE.match(path.name)
    if not match:
        return None
    prefix, date_part, time_part, seq_part, cam_suffix = match.groups()
    expected_prefix = TYPE_PREFIX[record_type]
    expected_suffix = "F" if camera == "Front" else "B"
    if prefix.upper() != expected_prefix or cam_suffix.upper() != expected_suffix:
        return None
    timestamp = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    return Clip(
        path=path,
        record_type=record_type,
        camera=camera,
        timestamp=timestamp,
        sequence=int(seq_part),
    )


def parse_datetime(value: str) -> datetime:
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m-%d-%Y %H:%M:%S",
        "%m-%d-%Y %H:%M",
        "%m-%d-%Y",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date/time: {value!r}. Use YYYY-MM-DD[ HH:MM[:SS]] or MM-DD-YYYY[ HH:MM[:SS]]"
    )


def parse_time(value: str) -> tuple[int, int, int]:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.hour, parsed.minute, parsed.second
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid time: {value!r}. Use HH:MM or HH:MM:SS")


def clip_in_range(
    clip: Clip,
    range_start: datetime | None,
    range_end: datetime | None,
) -> bool:
    if range_start and clip.timestamp < range_start:
        return False
    if range_end and clip.timestamp >= range_end:
        return False
    return True


def filter_clips(
    clips: list[Clip],
    range_start: datetime | None,
    range_end: datetime | None,
) -> list[Clip]:
    return [clip for clip in clips if clip_in_range(clip, range_start, range_end)]


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def format_clock(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


@dataclass(frozen=True)
class SessionRange:
    start: datetime
    end: datetime
    clip_count: int


@dataclass(frozen=True)
class GpsFileSummary:
    path: Path
    point_count: int
    start: datetime
    end: datetime
    size_bytes: int
    points_by_day: tuple[tuple[date, int], ...]


@dataclass(frozen=True)
class FolderInventory:
    record_type: str
    camera: str
    file_count: int
    total_bytes: int
    start: datetime | None
    end: datetime | None
    extension: str


@dataclass(frozen=True)
class PhotoFile:
    path: Path
    camera: str
    timestamp: datetime


def parse_photo(path: Path, camera: str) -> PhotoFile | None:
    match = PHOTO_RE.match(path.name)
    if not match:
        return None
    date_part, time_part, _seq, cam_suffix = match.groups()
    expected_suffix = "F" if camera == "Front" else "B"
    if cam_suffix.upper() != expected_suffix:
        return None
    timestamp = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    return PhotoFile(path=path, camera=camera, timestamp=timestamp)


def timestamp_from_media_name(name: str) -> datetime | None:
    match = re.match(
        r"^[A-Z]{2}(\d{8})-(\d{6})-\d+[FB]\.(MP4|JPG)$",
        name,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def scan_folder_inventory(
    source: Path,
    record_type: str,
    camera: str,
    *,
    extensions: tuple[str, ...] = (".MP4",),
) -> FolderInventory | None:
    folder = source / record_type / camera
    if not folder.is_dir():
        return None
    ext_set = {ext.upper() for ext in extensions}
    file_count = 0
    total_bytes = 0
    start: datetime | None = None
    end: datetime | None = None
    for path in folder.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.upper() not in ext_set:
            continue
        file_count += 1
        total_bytes += path.stat().st_size
        timestamp = timestamp_from_media_name(path.name)
        if timestamp:
            start = timestamp if start is None else min(start, timestamp)
            end = timestamp if end is None else max(end, timestamp)
    extension = extensions[0].lstrip(".").upper()
    return FolderInventory(
        record_type=record_type,
        camera=camera,
        file_count=file_count,
        total_bytes=total_bytes,
        start=start,
        end=end,
        extension=extension,
    )


def scan_all_inventory(source: Path) -> list[FolderInventory]:
    items: list[FolderInventory] = []
    for record_type in ALL_SCAN_TYPES:
        for camera in CAMERAS:
            item = scan_folder_inventory(source, record_type, camera)
            if item:
                items.append(item)
    for camera in CAMERAS:
        item = scan_folder_inventory(
            source, "Photo", camera, extensions=(".JPG", ".JPEG")
        )
        if item:
            items.append(item)
    return items


def scan_photos(source: Path, cameras: list[str]) -> list[PhotoFile]:
    photos: list[PhotoFile] = []
    for camera in cameras:
        folder = source / "Photo" / camera
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.upper() not in {".JPG", ".JPEG"}:
                continue
            photo = parse_photo(path, camera)
            if photo:
                photos.append(photo)
    return photos


def print_record_types_reference() -> None:
    log("\n=== Record types (70mai A810) ===")
    for record_type, (prefix, ext, description) in RECORD_TYPE_INFO.items():
        log(f"  {record_type:8} [{prefix}] .{ext:3}  {description}")


def print_card_inventory(inventory: list[FolderInventory]) -> None:
    log("\n=== Card inventory ===")
    if not inventory:
        log("  No media folders found.")
        return

    by_type: dict[str, list[FolderInventory]] = {}
    for item in inventory:
        by_type.setdefault(item.record_type, []).append(item)

    grand_files = 0
    grand_bytes = 0
    for record_type in (*ALL_SCAN_TYPES, "Photo"):
        items = by_type.get(record_type, [])
        if not items:
            continue
        prefix, ext, description = RECORD_TYPE_INFO[record_type]
        type_files = sum(i.file_count for i in items)
        type_bytes = sum(i.total_bytes for i in items)
        grand_files += type_files
        grand_bytes += type_bytes
        log(f"\n  {record_type} [{prefix}] — {description}")
        if type_files == 0:
            log("    (empty)")
            continue
        for item in items:
            if item.file_count == 0:
                log(f"    {item.camera:5}  empty")
                continue
            range_text = "no timestamps"
            if item.start and item.end:
                range_text = f"{format_ts(item.start)} -> {format_ts(item.end)}"
            log(
                f"    {item.camera:5}  {item.file_count:4} files, "
                f"{format_file_size(item.total_bytes):>8}  |  {range_text}"
            )

    log(
        f"\n  Total media: {grand_files} files, {format_file_size(grand_bytes)} "
        f"(excluding GPS logs)"
    )


def print_photos_section(photos: list[PhotoFile]) -> None:
    log("\n=== Photos ===")
    if not photos:
        log("  No photos found.")
        return
    for camera in CAMERAS:
        camera_photos = [p for p in photos if p.camera == camera]
        if not camera_photos:
            continue
        ordered = sorted(camera_photos, key=lambda p: p.timestamp)
        log(
            f"\n  Photo / {camera} — {len(ordered)} photo(s), "
            f"{format_ts(ordered[0].timestamp)} -> {format_ts(ordered[-1].timestamp)}"
        )
        for photo in ordered:
            size_kb = photo.path.stat().st_size / 1_000
            log(
                f"    {format_ts(photo.timestamp)}  "
                f"{photo.path.name}  ({size_kb:.0f} KB)"
            )


def format_file_size(num_bytes: int) -> str:
    if num_bytes >= 1_000_000_000:
        return f"{num_bytes / 1_000_000_000:.1f} GB"
    if num_bytes >= 1_000_000:
        return f"{num_bytes / 1_000_000:.1f} MB"
    if num_bytes >= 1_000:
        return f"{num_bytes / 1_000:.1f} KB"
    return f"{num_bytes} B"


def scan_gps_files(source: Path) -> list[GpsFileSummary]:
    summaries: list[GpsFileSummary] = []
    for path in sorted(source.glob("GPSData*.txt")):
        if not path.is_file():
            continue
        first: datetime | None = None
        last: datetime | None = None
        count = 0
        by_day: dict[date, int] = {}
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("$"):
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    ts = int(parts[0])
                except ValueError:
                    continue
                if ts < MIN_GPS_TIMESTAMP:
                    continue
                timestamp = datetime.fromtimestamp(ts)
                count += 1
                if first is None:
                    first = timestamp
                last = timestamp
                day = timestamp.date()
                by_day[day] = by_day.get(day, 0) + 1
        if first is None or last is None:
            continue
        summaries.append(
            GpsFileSummary(
                path=path,
                point_count=count,
                start=first,
                end=last,
                size_bytes=path.stat().st_size,
                points_by_day=tuple(sorted(by_day.items())),
            )
        )
    return summaries


def print_events_section(event_groups: list[tuple[str, str, list[Clip]]]) -> None:
    log("\n=== Events ===")
    if not event_groups:
        log("  No event clips found.")
        return

    for _record_type, camera, clips in event_groups:
        ordered = sorted(clips, key=lambda c: (c.timestamp, c.sequence))
        start = ordered[0].timestamp
        end = ordered[-1].timestamp
        log(
            f"\nEvent / {camera} — {len(clips)} event(s), "
            f"{format_ts(start)} -> {format_ts(end)}"
        )
        by_day: dict[date, list[Clip]] = {}
        for clip in ordered:
            by_day.setdefault(clip.timestamp.date(), []).append(clip)
        for day in sorted(by_day):
            day_events = by_day[day]
            log(f"  {day.isoformat()}  ({len(day_events)} event(s))")
            for clip in day_events:
                log(f"    {format_clock(clip.timestamp)}  {clip.path.name}")


def print_gps_section(gps_files: list[GpsFileSummary]) -> None:
    log("\n=== GPS tracks ===")
    if not gps_files:
        log("  No GPSData*.txt files found on the card.")
        return

    overall_start = min(item.start for item in gps_files)
    overall_end = max(item.end for item in gps_files)
    total_points = sum(item.point_count for item in gps_files)
    log(
        f"  {len(gps_files)} file(s) | {total_points} points | "
        f"{format_ts(overall_start)} -> {format_ts(overall_end)}"
    )
    log(
        f"  calendar days: {format_day(overall_start)} .. {format_day(overall_end)}"
    )

    merged_days: dict[date, int] = {}
    for item in gps_files:
        for day, count in item.points_by_day:
            merged_days[day] = merged_days.get(day, 0) + count

    for item in gps_files:
        log(
            f"\n  {item.path.name} — {format_file_size(item.size_bytes)}, "
            f"{item.point_count} points"
        )
        log(f"    range: {format_ts(item.start)} -> {format_ts(item.end)}")
        log(f"    days:  {format_day(item.start)} .. {format_day(item.end)}")
        if len(item.points_by_day) <= 14:
            for day, count in item.points_by_day:
                log(f"      {day.isoformat()}  {count} points")
        else:
            for day, count in item.points_by_day[:7]:
                log(f"      {day.isoformat()}  {count} points")
            log(f"      ... ({len(item.points_by_day) - 10} more days)")
            for day, count in item.points_by_day[-3:]:
                log(f"      {day.isoformat()}  {count} points")

    if len(merged_days) <= 21:
        log("\n  GPS by date (all files):")
        for day in sorted(merged_days):
            log(f"    {day.isoformat()}  {merged_days[day]} points")
    else:
        log(
            f"\n  GPS by date: {len(merged_days)} days with data "
            f"({format_day(overall_start)} .. {format_day(overall_end)})"
        )


def session_ranges(clips: list[Clip], gap_seconds: float) -> list[SessionRange]:
    ranges: list[SessionRange] = []
    for session in split_sessions(clips, gap_seconds):
        ranges.append(
            SessionRange(
                start=session[0].timestamp,
                end=session[-1].timestamp,
                clip_count=len(session),
            )
        )
    return ranges


def collect_groups(
    source: Path,
    record_types: list[str],
    cameras: list[str],
    *,
    warn: bool = True,
) -> list[tuple[str, str, list[Clip]]]:
    groups: list[tuple[str, str, list[Clip]]] = []
    for record_type in record_types:
        for camera in cameras:
            folder = source / record_type / camera
            if not folder.is_dir():
                if warn:
                    log(f"Warning: missing folder {folder}")
                continue
            clips = scan_clips(source, [record_type], [camera])
            if clips:
                groups.append((record_type, camera, clips))
    return groups


def print_scan_report(
    source: Path,
    groups: list[tuple[str, str, list[Clip]]],
    gap_seconds: float,
    gps_files: list[GpsFileSummary],
    inventory: list[FolderInventory],
    photos: list[PhotoFile],
) -> None:
    all_clips = [clip for _, _, clips in groups for clip in clips]
    recording_groups = [
        (record_type, camera, clips)
        for record_type, camera, clips in groups
        if record_type not in ("Event",)
    ]
    event_groups = [
        (record_type, camera, clips)
        for record_type, camera, clips in groups
        if record_type == "Event"
    ]

    log(f"Scanning {source}")
    log(f"Session gap: {gap_seconds:g} sec (pauses longer than this start a new range)")

    print_record_types_reference()
    print_card_inventory(inventory)

    if not all_clips and not gps_files and not photos:
        log("\nNo clips, photos, or GPS data found on the card.")
        return

    log("\n=== Overall ===")
    if all_clips:
        overall_start = min(c.timestamp for c in all_clips)
        overall_end = max(c.timestamp for c in all_clips)
        log(
            f"  video: {len(all_clips)} clips | "
            f"{format_ts(overall_start)} -> {format_ts(overall_end)}"
        )
        log(
            f"  video days: {format_day(overall_start)} .. {format_day(overall_end)}"
        )
    else:
        log("  video: no clips found")

    if gps_files:
        gps_start = min(item.start for item in gps_files)
        gps_end = max(item.end for item in gps_files)
        gps_points = sum(item.point_count for item in gps_files)
        gps_bytes = sum(item.size_bytes for item in gps_files)
        log(
            f"  GPS:   {gps_points} points in {len(gps_files)} file(s), "
            f"{format_file_size(gps_bytes)} | "
            f"{format_ts(gps_start)} -> {format_ts(gps_end)}"
        )
        log(f"  GPS days: {format_day(gps_start)} .. {format_day(gps_end)}")

    if photos:
        photo_start = min(p.timestamp for p in photos)
        photo_end = max(p.timestamp for p in photos)
        log(
            f"  photos: {len(photos)} file(s) | "
            f"{format_ts(photo_start)} -> {format_ts(photo_end)}"
        )

    if recording_groups:
        log("\n=== By type / camera ===")
        for record_type, camera, clips in recording_groups:
            start = min(c.timestamp for c in clips)
            end = max(c.timestamp for c in clips)
            sessions = session_ranges(clips, gap_seconds)
            log(
                f"\n{record_type} / {camera} — "
                f"{len(clips)} clips, {format_ts(start)} -> {format_ts(end)}"
            )
            if len(sessions) == 1:
                session = sessions[0]
                log(
                    f"  continuous: {format_ts(session.start)} -> {format_ts(session.end)} "
                    f"({session.clip_count} clips)"
                )
            else:
                log(f"  {len(sessions)} recording session(s):")
                for idx, session in enumerate(sessions, start=1):
                    log(
                        f"    {idx}. {format_ts(session.start)} -> {format_ts(session.end)} "
                        f"({session.clip_count} clips)"
                    )

    print_events_section(event_groups)
    print_photos_section(photos)
    print_gps_section(gps_files)

    if all_clips:
        log("\n=== By date (video) ===")
        by_day: dict[date, list[Clip]] = {}
        for clip in all_clips:
            day = clip.timestamp.date()
            by_day.setdefault(day, []).append(clip)

        for day in sorted(by_day):
            day_clips = by_day[day]
            day_start = min(c.timestamp for c in day_clips)
            day_end = max(c.timestamp for c in day_clips)
            active_groups = sorted(
                {
                    f"{record_type}/{camera}"
                    for record_type, camera, clips in groups
                    if any(c.timestamp.date() == day for c in clips)
                }
            )
            log(
                f"  {day.isoformat()}  {format_clock(day_start)} — {format_clock(day_end)}  "
                f"| {len(day_clips)} clips | {', '.join(active_groups)}"
            )

    log(
        "\nUse --date / --from-time / --to-time or --from / --to to export a range."
    )
    log("Use --export-events to copy each event clip as a separate file.")


def scan_clips(
    source: Path,
    record_types: list[str],
    cameras: list[str],
    *,
    warn: bool = True,
) -> list[Clip]:
    clips: list[Clip] = []
    for record_type in record_types:
        for camera in cameras:
            folder = source / record_type / camera
            if not folder.is_dir():
                if warn:
                    print(f"Warning: missing folder {folder}", file=sys.stderr)
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.suffix.upper() != ".MP4":
                    continue
                clip = parse_filename(path, record_type, camera)
                if clip:
                    clips.append(clip)
    return clips


def split_sessions(clips: list[Clip], gap_seconds: float) -> list[list[Clip]]:
    if not clips:
        return []
    ordered = sorted(clips, key=lambda c: (c.timestamp, c.sequence))
    sessions: list[list[Clip]] = [[ordered[0]]]
    for clip in ordered[1:]:
        prev = sessions[-1][-1]
        gap = (clip.timestamp - prev.timestamp).total_seconds()
        if gap > gap_seconds:
            sessions.append([clip])
        else:
            sessions[-1].append(clip)
    return sessions


def _probe_duration_uncached(path: Path, ffprobe: str) -> float:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {path}")
    return float(result.stdout.strip())


def probe_duration(path: Path, ffprobe: str) -> float:
    from probe_cache import cached_probe_duration

    return cached_probe_duration(
        path, lambda p: _probe_duration_uncached(p, ffprobe)
    )


def probe_duration_safe(path: Path, ffprobe: str) -> float | None:
    try:
        return probe_duration(path, ffprobe)
    except (RuntimeError, ValueError, OSError):
        return None


def is_valid_merge_output(
    path: Path,
    ffprobe: str,
    expected_duration_sec: float,
) -> bool:
    """True when output exists, is non-trivial size, and ffprobe duration looks sane."""
    if not path.is_file():
        return False
    try:
        if path.stat().st_size < MIN_MERGE_BYTES:
            return False
    except OSError:
        return False
    duration = probe_duration_safe(path, ffprobe)
    if duration is None or duration < 0.5:
        return False
    if expected_duration_sec > 0 and duration < expected_duration_sec * MERGE_DURATION_TOLERANCE:
        return False
    return True


def _ffmpeg_merge_error(result: subprocess.CompletedProcess[str]) -> str:
    err = result.stderr.strip() or result.stdout.strip()
    return err or f"ffmpeg exit {result.returncode}"


def prefetch_durations(
    paths: list[Path],
    ffprobe: str,
    cache: dict[Path, float],
    progress: ProgressTracker | None = None,
    pipeline: PipelineProgress | None = None,
    workers: int = PROBE_WORKERS,
) -> None:
    pending = [path for path in paths if path not in cache]
    if not pending:
        return

    def probe_one(path: Path) -> tuple[Path, float]:
        return path, probe_duration(path, ffprobe)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(probe_one, path): path for path in pending}
        for future in as_completed(futures):
            path, duration = future.result()
            cache[path] = duration
            if progress:
                progress.update(path.name)
            if pipeline:
                pipeline.probe_step()


def attach_durations(
    clips: list[Clip],
    ffprobe: str,
    cache: dict[Path, float],
    progress: ProgressTracker | None = None,
    pipeline: PipelineProgress | None = None,
    assume_seconds: float | None = None,
    parallel_probe: bool = True,
    workers: int = PROBE_WORKERS,
) -> list[Clip]:
    if assume_seconds is not None:
        for clip in clips:
            if clip.path not in cache:
                cache[clip.path] = assume_seconds
                if progress:
                    progress.update(clip.path.name)
                if pipeline:
                    pipeline.probe_step()
    elif parallel_probe:
        prefetch_durations(
            [clip.path for clip in clips],
            ffprobe,
            cache,
            progress,
            pipeline,
            workers=max(1, int(workers)),
        )
    else:
        for clip in clips:
            if clip.path not in cache:
                if progress:
                    progress.update(clip.path.name)
                cache[clip.path] = probe_duration(clip.path, ffprobe)
                if pipeline:
                    pipeline.probe_step()

    return [
        Clip(
            path=clip.path,
            record_type=clip.record_type,
            camera=clip.camera,
            timestamp=clip.timestamp,
            sequence=clip.sequence,
            duration=cache[clip.path],
        )
        for clip in clips
    ]


def split_chunks(
    session: list[Clip],
    chunk_seconds: float | None = None,
    chunk_clips: int | None = None,
) -> list[list[Clip]]:
    """Pack clips into chunks by file count and/or duration."""
    if not session:
        return []
    max_clips = int(chunk_clips) if chunk_clips and chunk_clips > 0 else 0
    max_seconds = float(chunk_seconds) if chunk_seconds and chunk_seconds > 0 else 0.0
    if max_clips <= 0 and max_seconds <= 0:
        max_clips = 10

    chunks: list[list[Clip]] = []
    current: list[Clip] = []
    current_duration = 0.0
    for clip in session:
        duration = clip.duration or 0.0
        would_exceed_count = bool(max_clips and current and len(current) >= max_clips)
        would_exceed_time = bool(
            max_seconds and current and current_duration + duration > max_seconds
        )
        if would_exceed_count or would_exceed_time:
            chunks.append(current)
            current = [clip]
            current_duration = duration
        else:
            current.append(clip)
            current_duration += duration
    if current:
        chunks.append(current)
    return chunks


def output_name(chunk: list[Clip]) -> str:
    first = chunk[0]
    last = chunk[-1]
    start = first.timestamp.strftime("%Y%m%d-%H%M%S")
    end = last.timestamp.strftime("%H%M%S")
    return f"{first.prefix}_{start}_{end}_{first.camera_suffix}.mp4"


def escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _prefetch_files(paths: list[Path]) -> None:
    """Sequentially read files to warm the OS page cache (slow SD readers)."""
    for path in paths:
        try:
            with open(path, "rb", buffering=0) as handle:
                while handle.read(PREFETCH_BLOCK):
                    pass
        except OSError:
            return


def start_prefetch(paths: list[Path]) -> threading.Thread:
    thread = threading.Thread(
        target=_prefetch_files, args=(list(paths),), daemon=True
    )
    thread.start()
    return thread


def _same_filesystem(a: Path, b: Path) -> bool:
    try:
        return a.resolve().stat().st_dev == b.resolve().stat().st_dev
    except OSError:
        return False


def stage_clips_locally(
    chunk: list[Clip],
    stage_dir: Path,
    *,
    index_offset: int = 0,
    total_hint: int | None = None,
    on_clip: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
    """Copy chunk clips from SD to local stage_dir (sequential, one USB stream)."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    total = total_hint if total_hint is not None else len(chunk)
    for i, clip in enumerate(chunk):
        idx = index_offset + i + 1
        dest = stage_dir / clip.path.name
        src_size = clip.path.stat().st_size
        if on_clip is not None:
            try:
                on_clip(idx, total, clip.path.name)
            except Exception:
                pass
        if dest.is_file() and dest.stat().st_size == src_size:
            log(f"  [copy] {idx}/{total}: {clip.path.name} (already on SSD)")
            staged.append(dest)
            continue
        partial = dest.with_suffix(dest.suffix + ".partial")
        partial.unlink(missing_ok=True)
        log(
            f"  [copy] {idx}/{total}: {clip.path.name} "
            f"({src_size / 1_000_000:.0f} MB) SD→SSD"
        )
        t0 = time.monotonic()
        shutil.copyfile(clip.path, partial)
        partial.replace(dest)
        log(
            f"  [copy] {idx}/{total}: ok in {format_duration(time.monotonic() - t0)}"
        )
        staged.append(dest)
    return staged


def cleanup_stage_dir(stage_dir: Path | None) -> None:
    if stage_dir is None:
        return
    try:
        shutil.rmtree(stage_dir, ignore_errors=True)
    except OSError:
        pass


def _ffmpeg_concat_copy(
    ffmpeg: str,
    sources: list[Path],
    output_path: Path,
    *,
    heartbeat: str | None = None,
    on_heartbeat: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as list_file:
        for src in sources:
            list_file.write(f"file '{escape_concat_path(src)}'\n")
        list_path = Path(list_file.name)
    try:
        return run_quiet_subprocess(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-probesize",
                "1M",
                "-analyzeduration",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(output_path),
            ],
            heartbeat=heartbeat,
            on_heartbeat=on_heartbeat,
        )
    finally:
        list_path.unlink(missing_ok=True)


def merge_clips(
    chunk: list[Clip],
    output_path: Path,
    ffmpeg: str,
    ffprobe: str,
    dry_run: bool,
    reporter: MergeReporter | None = None,
    merge_progress: ProgressTracker | None = None,
    pipeline: PipelineProgress | None = None,
    import_store: object | None = None,
    *,
    record_type: str = "",
    camera: str = "",
    session_idx: int = 0,
    session_total: int = 0,
    stage_batch_clips: int = 10,
    merge_max_attempts: int | None = None,
    merge_retry_delay_sec: float | None = None,
    pre_staged: tuple[Path | None, list[Path]] | None = None,
) -> str:
    expected_duration = sum(c.duration or 0.0 for c in chunk)

    if output_path.exists():
        if is_valid_merge_output(output_path, ffprobe, expected_duration):
            size_mb = output_path.stat().st_size / 1_000_000
            if import_store is not None:
                import_store.record_merge(
                    record_type=record_type,
                    camera=camera,
                    filename=output_path.name,
                    status="skipped",
                    session_idx=session_idx,
                    clip_count=len(chunk),
                    size_mb=size_mb,
                )
            if reporter:
                reporter.skip(output_path.name, size_mb, pipeline)
            else:
                log(f"  skip (exists, {size_mb:.0f} MB): {output_path.name}")
                if merge_progress:
                    merge_progress.update("skipped")
                if pipeline:
                    pipeline.merge_step()
            return "skipped"
        log(
            f"  invalid or incomplete merge output, rebuilding: {output_path.name}"
        )
        output_path.unlink(missing_ok=True)
        if import_store is not None:
            import_store.record_merge(
                record_type=record_type,
                camera=camera,
                filename=output_path.name,
                status="pending",
                session_idx=session_idx,
                clip_count=len(chunk),
            )

    if reporter:
        reporter.begin_merge(
            session_idx=session_idx,
            session_total=session_total,
            chunk=chunk,
            output_name=output_path.name,
        )
    else:
        total_duration = sum(c.duration or 0.0 for c in chunk)
        log(
            f"  merging {len(chunk)} clips ({total_duration / 60:.1f} min) "
            f"-> {output_path.name}"
        )
    if dry_run:
        if import_store is not None:
            import_store.record_merge(
                record_type=record_type,
                camera=camera,
                filename=output_path.name,
                status="planned",
                session_idx=session_idx,
                clip_count=len(chunk),
            )
        if reporter:
            reporter.finish_merge(
                size_mb=0.0,
                elapsed=0.0,
                pipeline=pipeline,
                dry_run=True,
            )
        elif merge_progress:
            merge_progress.update("planned")
            if pipeline:
                pipeline.merge_step()
        return "planned"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merge_start = time.monotonic()
    attempts = merge_max_attempts if merge_max_attempts is not None else MERGE_MAX_ATTEMPTS
    retry_delay = (
        merge_retry_delay_sec
        if merge_retry_delay_sec is not None
        else MERGE_RETRY_DELAY_SEC
    )
    batch_size = max(1, int(stage_batch_clips or len(chunk) or 1))

    stage_dir: Path | None = None
    pre_staged_paths: list[Path] | None = None
    if pre_staged is not None:
        stage_dir, pre_staged_paths = pre_staged
    need_stage = (
        pre_staged_paths is None
        and bool(chunk)
        and not _same_filesystem(chunk[0].path, output_path.parent)
    )
    on_hb = (
        (
            lambda elapsed: reporter.publish_status(
                elapsed_note=format_duration(elapsed)
            )
        )
        if reporter is not None
        else None
    )

    def _fail(msg: str) -> str:
        log(f"       ERROR: {msg}")
        output_path.unlink(missing_ok=True)
        cleanup_stage_dir(stage_dir)
        if import_store is not None:
            import_store.record_merge(
                record_type=record_type,
                camera=camera,
                filename=output_path.name,
                status="failed",
                session_idx=session_idx,
                clip_count=len(chunk),
                elapsed_sec=time.monotonic() - merge_start,
            )
        if reporter:
            reporter.finish_merge(
                size_mb=0.0,
                elapsed=time.monotonic() - merge_start,
                pipeline=pipeline,
                failed=True,
            )
        elif merge_progress:
            merge_progress.update("failed")
            if pipeline:
                pipeline.merge_step()
        return "failed"

    def _concat_sources(sources: list[Path], dest: Path, label: str) -> str | None:
        """Return None on success, else error string."""
        last_error = ""
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                log(
                    f"       retry {attempt}/{attempts} "
                    f"({last_error or 'previous attempt failed'})"
                )
                time.sleep(retry_delay)
            result = _ffmpeg_concat_copy(
                ffmpeg,
                sources,
                dest,
                heartbeat=label,
                on_heartbeat=on_hb,
            )
            if result.returncode != 0:
                last_error = _ffmpeg_merge_error(result)
                log(f"       ERROR: {last_error}")
                dest.unlink(missing_ok=True)
                continue
            return None
        return last_error or "ffmpeg concat failed"

    try:
        if pre_staged_paths is not None:
            log(
                f"  [merge] concat -c copy from SSD "
                f"({len(pre_staged_paths)} clips) → {output_path.name}"
            )
            err = _concat_sources(
                pre_staged_paths,
                output_path,
                f"merging {output_path.name}",
            )
            if err:
                return _fail(err)
        elif need_stage:
            stage_dir = output_path.parent / ".merge_stage" / output_path.stem
            batches = [
                chunk[i : i + batch_size] for i in range(0, len(chunk), batch_size)
            ]
            log(
                f"  [copy] sequential fallback: {len(chunk)} clip(s) in "
                f"{len(batches)} batch(es) of ≤{batch_size} → {stage_dir.name}/"
            )
            if len(batches) == 1:
                try:
                    concat_sources = stage_clips_locally(
                        chunk, stage_dir, total_hint=len(chunk)
                    )
                except OSError as exc:
                    return _fail(f"staging failed: {exc}")
                log(
                    f"  [merge] concat -c copy from SSD "
                    f"({len(concat_sources)} clips) → {output_path.name}"
                )
                err = _concat_sources(
                    concat_sources, output_path, f"merging {output_path.name}"
                )
                if err:
                    return _fail(err)
            else:
                growing: Path | None = None
                offset = 0
                for batch_idx, batch in enumerate(batches, start=1):
                    try:
                        staged = stage_clips_locally(
                            batch,
                            stage_dir,
                            index_offset=offset,
                            total_hint=len(chunk),
                        )
                    except OSError as exc:
                        if growing is not None:
                            growing.unlink(missing_ok=True)
                        return _fail(f"staging failed: {exc}")
                    offset += len(batch)
                    part_out = stage_dir / f"_partial_{batch_idx}.mp4"
                    sources = ([growing] if growing is not None else []) + staged
                    log(
                        f"  [merge] concat batch {batch_idx}/{len(batches)} "
                        f"({len(sources)} inputs) …"
                    )
                    err = _concat_sources(
                        sources,
                        part_out,
                        f"merging {output_path.name} batch {batch_idx}/{len(batches)}",
                    )
                    for path in staged:
                        path.unlink(missing_ok=True)
                    if growing is not None:
                        growing.unlink(missing_ok=True)
                    if err:
                        part_out.unlink(missing_ok=True)
                        return _fail(err)
                    growing = part_out
                assert growing is not None
                growing.replace(output_path)
        else:
            concat_sources = [clip.path for clip in chunk]
            log(
                f"  [merge] concat -c copy (sources already local, "
                f"{len(concat_sources)} clips) → {output_path.name}"
            )
            err = _concat_sources(
                concat_sources, output_path, f"merging {output_path.name}"
            )
            if err:
                return _fail(err)

        if not is_valid_merge_output(output_path, ffprobe, expected_duration):
            return _fail("output failed ffprobe validation")

        size_mb = output_path.stat().st_size / 1_000_000
        elapsed = time.monotonic() - merge_start
        log(
            f"  [merge] DONE {output_path.name}: {size_mb:.0f} MB "
            f"in {format_duration(elapsed)}"
        )
        if import_store is not None:
            import_store.record_merge(
                record_type=record_type,
                camera=camera,
                filename=output_path.name,
                status="merged",
                session_idx=session_idx,
                clip_count=len(chunk),
                size_mb=size_mb,
                elapsed_sec=elapsed,
            )
        if reporter:
            reporter.finish_merge(
                size_mb=size_mb,
                elapsed=elapsed,
                pipeline=pipeline,
            )
        elif merge_progress:
            merge_progress.update(
                f"done {size_mb:.0f} MB in {format_duration(elapsed)}"
            )
            if pipeline:
                pipeline.merge_step()
        else:
            if pipeline:
                pipeline.merge_step()
        return "merged"
    finally:
        cleanup_stage_dir(stage_dir)


def process_event_group(
    clips: list[Clip],
    output_dir: Path,
    ffmpeg: str,
    dry_run: bool,
    ffprobe: str,
    duration_cache: dict[Path, float],
    probe_progress: ProgressTracker | None,
    merge_reporter: MergeReporter | None,
    pipeline: PipelineProgress | None,
    import_store: object | None = None,
    assume_seconds: float | None = None,
) -> tuple[int, int, int, int]:
    """Merge all Event clips into one file per camera (lossless concat)."""
    if not clips:
        return 0, 0, 0, 0

    record_type = clips[0].record_type
    camera = clips[0].camera
    sorted_clips = sorted(clips, key=lambda c: (c.timestamp, c.sequence))

    if pipeline:
        pipeline.set_phase("probing", f"{record_type}/{camera}")

    uncached = sum(1 for clip in sorted_clips if clip.path not in duration_cache)
    if uncached:
        if assume_seconds is not None:
            log(f"  estimating {uncached} clip(s) at {assume_seconds:g}s each...")
        else:
            log(f"  probing {uncached} clip(s) ({PROBE_WORKERS} parallel workers)...")

    attach_durations(
        sorted_clips,
        ffprobe,
        duration_cache,
        probe_progress,
        pipeline,
        assume_seconds=assume_seconds,
    )

    if pipeline:
        pipeline.set_phase("merging", f"{record_type}/{camera}")

    chunk = [
        Clip(
            path=clip.path,
            record_type=clip.record_type,
            camera=clip.camera,
            timestamp=clip.timestamp,
            sequence=clip.sequence,
            duration=duration_cache[clip.path],
        )
        for clip in sorted_clips
    ]
    out_path = output_dir / record_type / camera / output_name(chunk)
    total_duration = sum(c.duration or 0.0 for c in chunk)

    log(
        f"=== Merging {record_type}/{camera}: {len(chunk)} event clip(s) "
        f"-> 1 file ({format_duration(total_duration)}) ==="
    )

    from runtime_config import import_settings

    live = import_settings()
    status = merge_clips(
        chunk,
        out_path,
        ffmpeg,
        ffprobe,
        dry_run,
        merge_reporter,
        pipeline=pipeline,
        import_store=import_store,
        record_type=record_type,
        camera=camera,
        session_idx=1,
        session_total=1,
        stage_batch_clips=int(live.get("stage_batch_clips") or 10),
        merge_max_attempts=int(live.get("merge_max_attempts") or MERGE_MAX_ATTEMPTS),
        merge_retry_delay_sec=float(
            live.get("merge_retry_delay_sec") or MERGE_RETRY_DELAY_SEC
        ),
    )

    merged = 1 if status == "merged" else 0
    skipped = 1 if status == "skipped" else 0
    failed = 1 if status == "failed" else 0
    planned = 1 if status == "planned" else 0

    log(
        f"--- {record_type}/{camera} done: {merged} merged, {skipped} skipped"
        + (f", {planned} planned" if dry_run else "")
        + (f", {failed} failed" if failed else "")
    )
    return merged, skipped, failed, planned


def _run_copy_merge_pipeline(
    merge_jobs: list[tuple[int, list[Clip], Path]],
    *,
    ffmpeg: str,
    ffprobe: str,
    merge_reporter: MergeReporter | None,
    pipeline: PipelineProgress | None,
    import_store: object | None,
    record_type: str,
    camera: str,
    session_total: int,
    stage_ahead: int,
    merge_workers: int,
) -> list[str]:
    """Run [copy] SD→SSD and [merge] concat as overlapping conveyors."""
    from runtime_config import import_settings, log_config_if_changed

    ahead = max(1, int(stage_ahead))
    ahead_sem = threading.Semaphore(ahead)
    ready: Queue = Queue(maxsize=ahead + 4)
    stop = object()
    statuses: list[str] = []
    staged_inflight = 0
    inflight_lock = threading.Lock()

    def _merge_kwargs(live: dict) -> dict:
        return {
            "stage_batch_clips": int(live.get("stage_batch_clips") or 10),
            "merge_max_attempts": int(
                live.get("merge_max_attempts") or MERGE_MAX_ATTEMPTS
            ),
            "merge_retry_delay_sec": float(
                live.get("merge_retry_delay_sec") or MERGE_RETRY_DELAY_SEC
            ),
        }

    def copy_worker() -> None:
        nonlocal staged_inflight
        for job_i, (session_idx, chunk, out_path) in enumerate(merge_jobs, start=1):
            log_config_if_changed(log)
            live = import_settings()
            expected = sum(c.duration or 0.0 for c in chunk)
            chunk_label = f"{job_i}/{len(merge_jobs)}"
            sess_start = chunk[0].timestamp if chunk else None
            rec_type = chunk[0].record_type if chunk else record_type

            if out_path.exists() and is_valid_merge_output(
                out_path, ffprobe, expected
            ):
                log(f"  [copy] skip (output exists): {out_path.name}")
                if merge_reporter is not None:
                    merge_reporter.update_copy(
                        active=False,
                        file=out_path.name,
                        chunk=chunk_label,
                        detail="skip exists",
                        record_type=rec_type,
                        session_start=sess_start,
                    )
                ready.put(("exists", session_idx, chunk, out_path, None, None))
                continue

            need_copy = bool(chunk) and not _same_filesystem(
                chunk[0].path, out_path.parent
            )
            if not need_copy:
                log(
                    f"  [copy] not needed (already local FS): {out_path.name} "
                    f"— hand off {len(chunk)} clips to [merge]"
                )
                if merge_reporter is not None:
                    merge_reporter.update_copy(
                        active=False,
                        file=out_path.name,
                        chunk=chunk_label,
                        detail="local FS",
                        record_type=rec_type,
                        session_start=sess_start,
                    )
                ready.put(
                    (
                        "ready",
                        session_idx,
                        chunk,
                        out_path,
                        None,
                        [c.path for c in chunk],
                    )
                )
                continue

            ahead_sem.acquire()
            with inflight_lock:
                staged_inflight += 1
                inflight = staged_inflight
            stage_dir = out_path.parent / ".merge_stage" / out_path.stem
            total_mb = 0.0
            try:
                total_mb = sum(c.path.stat().st_size for c in chunk) / 1_000_000
            except OSError:
                pass
            ahead_label = f"{inflight}/{ahead}"
            log(
                f"  [copy] START {out_path.name}: {len(chunk)} clips "
                f"(~{total_mb:.0f} MB) SD→SSD (staged ahead {ahead_label})"
            )
            if merge_reporter is not None:
                merge_reporter.update_copy(
                    active=True,
                    file=out_path.name,
                    chunk=chunk_label,
                    clip=f"0/{len(chunk)}",
                    detail="SD→SSD",
                    stage_ahead=ahead_label,
                    record_type=rec_type,
                    session_start=sess_start,
                )

            def _on_clip(idx: int, total: int, name: str, _out=out_path.name) -> None:
                if merge_reporter is None:
                    return
                # Avoid flooding status JSON on huge Event/Parking merges.
                if total > 20 and idx not in (1, total) and idx % 5 != 0:
                    return
                merge_reporter.update_copy(
                    active=True,
                    file=_out,
                    chunk=chunk_label,
                    clip=f"{idx}/{total}",
                    detail=f"SD→SSD {name}",
                    stage_ahead=ahead_label,
                    record_type=rec_type,
                    session_start=sess_start,
                )

            t0 = time.monotonic()
            try:
                batch = max(1, int(live.get("stage_batch_clips") or len(chunk) or 1))
                staged: list[Path] = []
                for offset in range(0, len(chunk), batch):
                    part = chunk[offset : offset + batch]
                    staged.extend(
                        stage_clips_locally(
                            part,
                            stage_dir,
                            index_offset=offset,
                            total_hint=len(chunk),
                            on_clip=_on_clip,
                        )
                    )
                elapsed = time.monotonic() - t0
                log(
                    f"  [copy] DONE  {out_path.name}: {len(staged)} clips on SSD "
                    f"in {format_duration(elapsed)} — queued for [merge]"
                )
                if merge_reporter is not None:
                    merge_reporter.update_copy(
                        active=False,
                        file=out_path.name,
                        chunk=chunk_label,
                        clip=f"{len(chunk)}/{len(chunk)}",
                        detail="queued for merge",
                        stage_ahead=ahead_label,
                        record_type=rec_type,
                        session_start=sess_start,
                    )
                ready.put(("ready", session_idx, chunk, out_path, stage_dir, staged))
            except OSError as exc:
                log(f"  [copy] FAIL  {out_path.name}: {exc}")
                if merge_reporter is not None:
                    merge_reporter.update_copy(
                        active=False,
                        file=out_path.name,
                        chunk=chunk_label,
                        detail=f"FAIL {exc}",
                        stage_ahead=ahead_label,
                        record_type=rec_type,
                        session_start=sess_start,
                    )
                ready.put(("copy_fail", session_idx, chunk, out_path, None, str(exc)))
                with inflight_lock:
                    staged_inflight = max(0, staged_inflight - 1)
                ahead_sem.release()
        if merge_reporter is not None:
            merge_reporter.update_copy(
                active=False,
                detail="copy queue empty",
                stage_ahead=f"0/{ahead}",
            )
        ready.put(stop)

    def do_merge(item: tuple) -> str:
        nonlocal staged_inflight
        kind, session_idx, chunk, out_path, stage_dir, payload = item
        log_config_if_changed(log)
        live = import_settings()
        kw = _merge_kwargs(live)
        if kind == "exists":
            return merge_clips(
                chunk,
                out_path,
                ffmpeg,
                ffprobe,
                False,
                merge_reporter,
                pipeline=pipeline,
                import_store=import_store,
                record_type=record_type,
                camera=camera,
                session_idx=session_idx,
                session_total=session_total,
                **kw,
            )
        if kind == "copy_fail":
            log(f"  [merge] skip (copy failed): {out_path.name} — {payload}")
            if merge_reporter:
                merge_reporter.begin_merge(
                    session_idx=session_idx,
                    session_total=session_total,
                    chunk=chunk,
                    output_name=out_path.name,
                )
                merge_reporter.finish_merge(
                    size_mb=0.0,
                    elapsed=0.0,
                    pipeline=pipeline,
                    failed=True,
                )
            elif pipeline:
                pipeline.merge_step()
            return "failed"

        assert kind == "ready"
        release_ahead = stage_dir is not None
        try:
            log(
                f"  [merge] START {out_path.name}: "
                f"enough clips on SSD ({len(payload)}) — concat"
            )
            return merge_clips(
                chunk,
                out_path,
                ffmpeg,
                ffprobe,
                False,
                merge_reporter,
                pipeline=pipeline,
                import_store=import_store,
                record_type=record_type,
                camera=camera,
                session_idx=session_idx,
                session_total=session_total,
                pre_staged=(stage_dir, list(payload)),
                **kw,
            )
        finally:
            if release_ahead:
                with inflight_lock:
                    staged_inflight = max(0, staged_inflight - 1)
                ahead_sem.release()

    copy_thread = threading.Thread(
        target=copy_worker, name="sd-copy", daemon=True
    )
    copy_thread.start()

    workers = max(1, int(merge_workers))
    if workers == 1:
        while True:
            item = ready.get()
            if item is stop:
                break
            statuses.append(do_merge(item))
    else:
        log(f"  [merge] {workers} parallel concat workers")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            while True:
                item = ready.get()
                if item is stop:
                    break
                futures.append(pool.submit(do_merge, item))
            for fut in futures:
                statuses.append(fut.result())

    copy_thread.join(timeout=1)
    return statuses


def process_group(
    clips: list[Clip],
    output_dir: Path,
    gap_seconds: float,
    chunk_seconds: float,
    ffmpeg: str,
    ffprobe: str,
    dry_run: bool,
    duration_cache: dict[Path, float],
    probe_progress: ProgressTracker | None,
    merge_reporter: MergeReporter | None,
    pipeline: PipelineProgress | None,
    import_store: object | None = None,
    assume_seconds: float | None = None,
    merge_workers: int = MERGE_WORKERS,
    chunk_clips: int | None = None,
) -> tuple[int, int, int, int]:
    if not clips:
        return 0, 0, 0, 0

    from runtime_config import import_settings, log_config_if_changed

    log_config_if_changed(log)
    settings = import_settings()
    record_type = clips[0].record_type
    camera = clips[0].camera
    sessions = split_sessions(clips, gap_seconds)
    merged = 0
    skipped = 0
    failed = 0
    planned = 0
    workers = max(
        1, int(merge_workers if merge_workers is not None else settings["merge_workers"])
    )
    clips_per_chunk = (
        int(chunk_clips)
        if chunk_clips is not None
        else int(settings.get("chunk_clips") or 0)
    )
    probe_workers = max(1, int(settings.get("probe_workers") or PROBE_WORKERS))

    log(f"\n--- {record_type}/{camera}: {len(clips)} clips, {len(sessions)} sessions ---")
    if pipeline:
        pipeline.set_phase("probing", f"{record_type}/{camera}")

    uncached_all = sum(1 for clip in clips if clip.path not in duration_cache)
    if uncached_all:
        if assume_seconds is not None:
            log(f"  estimating {uncached_all} clip(s) at {assume_seconds:g}s each...")
        else:
            log(f"  probing {uncached_all} clip(s) ({probe_workers} parallel workers)...")

    attach_durations(
        clips,
        ffprobe,
        duration_cache,
        probe_progress,
        pipeline,
        assume_seconds=assume_seconds,
        workers=probe_workers,
    )

    if pipeline:
        pipeline.set_phase("merging", f"{record_type}/{camera}")

    log(
        f"  chunking: ≤{clips_per_chunk or '∞'} clips"
        f", ≤{(chunk_seconds or 0) / 60:g} min"
    )

    session_chunks: list[tuple[int, list[Clip], list[list[Clip]]]] = []
    for session_idx, session in enumerate(sessions, start=1):
        session_with_duration = [
            Clip(
                path=clip.path,
                record_type=clip.record_type,
                camera=clip.camera,
                timestamp=clip.timestamp,
                sequence=clip.sequence,
                duration=duration_cache[clip.path],
            )
            for clip in session
        ]
        chunks = split_chunks(
            session_with_duration,
            chunk_seconds=chunk_seconds,
            chunk_clips=clips_per_chunk,
        )
        session_chunks.append((session_idx, session_with_duration, chunks))

    total_chunk_files = sum(len(chunks) for _, _, chunks in session_chunks)
    session_span = (
        f"{sessions[0][0].timestamp:%Y-%m-%d %H:%M} – "
        f"{sessions[-1][-1].timestamp:%Y-%m-%d %H:%M}"
        if sessions
        else "—"
    )
    log(
        f"=== Merging {record_type}/{camera}: {len(sessions)} sessions, "
        f"{total_chunk_files} output file(s) | {session_span} ==="
    )

    merge_jobs: list[tuple[int, list[Clip], Path]] = []
    for session_idx, session_with_duration, chunks in session_chunks:
        session_duration = sum(c.duration or 0.0 for c in session_with_duration)
        log(
            f"  session {session_idx}/{len(sessions)}: {len(chunks)} file(s), "
            f"{session_duration / 60:.1f} min raw | "
            f"{session_with_duration[0].timestamp:%Y-%m-%d %H:%M} – "
            f"{session_with_duration[-1].timestamp:%H:%M}"
        )
        for chunk in chunks:
            out_path = output_dir / record_type / camera / output_name(chunk)
            merge_jobs.append((session_idx, chunk, out_path))

    if dry_run:
        statuses = [
            merge_clips(
                chunk,
                out_path,
                ffmpeg,
                ffprobe,
                True,
                merge_reporter,
                pipeline=pipeline,
                import_store=import_store,
                record_type=record_type,
                camera=camera,
                session_idx=session_idx,
                session_total=len(sessions),
            )
            for session_idx, chunk, out_path in merge_jobs
        ]
    else:
        # Two conveyors: [copy] SD→SSD fills a queue; [merge] concat consumes it.
        stage_ahead = max(1, int(settings.get("prefetch_batches") or 2))
        log(
            f"  pipeline: [copy] SD→SSD ∥ [merge] concat "
            f"(up to {stage_ahead} chunk(s) staged ahead)"
        )
        statuses = _run_copy_merge_pipeline(
            merge_jobs,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            merge_reporter=merge_reporter,
            pipeline=pipeline,
            import_store=import_store,
            record_type=record_type,
            camera=camera,
            session_total=len(sessions),
            stage_ahead=stage_ahead,
            merge_workers=workers,
        )

    for status in statuses:
        if status == "merged":
            merged += 1
        elif status == "skipped":
            skipped += 1
        elif status == "planned":
            planned += 1
        else:
            failed += 1

    log(
        f"--- {record_type}/{camera} done: {merged} merged, {skipped} skipped"
        + (f", {planned} planned" if dry_run else "")
        + (f", {failed} failed" if failed else "")
    )
    return merged, skipped, failed, planned


def event_output_name(clip: Clip) -> str:
    ts = clip.timestamp.strftime("%Y%m%d-%H%M%S")
    return f"EV_{ts}_{clip.camera_suffix}.mp4"


def export_event_clip(
    clip: Clip,
    output_path: Path,
    dry_run: bool,
    progress: ProgressTracker | None = None,
) -> str:
    if output_path.exists():
        size_mb = output_path.stat().st_size / 1_000_000
        log(f"  skip (exists, {size_mb:.1f} MB): {output_path.name}")
        if progress:
            progress.update("skipped")
        return "skipped"

    size_mb = clip.path.stat().st_size / 1_000_000
    log(f"  export {clip.path.name} ({size_mb:.1f} MB) -> {output_path.name}")
    if dry_run:
        if progress:
            progress.update("planned")
        return "planned"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(clip.path, output_path)
    except OSError as exc:
        log(f"  ERROR: {exc}")
        if progress:
            progress.update("failed")
        return "failed"

    if progress:
        progress.update("done")
    return "exported"


def export_events(
    event_groups: list[tuple[str, str, list[Clip]]],
    output_dir: Path,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    all_events = [
        clip
        for _record_type, _camera, clips in event_groups
        for clip in sorted(clips, key=lambda c: (c.timestamp, c.sequence))
    ]
    if not all_events:
        log("No event clips to export.")
        return 0, 0, 0, 0

    progress = ProgressTracker(len(all_events), "Export events")
    exported = 0
    skipped = 0
    failed = 0
    planned = 0

    for clip in all_events:
        out_path = output_dir / "Event" / clip.camera / event_output_name(clip)
        status = export_event_clip(clip, out_path, dry_run, progress)
        if status == "exported":
            exported += 1
        elif status == "skipped":
            skipped += 1
        elif status == "planned":
            planned += 1
        else:
            failed += 1

    progress.finish()
    return exported, skipped, failed, planned


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(
            f"Required tool '{name}' not found. Install ffmpeg, e.g. `brew install ffmpeg`."
        )
    return path


EXPORT_RANGE = """
Export range (clip start timestamp must satisfy: start <= t < end):

  Date + time window:
    --date 04-27-2026 --from-time 08:00 --to-time 09:00
    --date 2026-04-27                        # whole day

  Full datetime:
    --from "2026-04-27 08:00" --to "2026-04-27 09:00"

  Date formats: YYYY-MM-DD[ HH:MM[:SS]] or MM-DD-YYYY[ HH:MM[:SS]]
  Time formats: HH:MM or HH:MM:SS (for --from-time / --to-time)

Scan SD card for available data ranges:
  python3 import_70mai.py --scan

Export each event as a separate clip (no merge):
  python3 import_70mai.py --export-events
  python3 import_70mai.py --export-events --date 2026-04-27 --cameras Front
"""


def _split_csv_or_list(value: str | list[str]) -> list[str]:
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        out.extend(part.strip() for part in item.split(",") if part.strip())
    return out


def parse_types_and_cameras(
    types: str | list[str], cameras: str | list[str]
) -> tuple[list[str], list[str]]:
    record_types = _split_csv_or_list(types)
    camera_list = _split_csv_or_list(cameras)
    invalid_types = set(record_types) - set(RECORD_TYPES)
    invalid_cameras = set(camera_list) - set(CAMERAS)
    if invalid_types:
        raise SystemExit(f"Unknown record types: {', '.join(sorted(invalid_types))}")
    if invalid_cameras:
        raise SystemExit(f"Unknown cameras: {', '.join(sorted(invalid_cameras))}")
    return record_types, camera_list


def main() -> int:
    from runtime_config import (
        ensure_default_config_file,
        import_settings,
        log_config_if_changed,
    )

    ensure_default_config_file()
    imp_defaults = import_settings(force=True)

    parser = argparse.ArgumentParser(
        description="Import and merge 70mai SD card clips into longer videos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXPORT_RANGE,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/Volumes/Untitled"),
        help="SD card mount path",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "video" / "Output",
        help="Output directory for merged videos",
    )
    parser.add_argument(
        "--chunk-minutes",
        type=float,
        default=float(imp_defaults.get("chunk_minutes") or 10.0),
        help="Max chunk length in minutes (with --chunk-clips)",
    )
    parser.add_argument(
        "--chunk-clips",
        type=int,
        default=int(imp_defaults.get("chunk_clips") or 10),
        metavar="N",
        help="Max source clips per merged file (default from 70mai_runtime.json)",
    )
    parser.add_argument(
        "--stage-batch-clips",
        type=int,
        default=int(imp_defaults.get("stage_batch_clips") or 10),
        metavar="N",
        help="How many clips to copy to SSD per staging batch (hot-reloaded from JSON)",
    )
    parser.add_argument(
        "--merge-workers",
        type=int,
        default=int(imp_defaults.get("merge_workers") or MERGE_WORKERS),
        metavar="N",
        help=(
            "Parallel ffmpeg concat workers per camera group "
            f"(default: {MERGE_WORKERS}; use 1 on USB/SD to avoid seek thrashing)"
        ),
    )
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=float(imp_defaults.get("gap_seconds") or 120.0),
        help="Start new session if gap between clips exceeds this",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=["Normal", "Event", "Parking"],
        metavar="TYPE",
        help="Record types: Normal Event or Normal,Event (default: all three)",
    )
    parser.add_argument(
        "--cameras",
        nargs="+",
        default=["Front", "Back"],
        metavar="CAMERA",
        help="Cameras: Front Back or Front,Back (default: both)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show merge plan without running ffmpeg",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan SD card: inventory, ranges, events, photos, GPS (no ffmpeg)",
    )
    parser.add_argument(
        "--export-events",
        action="store_true",
        help="Export each Event clip as a separate file (copy, no merge)",
    )
    parser.add_argument(
        "--from",
        dest="range_from",
        type=parse_datetime,
        metavar="DATETIME",
        help="Export range start, e.g. 2026-04-27 08:00 or 04-27-2026 08:00",
    )
    parser.add_argument(
        "--to",
        dest="range_to",
        type=parse_datetime,
        metavar="DATETIME",
        help="Export range end (exclusive), e.g. 2026-04-27 09:00",
    )
    parser.add_argument(
        "--date",
        type=parse_datetime,
        metavar="DATE",
        help="Shortcut: date only, use with --from-time and --to-time",
    )
    parser.add_argument(
        "--from-time",
        type=parse_time,
        metavar="HH:MM",
        help="Range start time on --date, e.g. 08:00",
    )
    parser.add_argument(
        "--to-time",
        type=parse_time,
        metavar="HH:MM",
        help="Range end time on --date (exclusive), e.g. 09:00",
    )
    parser.add_argument(
        "--state-on-sd",
        action="store_true",
        help="Write import merge state + card inventory to SD /.70mai/import/",
    )
    parser.add_argument(
        "--no-state-on-sd",
        action="store_true",
        help="Do not write import state to SD (host cache only)",
    )
    parser.add_argument(
        "--skip-inventory-refresh",
        action="store_true",
        help="Skip card inventory build (autopilot already wrote CARD_SUMMARY.txt)",
    )
    parser.add_argument(
        "--status-dir",
        type=Path,
        default=None,
        help="Write autopilot_status.json here during merge (live dashboard)",
    )
    args = parser.parse_args()

    range_start = args.range_from
    range_end = args.range_to
    if args.date or args.from_time or args.to_time:
        if not args.date:
            raise SystemExit("--date is required when using --from-time or --to-time")
        day = args.date.date()
        if args.from_time:
            h, m, s = args.from_time
            range_start = datetime(day.year, day.month, day.day, h, m, s)
        else:
            range_start = datetime(day.year, day.month, day.day)
        if args.to_time:
            h, m, s = args.to_time
            range_end = datetime(day.year, day.month, day.day, h, m, s)
        else:
            range_end = datetime(day.year, day.month, day.day, 23, 59, 59)
    if range_start and range_end and range_start >= range_end:
        raise SystemExit(f"Invalid range: {range_start} >= {range_end}")

    record_types, cameras = parse_types_and_cameras(args.types, args.cameras)
    if not args.source.is_dir():
        raise SystemExit(f"Source not found: {args.source}")

    if args.scan:
        if args.dry_run:
            log("Note: --dry-run is ignored with --scan")
        if args.export_events:
            log("Note: --export-events is ignored with --scan")
        scan_types = list(ALL_SCAN_TYPES)
        groups = collect_groups(args.source, scan_types, cameras, warn=False)
        inventory = scan_all_inventory(args.source)
        photos = scan_photos(args.source, list(CAMERAS))
        gps_files = scan_gps_files(args.source)
        print_scan_report(
            args.source, groups, args.gap_seconds, gps_files, inventory, photos
        )
        return 0

    if args.export_events:
        if args.dry_run:
            log("Mode:    dry-run")
        log(f"Source:  {args.source}")
        log(f"Output:  {args.output}")
        log(f"Cameras: {', '.join(cameras)}")
        if range_start or range_end:
            log(f"Range:   {range_start or '...'} -> {range_end or '...'}")
        log("Export:  one file per event (lossless copy)")

        event_groups: list[tuple[str, str, list[Clip]]] = []
        for camera in cameras:
            clips = scan_clips(args.source, ["Event"], [camera], warn=False)
            clips = filter_clips(clips, range_start, range_end)
            if clips:
                event_groups.append(("Event", camera, clips))

        run_start = time.monotonic()
        exported, skipped, failed, planned = export_events(
            event_groups, args.output, args.dry_run
        )
        elapsed = time.monotonic() - run_start
        if args.dry_run:
            log(
                f"\nDone in {format_duration(elapsed)}: "
                f"{planned} planned, {skipped} skipped, {failed} failed"
            )
        else:
            log(
                f"\nDone in {format_duration(elapsed)}: "
                f"{exported} exported, {skipped} skipped, {failed} failed"
            )
        return 1 if failed else 0

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    log_config_if_changed(log, force=True)
    live = import_settings()
    # CLI wins for this process start; JSON hot-reloads stage_batch / retries mid-run.
    chunk_minutes = float(args.chunk_minutes)
    chunk_clips = int(args.chunk_clips)
    chunk_seconds = chunk_minutes * 60.0
    run_start = time.monotonic()

    log(f"Source:  {args.source}")
    log(f"Output:  {args.output}")
    log(f"Chunk:   ≤{chunk_clips} clips / ≤{chunk_minutes:g} min")
    log(f"Stage:   {args.stage_batch_clips} clips/batch (live from JSON)")
    log(f"Gap:     {args.gap_seconds:g} sec")
    log(f"Types:   {', '.join(record_types)}")
    log(f"Cameras: {', '.join(cameras)}")
    if range_start or range_end:
        log(f"Range:   {range_start or '...'} -> {range_end or '...'}")
    if args.dry_run:
        log("Mode:    dry-run")

    log("Merge:   ffmpeg concat -c copy (lossless, no re-encode)")
    log(
        f"Workers: {max(1, args.merge_workers)} "
        f"(prefetch={'on' if live.get('prefetch') and max(1, args.merge_workers) == 1 else 'off'}; "
        "SD clips staged locally then deleted)"
    )
    log(f"Probe:   {int(live.get('probe_workers') or PROBE_WORKERS)} parallel ffprobe workers")

    groups: list[tuple[str, str, list[Clip]]] = []
    total_clips = 0
    for record_type in record_types:
        for camera in cameras:
            folder = args.source / record_type / camera
            if not folder.is_dir():
                log(f"Warning: missing folder {folder}")
                continue
            clips = scan_clips(args.source, [record_type], [camera], warn=False)
            clips = filter_clips(clips, range_start, range_end)
            if clips:
                groups.append((record_type, camera, clips))
                total_clips += len(clips)

    total_outputs = 0
    for _, _, clips in groups:
        for session in split_sessions(clips, args.gap_seconds):
            if chunk_clips > 0:
                total_outputs += max(1, -(-len(session) // chunk_clips))
            else:
                session_seconds = len(session) * 60
                total_outputs += max(1, -(-session_seconds // int(chunk_seconds or 600)))

    log(f"\nFound {total_clips} clips in {len(groups)} group(s)")
    log(f"Estimated output files: ~{total_outputs}")

    assume_seconds = 60.0 if args.dry_run else None
    if assume_seconds is not None:
        log("Dry-run uses assumed 60s per clip (no ffprobe) for a fast preview")

    probe_progress = (
        None if assume_seconds is not None else ProgressTracker(total_clips, "Probe")
    )
    merge_reporter = MergeReporter(max(total_outputs, 1), status_dir=args.status_dir)
    if args.status_dir is not None:
        try:
            from autopilot_dashboard import write_import_status

            write_import_status(
                args.status_dir,
                record_type=record_types[0] if record_types else "Normal",
                percent=0.0,
                detail="starting",
            )
        except Exception:
            pass
    pipeline = PipelineProgress(total_clips, max(total_outputs, 1))

    label = "_".join(record_types)
    state_on_sd = args.state_on_sd and not args.no_state_on_sd
    import_store = None
    if state_on_sd and not args.dry_run:
        from import_state import ImportStateStore

        import_store = ImportStateStore(
            args.source,
            label,
            state_on_sd=True,
            local_dir=args.output / ".import_tmp",
            chunk_minutes=args.chunk_minutes,
            gap_seconds=args.gap_seconds,
        )
        if not args.skip_inventory_refresh:
            import_store.refresh_inventory(
                types=record_types,
                ffprobe=ffprobe,
            )
        failed_prev = import_store.count_failed_merges()
        if failed_prev:
            log(
                f"Merge retry: {failed_prev} previously failed output(s) "
                "will be rebuilt if still missing"
            )

    duration_cache: dict[Path, float] = {}
    total_merged = 0
    total_skipped = 0
    total_failed = 0
    total_planned = 0

    for group_idx, (record_type, camera, clips) in enumerate(groups, start=1):
        log(f"\n>>> Group {group_idx}/{len(groups)}: {record_type}/{camera}")
        if record_type in ("Event", "Parking"):
            merged, skipped, failed, planned = process_event_group(
                clips,
                args.output,
                ffmpeg,
                args.dry_run,
                ffprobe,
                duration_cache,
                probe_progress,
                merge_reporter,
                pipeline,
                import_store,
                assume_seconds=assume_seconds,
            )
        else:
            merged, skipped, failed, planned = process_group(
                clips,
                args.output,
                args.gap_seconds,
                chunk_seconds,
                ffmpeg,
                ffprobe,
                args.dry_run,
                duration_cache,
                probe_progress,
                merge_reporter,
                pipeline,
                import_store,
                assume_seconds=assume_seconds,
                merge_workers=max(1, args.merge_workers),
                chunk_clips=chunk_clips,
            )
        total_merged += merged
        total_skipped += skipped
        total_failed += failed
        total_planned += planned

    if import_store is not None:
        import_store.update_merge_plan(groups, duration_cache)
        import_store.finalize()

    if probe_progress:
        probe_progress.finish()
    merge_reporter.finish()
    pipeline.finish()

    elapsed = time.monotonic() - run_start
    if args.dry_run:
        log(
            f"\nDone in {format_duration(elapsed)}: "
            f"{total_planned} planned, {total_skipped} skipped, {total_failed} failed"
        )
    else:
        log(
            f"\nDone in {format_duration(elapsed)}: "
            f"{total_merged} merged, {total_skipped} skipped, {total_failed} failed"
        )
    return 1 if total_failed else 0


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    raise SystemExit(main())
