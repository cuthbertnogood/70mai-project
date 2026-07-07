#!/usr/bin/env python3
"""Render dashcam-style telemetry overlay (map, speed, compass, G-force)."""

from __future__ import annotations

import math
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gps_70mai import (
    TelemetrySample,
    build_telemetry_samples,
    estimate_gps_offset,
    find_gps_files,
    load_gps_points,
)
from import_70mai import log

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SIZE = 256
USER_AGENT = "70mai_project/1.0 (local dashcam telemetry; contact: local)"
OSM_ATTRIBUTION = "© OpenStreetMap"

MAP_OPACITY = 0.38
HUD_BG = (12, 16, 24, 150)
ACCENT = (0, 210, 255)
ACCENT_DIM = (0, 140, 180)
TEXT = (240, 244, 255)
TEXT_DIM = (160, 170, 190)
TRACK_COLOR = (0, 200, 255, 220)
TRACK_TAIL = (0, 120, 160, 140)
MARKER = (255, 80, 60, 255)


@dataclass(frozen=True)
class MapViewport:
    zoom: int
    min_tx: float
    max_tx: float
    min_ty: float
    max_ty: float
    map_size: int


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


def _apply_map_opacity(image: Image.Image, opacity: float) -> Image.Image:
    image = image.convert("RGBA")
    alpha = image.getchannel("A").point(lambda value: int(value * opacity))
    image.putalpha(alpha)
    return image


def _fetch_tile(zoom: int, x: int, y: int, cache_dir: Path) -> Image.Image | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{zoom}_{x}_{y}.png"
    if cache_path.is_file():
        tile = Image.open(cache_path).convert("RGBA")
    else:
        url = TILE_URL.format(z=zoom, x=x, y=y)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read()
            cache_path.write_bytes(data)
            tile = Image.open(cache_path).convert("RGBA")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
    return _apply_map_opacity(tile, MAP_OPACITY)


def _build_viewport(samples: list[TelemetrySample], map_size: int) -> MapViewport:
    zoom = 16
    for trial in range(16, 11, -1):
        xs = [_lonlat_to_tile(s.lon, s.lat, trial)[0] for s in samples]
        ys = [_lonlat_to_tile(s.lon, s.lat, trial)[1] for s in samples]
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        max_span = max(span_x, span_y, 0.0005)
        if max_span * TILE_SIZE <= map_size * 1.2:
            zoom = trial
            break

    xs = [_lonlat_to_tile(s.lon, s.lat, zoom)[0] for s in samples]
    ys = [_lonlat_to_tile(s.lon, s.lat, zoom)[1] for s in samples]
    min_tx, max_tx = min(xs), max(xs)
    min_ty, max_ty = min(ys), max(ys)
    pad = max(0.0008, (max_tx - min_tx) * 0.2, (max_ty - min_ty) * 0.2)
    min_tx -= pad
    max_tx += pad
    min_ty -= pad
    max_ty += pad
    span_x = max(max_tx - min_tx, 0.0005)
    span_y = max(max_ty - min_ty, 0.0005)
    if span_x > span_y:
        extra = (span_x - span_y) / 2
        min_ty -= extra
        max_ty += extra
    else:
        extra = (span_y - span_x) / 2
        min_tx -= extra
        max_tx += extra
    return MapViewport(
        zoom=zoom,
        min_tx=min_tx,
        max_tx=max_tx,
        min_ty=min_ty,
        max_ty=max_ty,
        map_size=map_size,
    )


def _render_map_base(
    samples: list[TelemetrySample],
    viewport: MapViewport,
    cache_dir: Path,
) -> Image.Image:
    tx0 = math.floor(viewport.min_tx)
    ty0 = math.floor(viewport.min_ty)
    tx1 = math.ceil(viewport.max_tx)
    ty1 = math.ceil(viewport.max_ty)
    mosaic = Image.new("RGBA", ((tx1 - tx0) * TILE_SIZE, (ty1 - ty0) * TILE_SIZE), (0, 0, 0, 0))
    got_tile = False
    for tx in range(tx0, tx1):
        for ty in range(ty0, ty1):
            tile = _fetch_tile(viewport.zoom, tx, ty, cache_dir)
            if tile is None:
                continue
            got_tile = True
            mosaic.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE), tile)
    if not got_tile:
        draw = ImageDraw.Draw(mosaic)
        step = 32
        for x in range(0, mosaic.width, step):
            draw.line([(x, 0), (x, mosaic.height)], fill=(80, 90, 110, 60), width=1)
        for y in range(0, mosaic.height, step):
            draw.line([(0, y), (mosaic.width, y)], fill=(80, 90, 110, 60), width=1)

    left = int((viewport.min_tx - tx0) * TILE_SIZE)
    top = int((viewport.min_ty - ty0) * TILE_SIZE)
    width = max(1, int((viewport.max_tx - viewport.min_tx) * TILE_SIZE))
    height = max(1, int((viewport.max_ty - viewport.min_ty) * TILE_SIZE))
    cropped = mosaic.crop((left, top, left + width, top + height))
    return cropped.resize((viewport.map_size, viewport.map_size), Image.Resampling.LANCZOS)


