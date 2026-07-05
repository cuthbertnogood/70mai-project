#!/usr/bin/env python3
"""Render dashcam-style telemetry overlay (map, speed, compass, G-force)."""

from __future__ import annotations

import math
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gps_70mai import TelemetrySample, build_telemetry_samples, find_gps_files, load_gps_points
from import_70mai import log

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SIZE = 256
USER_AGENT = "70mai_project/1.0 (local dashcam telemetry; contact: local)"
OSM_ATTRIBUTION = "© OpenStreetMap"

PANEL_BG = (12, 16, 24, 220)
ACCENT = (0, 210, 255)
ACCENT_DIM = (0, 140, 180)
TEXT = (240, 244, 255)
TEXT_DIM = (160, 170, 190)
TRACK_COLOR = (0, 200, 255)
TRACK_TAIL = (0, 120, 160)
MARKER = (255, 80, 60)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _fetch_tile(zoom: int, x: int, y: int, cache_dir: Path) -> Image.Image | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{zoom}_{x}_{y}.png"
    if cache_path.is_file():
        return Image.open(cache_path).convert("RGBA")
    url = TILE_URL.format(z=zoom, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read()
        cache_path.write_bytes(data)
        return Image.open(cache_path).convert("RGBA")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _render_map_base(
    samples: list[TelemetrySample],
    map_size: int,
    cache_dir: Path,
) -> tuple[Image.Image, float, float, float, float]:
    lats = [s.lat for s in samples]
    lons = [s.lon for s in samples]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    pad = max(0.0004, (max_lat - min_lat) * 0.15, (max_lon - min_lon) * 0.15)
    min_lat -= pad
    max_lat += pad
    min_lon -= pad
    max_lon += pad

    zoom = 16
    for trial in range(16, 10, -1):
        x0, y0 = _lonlat_to_tile(min_lon, max_lat, trial)
        x1, y1 = _lonlat_to_tile(max_lon, min_lat, trial)
        width_tiles = math.ceil(x1) - math.floor(x0)
        height_tiles = math.ceil(y1) - math.floor(y0)
        if width_tiles * TILE_SIZE <= map_size * 2 and height_tiles * TILE_SIZE <= map_size * 2:
            zoom = trial
            break

    x0, y0 = _lonlat_to_tile(min_lon, max_lat, zoom)
    x1, y1 = _lonlat_to_tile(max_lon, min_lat, zoom)
    tx0, ty0 = math.floor(x0), math.floor(y0)
    tx1, ty1 = math.ceil(x1), math.ceil(y1)
    mosaic = Image.new("RGBA", ((tx1 - tx0) * TILE_SIZE, (ty1 - ty0) * TILE_SIZE), (30, 34, 42, 255))
    got_tile = False
    for tx in range(tx0, tx1):
        for ty in range(ty0, ty1):
            tile = _fetch_tile(zoom, tx, ty, cache_dir)
            if tile is None:
                continue
            got_tile = True
            mosaic.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))
    if not got_tile:
        draw = ImageDraw.Draw(mosaic)
        step = 32
        for x in range(0, mosaic.width, step):
            draw.line([(x, 0), (x, mosaic.height)], fill=(50, 56, 68, 255), width=1)
        for y in range(0, mosaic.height, step):
            draw.line([(0, y), (mosaic.width, y)], fill=(50, 56, 68, 255), width=1)

    crop_w = min(mosaic.width, map_size)
    crop_h = min(mosaic.height, map_size)
    left = max(0, (mosaic.width - crop_w) // 2)
    top = max(0, (mosaic.height - crop_h) // 2)
    cropped = mosaic.crop((left, top, left + crop_w, top + crop_h))
    if cropped.width != map_size or cropped.height != map_size:
        cropped = cropped.resize((map_size, map_size), Image.Resampling.LANCZOS)
    return cropped, min_lon, max_lon, min_lat, max_lat


def _project(
    lat: float,
    lon: float,
    map_size: int,
    min_lon: float,
    max_lon: float,
    min_lat: float,
    max_lat: float,
) -> tuple[int, int]:
    if max_lon == min_lon:
        x = map_size // 2
    else:
        x = int((lon - min_lon) / (max_lon - min_lon) * (map_size - 1))
    if max_lat == min_lat:
        y = map_size // 2
    else:
        y = int((max_lat - lat) / (max_lat - min_lat) * (map_size - 1))
    return max(0, min(map_size - 1, x)), max(0, min(map_size - 1, y))


def _draw_compass(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int, heading: float) -> None:
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=ACCENT_DIM,
        width=2,
    )
    for angle, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        rad = math.radians(angle - 90)
        lx = cx + int(math.cos(rad) * (radius - 8))
        ly = cy + int(math.sin(rad) * (radius - 8))
        draw.text((lx - 4, ly - 6), label, fill=TEXT_DIM, font=_font(9))
    rad = math.radians(heading - 90)
    tip_x = cx + int(math.cos(rad) * (radius - 4))
    tip_y = cy + int(math.sin(rad) * (radius - 4))
    draw.line([(cx, cy), (tip_x, tip_y)], fill=ACCENT, width=3)
    draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=ACCENT)


