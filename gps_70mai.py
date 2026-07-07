#!/usr/bin/env python3
"""Parse 70mai GPSData*.txt logs and interpolate by wall-clock time."""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MIN_GPS_TIMESTAMP = 1577836800  # 2020-01-01
VIDEO_NAME_RE = re.compile(
    r"^[A-Z]{2}(\d{8})-(\d{6})-\d+[FB]\.MP4$",
    re.IGNORECASE,
)
MIN_HEADING_SPEED_KMH = 8.0
MIN_BEARING_METERS = 4.0
HEADING_SMOOTHING = 0.2


@dataclass(frozen=True)
class GpsPoint:
    timestamp: datetime
    valid: bool
    lat: float
    lon: float
    speed_kmh: float
    heading_deg: float | None
    video_name: str


@dataclass(frozen=True)
class TelemetrySample:
    """GPS state at a moment in time (interpolated or nearest)."""

    timestamp: datetime
    lat: float
    lon: float
    speed_kmh: float
    heading_deg: float
    g_force: float


def find_gps_files(*roots: Path | str | None) -> list[Path]:
    seen: set[Path] = set()
    found: list[Path] = []
    for root in roots:
        if not root:
            continue
        base = Path(root)
        candidates = [base, base / ".GPS"]
        for directory in candidates:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("GPSData*.txt")):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(path)
    return found


def clip_time_from_video_name(name: str) -> datetime | None:
    match = VIDEO_NAME_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _parse_speed_kmh(parts: list[str]) -> float:
    if len(parts) < 9:
        return 0.0
    try:
        raw = int(parts[8])
    except ValueError:
        return 0.0
    if len(parts) >= 6:
        try:
            legacy = int(parts[5])
            if legacy > 0 and raw < 200:
                legacy_kmh = legacy * 0.036
                if abs(legacy_kmh - raw) < 25:
                    return max(0.0, legacy_kmh)
        except ValueError:
            pass
    return max(0.0, float(raw))


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float | None:
    dist = _haversine_m(lat1, lon1, lat2, lon2)
    if dist < MIN_BEARING_METERS:
        return None
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def parse_gps_line(line: str, *, offset_sec: float = 0.0) -> GpsPoint | None:
    line = line.strip()
    if not line or line.startswith("$"):
        return None
    parts = line.split(",")
    if len(parts) < 9:
        return None
    try:
        ts = int(parts[0])
        lat = float(parts[2])
        lon = float(parts[3])
    except ValueError:
        return None
    if ts < MIN_GPS_TIMESTAMP:
        return None
    status = parts[1].upper()
    video_name = parts[9] if len(parts) > 9 else ""
    wall_ts = ts + offset_sec
    return GpsPoint(
        timestamp=datetime.fromtimestamp(wall_ts),
        valid=status == "A",
        lat=lat,
        lon=lon,
        speed_kmh=_parse_speed_kmh(parts),
        heading_deg=None,
        video_name=video_name,
    )


