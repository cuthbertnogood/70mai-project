# 70mai Video Import

Import and merge 70mai A810 SD card clips into ~10 minute videos.

## Requirements

- Python 3.9+
- ffmpeg (`brew install ffmpeg`)

## SD Card Layout

The script reads from a mounted 70mai card:

```
/Volumes/Untitled/
├── Normal/Front/*.MP4
├── Normal/Back/*.MP4
├── Event/Front/*.MP4
├── Event/Back/*.MP4
├── Parking/Front/*.MP4
└── Parking/Back/*.MP4
```

Hidden `.s_Front` preview copies are ignored.

## Usage

Scan the SD card to see which date/time ranges contain data (fast, no ffmpeg). Always includes **events** and **GPS tracks**, even when `--types` is narrowed:

```bash
python3 import_70mai.py --scan
```

Example output:

```
Scanning /Volumes/Untitled
Session gap: 120 sec (pauses longer than this start a new range)

=== Overall ===
  video: 1906 clips | 2026-04-25 13:01:19 -> 2026-04-28 14:48:28
  video days: 2026-04-25 .. 2026-04-28
  GPS:   818315 points in 2 file(s) | 2025-03-18 03:40:07 -> 2026-04-27 00:53:56
  GPS days: 2025-03-18 .. 2026-04-27

=== By type / camera ===

Normal / Front — 520 clips, 2026-04-25 13:01:19 -> 2026-04-28 12:00:00
  3 recording session(s):
    1. 2026-04-25 13:01:19 -> 2026-04-25 18:30:00 (156 clips)
    ...

=== Events ===

Event / Front — 237 event(s), 2026-02-21 07:15:47 -> 2026-04-27 08:47:48
  2026-04-27  (3 event(s))
    08:47:48  EV20260427-084748-032775F.MP4
    ...

=== GPS tracks ===
  2 file(s) | 818315 points | 2025-03-18 03:40:07 -> 2026-04-27 00:53:56
  calendar days: 2025-03-18 .. 2026-04-27

  GPSData000002.txt — 63.0 MB, 722970 points
    range: 2025-03-18 03:40:07 -> 2026-04-12 13:12:09
  GPSData000003.txt — 8.3 MB, 95010 points
    range: 2026-04-12 13:16:24 -> 2026-04-27 00:53:56

=== By date (video) ===
  2026-04-25  13:01:19 — 23:58:42  | 890 clips | Event/Front, Normal/Back, Normal/Front
  2026-04-26  07:30:00 — 22:45:11  | 650 clips | Normal/Back, Normal/Front
```

Use the ranges from `--scan` to pick `--date` / `--from-time` / `--to-time` for export.

Preview the merge plan without writing files:

```bash
python3 import_70mai.py --dry-run
```

Run the full import:

```bash
python3 import_70mai.py \
  --source /Volumes/Untitled \
  --output ./video/Output \
  --chunk-minutes 10 \
  --gap-seconds 120
```

Process only one type or camera:

```bash
python3 import_70mai.py --types Normal --cameras Front
```

## Export Parameters

You can limit which clips are imported from the SD card by date and time. A clip is included when its **start timestamp** (parsed from the filename) falls within the range: `start <= timestamp < end` (end is exclusive).

### Option 1: date + time window (recommended)

Set a calendar day and optional hour/minute bounds:

| Flag | Description |
|------|-------------|
| `--date DATE` | Day to export. Required when using `--from-time` / `--to-time`. |
| `--from-time HH:MM` | Range start on that day. Default: `00:00` if omitted. |
| `--to-time HH:MM` | Range end on that day (exclusive). Default: `23:59:59` if omitted. |

```bash
# Export Normal recording from 08:00 to 09:00 on 27 Apr 2026
python3 import_70mai.py \
  --date 04-27-2026 \
  --from-time 08:00 \
  --to-time 09:00

# Whole day (00:00 – 23:59:59)
python3 import_70mai.py --date 2026-04-27

# From 14:30 until end of day
python3 import_70mai.py --date 2026-04-27 --from-time 14:30
```

`--from-time` / `--to-time` also accept seconds: `08:00:30`.

### Option 2: full datetime range

Use `--from` and `--to` for ranges that span multiple days or need explicit datetimes:

| Flag | Description |
|------|-------------|
| `--from DATETIME` | Range start (inclusive). |
| `--to DATETIME` | Range end (exclusive). |

