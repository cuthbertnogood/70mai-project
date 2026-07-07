# 70mai Video Import

Import and merge 70mai A810 SD card clips into ~10 minute videos.

## Requirements

- Python **3.10+** (recommended: **3.12** via Homebrew — system `/usr/bin/python3` 3.9 uses LibreSSL and triggers Google API warnings)
- ffmpeg (`brew install ffmpeg`)

### Python setup

First time only (or after dependency changes):

```bash
scripts/setup-venv.sh
```

All CLI scripts **auto-use `.venv`** — you can keep running `python3 publish_70mai.py ...` as before; if `.venv` is missing, it is created on first run.

Alternative launcher:

```bash
./run publish_70mai.py --source /Volumes/Untitled --estimate-only
```

## SD Card Layout

The script reads from a mounted 70mai card:

```
/Volumes/Untitled/
├── Normal/Front/*.MP4    [NO] continuous recording
├── Normal/Back/*.MP4
├── Event/Front/*.MP4     [EV] impact / collision events
├── Event/Back/*.MP4
├── Parking/Front/*.MP4   [PA] parking mode
├── Parking/Back/*.MP4
├── Lapse/Front/*.MP4     [LA] timelapse (may be empty)
├── Lapse/Back/*.MP4
├── Photo/Front/*.JPG     [PH] snapshot photos
├── Photo/Back/*.JPG
└── GPSData*.txt          GPS track logs
```

| Type | Prefix | Format | Description |
|------|--------|--------|-------------|
| Normal | NO | MP4 | Continuous loop recording (~1 min clips) |
| Event | EV | MP4 | Impact / collision / manual save events |
| Parking | PA | MP4 | Parking mode recordings |
| Lapse | LA | MP4 | Timelapse recordings |
| Photo | PH | JPG | Snapshot photos |

Hidden `.s_Front` preview copies are ignored.

## Usage

Scan the SD card for a **full inventory** (all types, sizes, date ranges, events, photos, GPS). No ffmpeg needed:

```bash
python3 import_70mai.py --scan
```

The scan always checks **all record types** (Normal, Event, Parking, Lapse, Photo) plus GPS — regardless of `--types`.

Example output:

```
=== Record types (70mai A810) ===
  Normal   [NO] .MP4  Continuous loop recording (~1 min clips)
  Event    [EV] .MP4  Impact / collision / manual save events
  ...

=== Card inventory ===
  Normal [NO] — Continuous loop recording (~1 min clips)
    Front   463 files, 116.9 GB  |  2026-04-25 13:01:19 -> 2026-04-27 08:56:55
    Back    463 files,  29.1 GB  |  ...
  Event [EV] — ...
  Parking [PA] — ...
  Lapse [LA] — (empty)
  Photo [PH] — ...
  Total media: 1908 files, 223.7 GB

=== Overall ===
  video: 1906 clips | 2024-12-28 -> 2026-04-27
  GPS:   806962 points in 2 file(s), 71.3 MB | 2025-03-18 -> 2026-04-27
  photos: 2 file(s) | 2024-10-15 07:53:45 -> ...

=== Events ===
  (each event listed by date and time)

=== Photos ===
=== GPS tracks ===
=== By date (video) ===
```

Use the ranges from `--scan` to pick `--date` / `--from-time` / `--to-time` for export.

### Export events (one file per event)

Events are short clips — export each as a separate file without merging:

```bash
# All events, both cameras
python3 import_70mai.py --export-events

# Preview first
python3 import_70mai.py --export-events --dry-run

# Filter by date / camera
python3 import_70mai.py --export-events \
  --date 2026-04-27 \
  --from-time 08:00 \
  --to-time 09:00 \
  --cameras Front
```

Output: `video/Output/Event/Front/EV_20260427-084748_F.mp4` (lossless copy from SD card).

Respects the same `--date`, `--from`/`--to`, and `--cameras` filters as normal export. Does not require ffmpeg.

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
| `--scan` | off | Full card scan: inventory, ranges, events, photos, GPS |
| `--export-events` | off | Export each Event clip as a separate file (copy) |
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

Live **progress bars** in the terminal (in-place on TTY):