def estimate_gps_offset(
    gps_files: list[Path],
    wall_start: datetime,
    wall_end: datetime,
    *,
    sample_limit: int = 2000,
    near_clip_sec: float = 90.0,
) -> float:
    """Estimate seconds added to GPS unix time to match video wall-clock.

    Uses GPS records logged near the start of each referenced clip — robust
    against mid-clip log lines where ``clip_start - gps_ts`` would be negative.
    """
    offsets: list[float] = []
    search_start = wall_start.timestamp() - 6 * 3600
    search_end = wall_end.timestamp() + 6 * 3600

    for path in gps_files:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("$"):
                    continue
                parts = line.split(",")
                if len(parts) < 10:
                    continue
                try:
                    ts = int(parts[0])
                except ValueError:
                    continue
                if ts < search_start or ts > search_end:
                    continue
                clip_start = clip_time_from_video_name(parts[9])
                if clip_start is None:
                    continue
                if abs(ts - clip_start.timestamp()) > near_clip_sec:
                    continue
                offsets.append(clip_start.timestamp() - ts)
                if len(offsets) >= sample_limit:
                    break
        if len(offsets) >= sample_limit:
            break

    if not offsets:
        return 0.0
    offsets.sort()
    return offsets[len(offsets) // 2]


def _file_time_range(path: Path, offset_sec: float = 0.0) -> tuple[datetime, datetime] | None:
    first: datetime | None = None
    last: datetime | None = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            point = parse_gps_line(line, offset_sec=offset_sec)
            if point is None:
                continue
            first = point.timestamp
            break
        if first is None:
            return None
        handle.seek(0, 2)
        size = handle.tell()
        chunk = min(size, 65536)
        handle.seek(max(0, size - chunk))
        tail = handle.read().splitlines()
        for line in reversed(tail):
            point = parse_gps_line(line, offset_sec=offset_sec)
            if point is None:
                continue
            last = point.timestamp
            break
    if first is None or last is None:
        return None
    return first, last


def load_gps_points(
    gps_files: list[Path],
    start: datetime,
    end: datetime,
    *,
    offset_sec: float = 0.0,
) -> list[GpsPoint]:
    gps_start = start.timestamp() - offset_sec
    gps_end = end.timestamp() - offset_sec
    points: list[GpsPoint] = []

    for path in gps_files:
        if not path.is_file():
            continue
        file_range = _file_time_range(path, offset_sec=offset_sec)
        if file_range is not None:
            file_start, file_end = file_range
            if file_end < start or file_start > end:
                continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("$"):
                    continue
                parts = line.split(",")
                if len(parts) < 9:
                    continue
                try:
                    ts = int(parts[0])
                except ValueError:
                    continue
                if ts < gps_start:
                    continue
                if ts > gps_end:
                    break
                point = parse_gps_line(line, offset_sec=offset_sec)
                if point is not None:
                    points.append(point)
    points.sort(key=lambda p: p.timestamp)
    return points


def _fill_headings(points: list[GpsPoint]) -> list[GpsPoint]:
    if not points:
        return points
    filled: list[GpsPoint] = []
    prev_heading: float | None = None
    for idx, point in enumerate(points):
        heading: float | None = None
        if idx + 1 < len(points):
            nxt = points[idx + 1]
            heading = _bearing_deg(point.lat, point.lon, nxt.lat, nxt.lon)
        if heading is None and idx > 0:
            prev = points[idx - 1]
            heading = _bearing_deg(prev.lat, prev.lon, point.lat, point.lon)
        if heading is None:
            heading = prev_heading or 0.0
        if point.speed_kmh < MIN_HEADING_SPEED_KMH and prev_heading is not None:
            heading = prev_heading
        elif prev_heading is not None:
            delta = ((heading - prev_heading + 540) % 360) - 180
            heading = (prev_heading + HEADING_SMOOTHING * delta) % 360
        prev_heading = heading
        filled.append(
            GpsPoint(
                timestamp=point.timestamp,
                valid=point.valid,
                lat=point.lat,
                lon=point.lon,
                speed_kmh=point.speed_kmh,
                heading_deg=heading,
                video_name=point.video_name,
            )
        )
    return filled


def _smooth_sample_headings(samples: list[TelemetrySample]) -> list[TelemetrySample]:
    if not samples:
        return samples
    prev_heading = samples[0].heading_deg
    smoothed: list[TelemetrySample] = []
    for sample in samples:
        heading = sample.heading_deg
        if sample.speed_kmh < MIN_HEADING_SPEED_KMH:
            heading = prev_heading
        else:
            delta = ((heading - prev_heading + 540) % 360) - 180
            heading = (prev_heading + HEADING_SMOOTHING * delta) % 360
        prev_heading = heading
        smoothed.append(
            TelemetrySample(
                timestamp=sample.timestamp,
                lat=sample.lat,
                lon=sample.lon,
                speed_kmh=sample.speed_kmh,
                heading_deg=heading,
                g_force=sample.g_force,
            )
        )
    return smoothed


def build_telemetry_samples(
    points: list[GpsPoint],
    wall_start: datetime,
    duration_sec: float,
    fps: int,
) -> list[TelemetrySample]:
    if not points:
        return []
    points = _fill_headings(points)
    timestamps = [p.timestamp for p in points]
    speeds = [p.speed_kmh for p in points]
    frame_count = max(1, int(math.ceil(duration_sec * fps)))
    samples: list[TelemetrySample] = []
    prev_speed = speeds[0]
    for frame_idx in range(frame_count):
        t = wall_start.timestamp() + frame_idx / fps
        moment = datetime.fromtimestamp(t)
        idx = bisect.bisect_left(timestamps, moment)
        if idx <= 0:
            point = points[0]
            accel = (point.speed_kmh - prev_speed) * fps / 3.6
            g_force = min(2.0, abs(accel / 9.81))
            prev_speed = point.speed_kmh
            samples.append(
                TelemetrySample(
                    timestamp=moment,
                    lat=point.lat,
                    lon=point.lon,
                    speed_kmh=max(0.0, point.speed_kmh),
                    heading_deg=point.heading_deg or 0.0,
                    g_force=g_force,
                )
            )
            continue
        if idx >= len(points):
            point = points[-1]
            accel = (point.speed_kmh - prev_speed) * fps / 3.6
            g_force = min(2.0, abs(accel / 9.81))
            prev_speed = point.speed_kmh
            samples.append(
                TelemetrySample(
                    timestamp=moment,
                    lat=point.lat,
                    lon=point.lon,
                    speed_kmh=max(0.0, point.speed_kmh),
                    heading_deg=point.heading_deg or 0.0,
                    g_force=g_force,
                )
            )
            continue

        prev_p, next_p = points[idx - 1], points[idx]
        span = (next_p.timestamp - prev_p.timestamp).total_seconds()
        if span <= 0:
            point = prev_p
            accel = (point.speed_kmh - prev_speed) * fps / 3.6
            g_force = min(2.0, abs(accel / 9.81))
            prev_speed = point.speed_kmh
            samples.append(
                TelemetrySample(
                    timestamp=moment,
                    lat=point.lat,
                    lon=point.lon,
                    speed_kmh=max(0.0, point.speed_kmh),
                    heading_deg=point.heading_deg or 0.0,
                    g_force=g_force,
                )
            )
            continue

        ratio = (moment - prev_p.timestamp).total_seconds() / span
        ratio = max(0.0, min(1.0, ratio))
        lat = prev_p.lat + (next_p.lat - prev_p.lat) * ratio
        lon = prev_p.lon + (next_p.lon - prev_p.lon) * ratio
        speed = prev_p.speed_kmh + (next_p.speed_kmh - prev_p.speed_kmh) * ratio
        h1 = prev_p.heading_deg or 0.0
        h2 = next_p.heading_deg or h1
        delta = ((h2 - h1 + 540) % 360) - 180
        heading = (h1 + delta * ratio) % 360
        accel = (speed - prev_speed) * fps / 3.6
        g_force = min(2.0, abs(accel / 9.81))
        prev_speed = speed
        samples.append(
            TelemetrySample(
                timestamp=moment,
                lat=lat,
                lon=lon,
                speed_kmh=max(0.0, speed),
                heading_deg=heading,
                g_force=g_force,
            )
        )
    return _smooth_sample_headings(samples)