def _draw_speed_gauge(draw: ImageDraw.ImageDraw, x: int, y: int, speed: float) -> None:
    speed_text = f"{speed:.0f}"
    font_big = _font(42, bold=True)
    font_small = _font(14)
    draw.text((x, y), speed_text, fill=ACCENT, font=font_big)
    bbox = draw.textbbox((x, y), speed_text, font=font_big)
    draw.text((bbox[2] + 6, y + 18), "KM/H", fill=TEXT_DIM, font=font_small)


def _draw_gforce(draw: ImageDraw.ImageDraw, x: int, y: int, g_force: float) -> None:
    draw.text((x, y), "G-FORCE", fill=TEXT_DIM, font=_font(10))
    draw.text((x, y + 14), f"{g_force:.2f}g", fill=TEXT, font=_font(16, bold=True))


def _format_coords(lat: float, lon: float) -> str:
    lat_dir = "N" if lat >= 0 else "S"
    lon_dir = "E" if lon >= 0 else "W"
    return f"{abs(lat):.5f}° {lat_dir}  {abs(lon):.5f}° {lon_dir}"


def render_overlay_frame(
    *,
    map_base: Image.Image,
    samples_upto: list[TelemetrySample],
    sample: TelemetrySample,
    map_size: int,
    panel_width: int,
    hud_height: int,
    min_lon: float,
    max_lon: float,
    min_lat: float,
    max_lat: float,
) -> Image.Image:
    panel = Image.new("RGBA", (panel_width, map_size + hud_height), PANEL_BG)
    map_layer = map_base.copy()
    draw_map = ImageDraw.Draw(map_layer)

    if len(samples_upto) >= 2:
        points = [
            _project(s.lat, s.lon, map_size, min_lon, max_lon, min_lat, max_lat)
            for s in samples_upto
        ]
        for idx in range(1, len(points)):
            color = TRACK_TAIL if idx < len(points) - 20 else TRACK_COLOR
            draw_map.line([points[idx - 1], points[idx]], fill=color, width=3)

    cx, cy = _project(
        sample.lat,
        sample.lon,
        map_size,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
    )
    heading_rad = math.radians(sample.heading_deg - 90)
    arrow_len = 14
    tip = (cx + int(math.cos(heading_rad) * arrow_len), cy + int(math.sin(heading_rad) * arrow_len))
    draw_map.line([(cx, cy), tip], fill=MARKER, width=3)
    draw_map.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=MARKER, outline=(255, 255, 255))

    panel.paste(map_layer, (0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, map_size - 18, panel_width, map_size), fill=(12, 16, 24, 180))
    draw.text((6, map_size - 16), OSM_ATTRIBUTION, fill=TEXT_DIM, font=_font(9))

    hud_y = map_size + 8
    draw.text((10, hud_y), sample.timestamp.strftime("%Y-%m-%d %H:%M:%S"), fill=TEXT, font=_font(11))
    draw.text((10, hud_y + 16), _format_coords(sample.lat, sample.lon), fill=TEXT_DIM, font=_font(10))
    _draw_speed_gauge(draw, panel_width - 130, hud_y + 4, sample.speed_kmh)
    _draw_gforce(draw, 10, hud_y + 38, sample.g_force)
    _draw_compass(draw, panel_width - 36, hud_y + 52, 24, sample.heading_deg)
    return panel


def render_telemetry_video(
    *,
    gps_sources: list[Path],
    wall_start: datetime,
    duration_sec: float,
    fps: int,
    output: Path,
    map_size: int = 280,
    cache_dir: Path | None = None,
    update_hz: int = 5,
) -> bool:
    wall_end = datetime.fromtimestamp(wall_start.timestamp() + duration_sec)
    points = load_gps_points(gps_sources, wall_start, wall_end)
    if not points:
        log("Telemetry: no GPS points for this time range — skipping overlay.")
        return False

    samples = build_telemetry_samples(points, wall_start, duration_sec, update_hz)
    if not samples:
        return False

    cache = cache_dir or Path.home() / ".cache" / "70mai" / "map_tiles"
    panel_width = map_size
    hud_height = 96
    map_base, min_lon, max_lon, min_lat, max_lat = _render_map_base(
        samples, map_size, cache
    )

    log(
        f"Telemetry: {len(points)} GPS points -> {len(samples)} overlay frames "
        f"({map_size}px map, {update_hz} Hz)"
    )

    with tempfile.TemporaryDirectory(prefix="70mai_telemetry_") as tmp:
        tmp_path = Path(tmp)
        for idx, sample in enumerate(samples):
            if idx % 25 == 0:
                log(f"  Rendering overlay frame {idx + 1}/{len(samples)}")
            frame = render_overlay_frame(
                map_base=map_base,
                samples_upto=samples[: idx + 1],
                sample=sample,
                map_size=map_size,
                panel_width=panel_width,
                hud_height=hud_height,
                min_lon=min_lon,
                max_lon=max_lon,
                min_lat=min_lat,
                max_lat=max_lat,
            )
            frame.save(tmp_path / f"frame_{idx:06d}.png")

        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(update_hz),
            "-i",
            str(tmp_path / "frame_%06d.png"),
            "-frames:v",
            str(len(samples)),
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            str(output),
        ]
        subprocess.run(cmd, check=True)
    return True


def resolve_gps_sources(
    gps_dir: Path | None,
    *fallbacks: Path | None,
) -> list[Path]:
    return find_gps_files(gps_dir, *fallbacks)