```
TOTAL: [████████░░░░░░░░░░░░░░░░░░░░░░░░] 42/1926 (2.2%) | probing Normal/Front | 3m 12s | ETA 2h 05m
Probe: [██████████████████░░░░░░░░░░░░░░░░] 854/1906 (44.8%) | 7m 18s elapsed | ETA 9m 02s
```

When output is piped to a log file (`tee`), bars are printed as periodic text lines instead.

Parallel **ffprobe** (8 workers) speeds up duration detection before merge.

Each line is flushed immediately:

```bash
python3 import_70mai.py 2>&1 | tee import.log
```

## Compose acceleration

[`compose_70mai.py`](compose_70mai.py) builds a vertical 3-camera video (screen + Front + Back) and re-encodes it. That step is CPU-heavy without hardware help.

During encoding, a live **progress bar** is shown (in-place on TTY):

```
Encode: [████████░░░░░░░░░░░░░░░░░░░░░░░░] 12m 05s/35m 10s (34.2%) | 22m 18s elapsed | ETA 42m 50s | speed 0.53x
```

When output is piped to a log file (`tee`), progress is printed as periodic text lines instead.

### Profiles

Use `--profile` instead of tuning flags manually:

| Profile | Use case | HW encode | Bitrate | Width | FPS |
|---------|----------|-----------|---------|-------|-----|
| `balanced` | **Default** — archive export | yes | 6.5 Mbps | 1206 | 25 |
| `draft` | Sync check / preview | yes | 5.0 Mbps | 960 | 20 |
| `quality` | Higher bitrate archive | yes | 7.5 Mbps | 1206 | 25 |

Profiles set **hw encode + quality/resolution presets** only (same pipeline as `--hw`). They do not enable hardware decode or `scale_vt` — on tested Macs that full GPU pipeline is slower than hw-encode-only because `vstack` runs on CPU after `hwdownload`.

Default CLI: `--profile balanced` and `-d 600` (10 minutes). A minimal export:

```bash
python3 compose_70mai.py "video/ScreenRecording_....mp4"
```

Quick 60-second test:

```bash
python3 compose_70mai.py "video/ScreenRecording_....mp4" -d 60
```

Draft preview (lower resolution/fps):

```bash
python3 compose_70mai.py "video/ScreenRecording_....mp4" --profile draft
```

### Manual flags

| Flag | Description |
|------|-------------|
| `--hw` | VideoToolbox H.264 encode (CPU decode/scale) — same pipeline as default profile |
| `--profile NAME` | Override default `balanced` with `draft` or `quality` |
| `--hw-decode` | Experimental: opt into hw decode + optional `scale_vt` (see below) |
| `--no-vt-scale` | With `--hw-decode`, use CPU `scale=` instead of `scale_vt` |
| `--hw-quality N` | Target bitrate `N×100` kbps (default 65 → 6.5 Mbps) |

**Default:** `--profile balanced -d 600` — CPU decode/scale + VideoToolbox encode (~20 min for a 10-minute composite on tested Mac).

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
# Auto (default: balanced profile, 10 min)
python3 compose_70mai.py "video/ScreenRecording_....mp4"

# Draft preview
python3 compose_70mai.py "video/ScreenRecording_....mp4" --profile draft

# Force mix with manual offset
python3 compose_70mai.py "video/ScreenRecording_....mp4" \
  --audio mix --audio-offset 0.5
```

## Publish plan (2-cam, trip-based chunks)

Before compose/upload, estimate trip-based chunks with `plan_estimate.py`. It probes clips, groups **trips** (gap >120 s between clip starts, same as import), and packs them into upload chunks:

- Trip **≥ target** (default 2 h): one chunk (whole trip, even if longer).
- Trip **< target**: merge with following trips until sum **≥ target**.
- Short tail at the end: final chunk.

Output: stdout summary + append to `video/Output/publish_plan.md`.

```bash
# Normal driving only (typical first step)
python3 plan_estimate.py --source /Volumes/Untitled --types Normal

# All record types on SD card
python3 plan_estimate.py --source /Volumes/Untitled \
  --types Normal Event Parking