```bash
python3 import_70mai.py \
  --from "2026-04-27 08:00" \
  --to "2026-04-27 09:00"

# Multi-day export
python3 import_70mai.py \
  --from "2026-04-27 08:00" \
  --to "2026-04-28 18:00"
```

### Accepted date/time formats

| Format | Example |
|--------|---------|
| `YYYY-MM-DD` | `2026-04-27` |
| `YYYY-MM-DD HH:MM` | `2026-04-27 08:00` |
| `YYYY-MM-DD HH:MM:SS` | `2026-04-27 08:00:30` |
| `MM-DD-YYYY` | `04-27-2026` |
| `MM-DD-YYYY HH:MM` | `04-27-2026 08:00` |
| `MM-DD-YYYY HH:MM:SS` | `04-27-2026 08:00:30` |
| `HH:MM` / `HH:MM:SS` | for `--from-time` / `--to-time` only |

### Combine with other filters

Export parameters work together with `--types`, `--cameras`, `--chunk-minutes`, and `--dry-run`:

```bash
python3 import_70mai.py \
  --date 04-27-2026 \
  --from-time 08:00 \
  --to-time 12:00 \
  --types Normal \
  --cameras Front \
  --dry-run
```

When a range is active, the script prints it at startup:

```
Range:   2026-04-27 08:00:00 -> 2026-04-27 09:00:00
```

### All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--source PATH` | `/Volumes/Untitled` | Mounted SD card path |
| `--output PATH` | `./video` | Output directory for merged files |
| `--chunk-minutes N` | `10` | Target length of each merged file (minutes) |
| `--gap-seconds N` | `120` | New session if gap between clips exceeds this |
| `--types LIST` | `Normal,Event,Parking` | Comma-separated record types |
| `--cameras LIST` | `Front,Back` | Comma-separated cameras |
| `--dry-run` | off | Preview merge plan without writing files |
| `--scan` | off | Scan SD card: video ranges, events list, GPS tracks |
| `--date DATE` | — | Export day (see above) |
| `--from-time HH:MM` | `00:00` | Start time on `--date` |
| `--to-time HH:MM` | `23:59:59` | End time on `--date` (exclusive) |
| `--from DATETIME` | — | Range start (inclusive) |
| `--to DATETIME` | — | Range end (exclusive) |

Run `python3 import_70mai.py --help` for the built-in reference.

## Output

Merged files are written to:

```
video/Output/
├── Normal/
│   ├── Front/NO_20260425-130119_131019_F.mp4
│   └── Back/
├── Event/
└── Parking/
```

Source files (e.g. screen recordings) stay in `video/`.
Composite 3-camera videos from `compose_70mai.py` are saved to `video/Output/` as well.

Naming format:

```
{TYPE}_{YYYYMMDD-HHMMSS}_{HHMMSS}_{F|B}.mp4
```

## How It Works

1. Scan clips and parse timestamps from filenames like `NO20260425-130119-040747F.MP4`
2. Split into recording sessions when the gap between clips exceeds 120 seconds
3. Group each session into chunks of about 10 minutes using ffprobe durations
4. Merge with `ffmpeg -f concat -c copy` without re-encoding

Existing output files are skipped, so the script can be resumed safely.

## Progress Output

The script prints live progress while running:

```
Found 1906 clips in 6 group(s)
Estimated output files: ~320

== Probing clip durations: 45/1906 (2%) | elapsed 1m 12s | ETA 52m 03s | NO20260425-131019-040756F.MP4
== Merging output files: 3/320 (1%) | elapsed 2m 05s | ETA 3h 41m 12s | planned

>>> Group 2/6: Normal/Back
--- Normal/Back: 463 clips, 11 sessions ---
  session 1/11: 52 clips, probing 52 new file(s)...
  merging 10 clips (10.0 min) -> NO_20260425-130119_131019_F.mp4
```

Each line is flushed immediately, so you can follow progress in the terminal or in a log file:

```bash
python3 import_70mai.py 2>&1 | tee import.log
```

## Compose acceleration

[`compose_70mai.py`](compose_70mai.py) builds a vertical 3-camera video (screen + Front + Back) and re-encodes it. That step is CPU-heavy without hardware help.

### Profiles

Use `--profile` instead of tuning flags manually:

| Profile | Use case | HW encode | Bitrate | Width | FPS |
|---------|----------|-----------|---------|-------|-----|
| `balanced` | Default archive export (recommended) | yes | 6.5 Mbps | 1206 | 25 |
| `draft` | Sync check / preview | yes | 5.0 Mbps | 960 | 20 |
| `quality` | Higher bitrate archive | yes | 7.5 Mbps | 1206 | 25 |

