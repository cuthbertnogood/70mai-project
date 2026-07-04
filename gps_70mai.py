#!/usr/bin/env python3
"""Parse 70mai GPSData*.txt logs and interpolate by wall-clock time."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MIN_GPS_TIMESTAMP = 1577836800  # 2020-01-01


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


def _parse_speed_kmh(parts: list[str]) -> float:
    if len(parts) < 9:
        return 0.0
    try:
        raw = int(parts[8])
    except ValueError:
        return 0.0
    # A810-style logs store km/h directly; older firmware uses cm/s in field 5.
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


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _parse_heading(parts: list[str]) -> float | None:
    if len(parts) < 7:
        return None
    try:
        raw = int(parts[4])
    except ValueError:
        return None
    if raw > 36000:
        return raw / 100.0 % 360.0
    if 0 <= raw <= 360:
        return float(raw)
    return None


def parse_gps_line(line: str) -> GpsPoint | None:
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
    return GpsPoint(
        timestamp=datetime.fromtimestamp(ts),
        valid=status == "A",
        lat=lat,
        lon=lon,
        speed_kmh=_parse_speed_kmh(parts),
        heading_deg=_parse_heading(parts),
        video_name=video_name,
    )


def load_gps_points(
    gps_files: list[Path],
    start: datetime,
    end: datetime,
) -> list[GpsPoint]:
    points: list[GpsPoint] = []
    for path in gps_files:
        if not path.is_file():
            continue
        file_start: datetime | None = None
        file_end: datetime | None = None
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                point = parse_gps_line(line)
                if point is None:
                    continue
                if file_start is None:
                    file_start = point.timestamp
                file_end = point.timestamp
                if point.timestamp < start:
                    continue
                if point.timestamp > end:
                    break
                points.append(point)
        if file_end is not None and file_end < start:
            continue
        if file_start is not None and file_start > end:
            continue
    points.sort(key=lambda p: p.timestamp)
    return points


def _fill_headings(points: list[GpsPoint]) -> list[GpsPoint]:
    if not points:
        return points
    filled: list[GpsPoint] = []
    for idx, point in enumerate(points):
        heading = point.heading_deg
        if heading is None and idx + 1 < len(points):
            nxt = points[idx + 1]
            heading = _bearing_deg(point.lat, point.lon, nxt.lat, nxt.lon)
        elif heading is None and idx > 0:
            prev = points[idx - 1]
            heading = _bearing_deg(prev.lat, prev.lon, point.lat, point.lon)
        elif heading is None:
            heading = 0.0
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
        elif idx >= len(points):
            point = points[-1]
        else:
            prev_p, next_p = points[idx - 1], points[idx]
            span = (next_p.timestamp - prev_p.timestamp).total_seconds()
            if span <= 0:
                point = prev_p
            else:
                ratio = (moment - prev_p.timestamp).total_seconds() / span
                ratio = max(0.0, min(1.0, ratio))
                lat = prev_p.lat + (next_p.lat - prev_p.lat) * ratio
                lon = prev_p.lon + (next_p.lon - prev_p.lon) * ratio
                speed = prev_p.speed_kmh + (next_p.speed_kmh - prev_p.speed_kmh) * ratio
                h1 = prev_p.heading_deg or 0.0
                h2 = next_p.heading_deg or h1
                delta = ((h2 - h1 + 540) % 360) - 180
                heading = (h1 + delta * ratio) % 360
                accel = (speed - prev_speed) * fps / 3.6  # m/s^2 per frame
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
                continue
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
    return samples