# Custom target chunk size (minutes)
python3 plan_estimate.py --source /Volumes/Untitled --chunk-minutes 120
```

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | `/Volumes/Untitled` | SD card or `video/Output`-style tree |
| `--types` | `Normal` | `Normal`, `Event`, `Parking` |
| `--chunk-minutes` | `120` | Target chunk size (minutes) |
| `--chunk-mode` | `trips` | Trip packing (`fixed` not implemented yet) |
| `--session-gap` | `120` | New trip after N seconds between clips |
| `--plan-file` | `video/Output/publish_plan.md` | Append markdown report |
| `--no-write` | off | Skip writing plan file |
| `--check-disk` | `.` | Path for free-disk check |

Example (current SD card, Normal only): **7h 30m** → **5 YouTube uploads**, peak chunk **~7.7 GB** (`balanced` estimate ~45 MB/min).

Full pipeline (`compose_2cam_70mai.py`, `publish_70mai.py`, YouTube upload) — use `plan_estimate.py` to preview chunks first.

### Compose 2-cam (Front + Back)

Vertical stack without Screen Recording. Sync by wall-clock (`--from` + `--to` / `-d`). Small gaps between merged clip boundaries (~tens of seconds) are skipped automatically; output may be slightly shorter than the wall-clock range.

```bash
# 60-second test
python3 compose_2cam_70mai.py --from "2026-04-27 08:13:38" -d 60 \
  -o video/Output/test_2cam_60s.mp4

# Default profile balanced (same as compose_70mai)
python3 compose_2cam_70mai.py --from "2026-04-25 13:01:19" --to "2026-04-25 13:46:49"
```

| Flag | Default | Description |
|------|---------|-------------|
| `--from DATETIME` | — | Wall-clock start (required) |
| `--to` / `-d` | — | End or duration (one required) |
| `--video-dir` | `video/Output` | Merged Normal/Front + Back |
| `--profile` | `balanced` | Encode profile |
| `--audio` | `front` | `front` or `back` |

GPS telemetry overlay (`--telemetry`) is **disabled** for now — see backlog in [`GOALS.md`](GOALS.md).

### Publish (trip chunks → YouTube)

```bash
# Dependencies: scripts/setup-venv.sh (once)
# OAuth: save Desktop client JSON to ~/.config/70mai/youtube_credentials.json

# Preview plan only
python3 publish_70mai.py --source /Volumes/Untitled --types Normal --estimate-only

# Compose chunk 5 only (short tail, good test) — no YouTube
python3 publish_70mai.py --source /Volumes/Untitled --types Normal \
  --compose-only --dry-run

# Full publish (needs OAuth; private by default)
python3 publish_70mai.py --source /Volumes/Untitled --types Normal \
  --title "Поездка 70mai"

# Upload already-composed trips (trip_01 was uploaded manually)
python3 publish_70mai.py --source /Volumes/Untitled --types Normal --chunk 1 \
  --upload-only --resume-upload \
  --mark-uploaded 1:1:q9EMi4eP3kI \
  --title "70mai 2026-04-25" --playlist "70mai 2026-04-25 Normal"

# Full per-trip loop: compose → upload → delete next trip
python3 publish_70mai.py --source /Volumes/Untitled --types Normal \
  --per-trip-upload --resume-upload --title "70mai 2026-04-25"

# Dry-run upload queue (no OAuth)
python3 publish_70mai.py --source /Volumes/Untitled --types Normal --chunk 1 \
  --upload-only --dry-run --mark-uploaded 1:1:q9EMi4eP3kI

# Resume interrupted YouTube upload (session URI saved in .upload.json)
python3 publish_70mai.py --source /Volumes/Untitled --types Normal --chunk 1 \
  --trip 1 --per-trip-upload --resume-upload --compose-only
python3 publish_70mai.py --source /Volumes/Untitled --types Normal --chunk 1 \
  --trip 1 --per-trip-upload --resume-upload