Profiles set **hw encode + quality/resolution presets** only (same pipeline as `--hw`). They do not enable hardware decode or `scale_vt` — on tested Macs that full GPU pipeline is slower than hw-encode-only because `vstack` runs on CPU after `hwdownload`.

```bash
# Recommended for 10-minute composites
python3 compose_70mai.py "video/ScreenRecording_....mp4" \
  --profile balanced \
  -d 600
```

### Manual flags

| Flag | Description |
|------|-------------|
| `--hw` | VideoToolbox H.264 encode (CPU decode/scale) — fastest on tested Mac |
| `--profile balanced` | Same as `--hw` plus tuned width/fps/bitrate (recommended default) |
| `--hw-decode` | Experimental: opt into hw decode + optional `scale_vt` (see below) |
| `--no-vt-scale` | With `--hw-decode`, use CPU `scale=` instead of `scale_vt` |
| `--hw-quality N` | Target bitrate `N×100` kbps (default 65 → 6.5 Mbps) |

**Recommended:** `--profile balanced` or `--hw` for exports. Both use CPU decode/scale + VideoToolbox encode.

**Experimental `--hw-decode`:** tries progressively heavier GPU pipelines, fastest first: hw encode only → hw decode + CPU scale → full VT (`scale_vt`). Full VT is often *slower* on Apple Silicon because stacking still hits CPU after GPU frames are downloaded. Use only if you want to experiment; the script falls back automatically on failure.

### Benchmark

Run a 60-second comparison (software vs hw-encode vs profile vs experimental full VT):

```bash
python3 benchmark_compose.py
```

Results are written to `video/Output/compose_benchmark_results.md`. Latest 60s run on this Mac:

| Mode | Wall time | ffmpeg speed |
|------|-----------|--------------|
| libx264 medium | ~4.7 min | 0.22× |
| `--hw` | ~2.2 min | 0.48× |
| `--profile balanced` | ~2.0 min | 0.53× |
| `--profile balanced --hw-decode` | ~1.9 min | 0.53× (fast-first fallback → same as balanced) |

When profiles previously defaulted to full VT (`hw_decode` + `scale_vt`), the same machine measured **~13.1 min** — full VT is slower because `vstack` still runs on CPU after `hwdownload`.

## Compose: sync and audio

Video sync uses the Screen Recording filename as the time base; Front/Back offsets are computed from merged clip timestamps (see `--sync-offset-front` / `--sync-offset-back` for manual tweaks).

### Automatic audio analysis (default)

Before encoding, `compose_70mai.py` extracts ~12 seconds of audio at **t≈30 s** and compares the **music-band envelope** (300–3000 Hz) between screen system audio and the front dashcam mic:

| Envelope correlation | `--audio` mode | Output sound |
|---------------------|----------------|--------------|
| ≥ 0.45 | `mix` | Screen + front (front at 65% volume) |
| 0.15 – 0.45 | `front` | Front dashcam mic only |
| < 0.15 | `screen` | Screen recording only (nav/music) |

The script also estimates **`--audio-offset`** for front audio: positive value delays front (typical ~+0.5 s when iOS system audio lags video). Example log line:

```
Audio analyze: envelope_corr=0.611 waveform_corr=0.098 RMS screen=6635 front=2250
Audio:         mix (auto, offset_front=+0.50s)
```

Waveform cross-correlation is weak between screen and dashcam (different sources: digital system audio vs cabin mic), so mode selection uses **envelope** correlation, not raw samples.

Requires **numpy** and **scipy** for analysis. If missing, falls back to `screen` audio.

### Audio flags

| Flag | Default | Description |
|------|---------|-------------|
| `--audio` | `auto` | `auto`, `screen`, `front`, or `mix` |
| `--audio-offset SEC` | from analysis | Shift front audio vs screen (+ delays front) |
| `--no-audio-analyze` | off | Skip analysis; use `screen` and offset `0` |

```bash
# Auto (recommended)
python3 compose_70mai.py "video/ScreenRecording_....mp4" \
  --profile draft -d 600

# Force mix with manual offset
python3 compose_70mai.py "video/ScreenRecording_....mp4" \
  --audio mix --audio-offset 0.5 -d 600
```

## Notes

- Front camera: 3840x2160, Back camera: 1920x1080
- GPS data stays in `GPSData*.txt` on the SD card and is not merged
- Full import of all types and cameras needs ~360 GB free disk space