def _project(lat: float, lon: float, viewport: MapViewport) -> tuple[int, int]:
    tx, ty = _lonlat_to_tile(lon, lat, viewport.zoom)
    if viewport.max_tx == viewport.min_tx:
        x = viewport.map_size // 2
    else:
        x = int((tx - viewport.min_tx) / (viewport.max_tx - viewport.min_tx) * (viewport.map_size - 1))
    if viewport.max_ty == viewport.min_ty:
        y = viewport.map_size // 2
    else:
        y = int((ty - viewport.min_ty) / (viewport.max_ty - viewport.min_ty) * (viewport.map_size - 1))
    return max(0, min(viewport.map_size - 1, x)), max(0, min(viewport.map_size - 1, y))


def _draw_compass(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int, heading: float) -> None:
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=ACCENT_DIM + (200,),
        width=2,
    )
    for angle, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        rad = math.radians(angle - 90)
        lx = cx + int(math.cos(rad) * (radius - 8))
        ly = cy + int(math.sin(rad) * (radius - 8))
        draw.text((lx - 4, ly - 6), label, fill=TEXT_DIM + (220,), font=_font(9))
    rad = math.radians(heading - 90)
    tip_x = cx + int(math.cos(rad) * (radius - 4))
    tip_y = cy + int(math.sin(rad) * (radius - 4))
    draw.line([(cx, cy), (tip_x, tip_y)], fill=ACCENT + (255,), width=3)
    draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=ACCENT + (255,))


def _draw_speed_gauge(draw: ImageDraw.ImageDraw, x: int, y: int, speed: float) -> None:
    speed_text = f"{speed:.0f}"
    font_big = _font(42, bold=True)
    font_small = _font(14)
    draw.text((x, y), speed_text, fill=ACCENT + (255,), font=font_big)
    bbox = draw.textbbox((x, y), speed_text, font=font_big)
    draw.text((bbox[2] + 6, y + 18), "KM/H", fill=TEXT_DIM + (220,), font=font_small)


def _draw_gforce(draw: ImageDraw.ImageDraw, x: int, y: int, g_force: float) -> None:
    draw.text((x, y), "G-FORCE", fill=TEXT_DIM + (220,), font=_font(10))
    draw.text((x, y + 14), f"{g_force:.2f}g", fill=TEXT + (255,), font=_font(16, bold=True))


def _format_coords(lat: float, lon: float) -> str:
    lat_dir = "N" if lat >= 0 else "S"
    lon_dir = "E" if lon >= 0 else "W"
    return f"{abs(lat):.5f}° {lat_dir}  {abs(lon):.5f}° {lon_dir}"


def render_overlay_frame(
    *,
    map_base: Image.Image,
    samples_upto: list[TelemetrySample],
    sample: TelemetrySample,
    viewport: MapViewport,
    panel_width: int,
    hud_height: int,
) -> Image.Image:
    map_size = viewport.map_size
    panel = Image.new("RGBA", (panel_width, map_size + hud_height), (0, 0, 0, 0))
    map_layer = map_base.copy()
    draw_map = ImageDraw.Draw(map_layer)

    if len(samples_upto) >= 2:
        points = [_project(s.lat, s.lon, viewport) for s in samples_upto]
        for idx in range(1, len(points)):
            color = TRACK_TAIL if idx < len(points) - 20 else TRACK_COLOR
            draw_map.line([points[idx - 1], points[idx]], fill=color, width=3)

    cx, cy = _project(sample.lat, sample.lon, viewport)
    heading_rad = math.radians(sample.heading_deg - 90)
    arrow_len = 14
    tip = (cx + int(math.cos(heading_rad) * arrow_len), cy + int(math.sin(heading_rad) * arrow_len))
    draw_map.line([(cx, cy), tip], fill=MARKER, width=3)
    draw_map.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=MARKER, outline=(255, 255, 255, 255))

    panel.paste(map_layer, (0, 0), map_layer)
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, map_size - 18, panel_width, map_size), fill=(0, 0, 0, 90))
    draw.text((6, map_size - 16), OSM_ATTRIBUTION, fill=TEXT_DIM + (180,), font=_font(9))

    draw.rectangle((0, map_size, panel_width, map_size + hud_height), fill=HUD_BG)
    hud_y = map_size + 8
    draw.text((10, hud_y), sample.timestamp.strftime("%Y-%m-%d %H:%M:%S"), fill=TEXT + (255,), font=_font(11))
    draw.text((10, hud_y + 16), _format_coords(sample.lat, sample.lon), fill=TEXT_DIM + (230,), font=_font(10))
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
    gps_offset_sec: float | None = None,
) -> bool:
    wall_end = datetime.fromtimestamp(wall_start.timestamp() + duration_sec)
    offset = gps_offset_sec
    if offset is None:
        offset = estimate_gps_offset(gps_sources, wall_start, wall_end)
        log(f"Telemetry: auto GPS offset {offset:+.0f} sec")

    points = load_gps_points(gps_sources, wall_start, wall_end, offset_sec=offset)
    if not points:
        log("Telemetry: no GPS points for this time range — skipping overlay.")
        return False

    samples = build_telemetry_samples(points, wall_start, duration_sec, update_hz)
    if not samples:
        return False

    cache = cache_dir or Path.home() / ".cache" / "70mai" / "map_tiles"
    viewport = _build_viewport(samples, map_size)
    map_base = _render_map_base(samples, viewport, cache)
    panel_width = map_size
    hud_height = 96

    log(
        f"Telemetry: {len(points)} GPS points -> {len(samples)} overlay frames "
        f"({map_size}px map, {update_hz} Hz, offset {offset:+.0f}s)"
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
                viewport=viewport,
                panel_width=panel_width,
                hud_height=hud_height,
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