```

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | `/Volumes/Untitled` | SD card for trip detection |
| `--video-dir` | `video/Output` | Merged clips for compose |
| `--chunk-minutes` | `120` | Target chunk size (trip packing) |
| `--chunk-mode` | `trips` | Pack by driving sessions |
| `--compose-only` | off | Skip YouTube upload |
| `--estimate-only` | off | Plan only, no ffmpeg |
| `--resume` | off | Load state file; skip chunks already marked uploaded |
| `--resume-upload` | off | Resume YouTube transfer from saved `.upload.json` |
| `--upload-only` | off | Skip compose; upload existing `chunk_NN/trip_NN.mp4` (auto `--per-trip-upload`, auto `--resume-upload`) |
| `--mark-uploaded` | — | Mark trip uploaded: `CHUNK:TRIP:VIDEO_ID` (repeatable) |
| `--continue-on-error` | off | On upload failure, continue to next trip |
| `--per-trip-upload` | off | Upload each trip separately (no concat) |
| `--trip` | all | Within chunk: only trip N (1-based) |
| `--diag-log` | `.publish_tmp/youtube_upload.diag.jsonl` | Structured upload diagnostics |
| `--no-diag` | off | Disable diagnostic JSONL |
| `--chunk` | all | Only chunk N (1-based) |
| `--keep` | off | Keep MP4 after upload (debug only) |
| `--credentials` | `~/.config/70mai/youtube_credentials.json` | OAuth client |
| `--token` | `~/.config/70mai/youtube_token.json` | Saved refresh token |

### YouTube OAuth (one-time)

1. [Google Cloud Console](https://console.cloud.google.com/) → enable **YouTube Data API v3**
2. OAuth consent screen → add your Google account as test user
3. Credentials → OAuth client ID → **Desktop app** → download JSON
4. Save as `~/.config/70mai/youtube_credentials.json`
5. First upload opens a browser; token saved to `~/.config/70mai/youtube_token.json`

Large uploads use the resumable protocol via `requests` (64 MB chunks, 600 s timeout). System proxy env vars are ignored to avoid VPN/proxy redirect errors.

**Upload progress:** during transfer, stdout/stderr shows a bar with MB uploaded, speed, and ETA, e.g.  
`Upload trip_02.mp4: [████░░░░] 512.00 MB/2.12 GB (24%) | 3.2 MB/s | ETA 8m 24s`

**Session resume:** `video/Output/.publish_tmp/<stem>.upload.json` (e.g. `trip_01.upload.json`) — deleted on success.

**Diagnostics:** structured JSONL at `video/Output/.publish_tmp/youtube_upload.diag.jsonl` (retries, throughput, error categories). Analyze after failures:

```bash
python3 scripts/analyze_youtube_upload.py
python3 scripts/analyze_youtube_upload.py --last 3 --write-report video/Output/.publish_tmp/youtube_upload.report.md
```

Standalone upload CLI:

```bash
python3 youtube_upload.py video/Output/.publish_tmp/chunk_01/trip_01.mp4 \
  --title "70mai 2026-04-25 — поездка 1" \
  --privacy private \
  --resume-upload
```

After compose finishes, upload a single part from Python:

```bash
python3 -c "
from pathlib import Path
from youtube_upload import upload_video
vid = upload_video(
    Path('video/Output/.publish_tmp/part_01.mp4'),
    title='70mai 2026-04-25 — часть 1/5',
    session_path=Path('video/Output/.publish_tmp/trip_01.upload.json'),
    resume=True,
)
print('https://youtu.be/' + vid)
"
```

State: `video/Output/.publish_tmp/publish_*.state.json`. Temp parts under `.publish_tmp/`.

Long compose runs — background monitor (restarts if stalled 15 min or process dies):

```bash
./scripts/monitor_compose.sh          # chunk 1, check every 60s
MONITOR_CHUNK=1 MONITOR_STALL_SEC=900 ./scripts/monitor_compose.sh
# log: video/Output/.publish_tmp/monitor_chunk1.log
```

## Notes

- Front camera: 3840x2160, Back camera: 1920x1080
- GPS logs (`GPSData*.txt`) scanned with `--scan`; video overlay **disabled** (backlog — [`GOALS.md`](GOALS.md))
- Map tiles 98% opaque (2% transparent); auto GPS offset from linked clip names
- Speed from GPS field 8 (≥34 = km/h, else ÷2) — matches 70mai burn-in
- Full import of all types and cameras needs ~360 GB free disk space
