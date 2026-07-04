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

Scan the SD card to see which date/time ranges contain data (fast, no ffmpeg):

```bash
python3 import_70mai.py --scan
```

Example output:

```
Scanning /Volumes/Untitled
Session gap: 120 sec (pauses longer than this start a new range)

=== Overall ===
  1906 clips | 2026-04-25 13:01:19 -> 2026-04-28 14:48:28
  calendar days: 2026-04-25 .. 2026-04-28

=== By type / camera ===

Normal / Front — 520 clips, 2026-04-25 13:01:19 -> 2026-04-28 12:00:00
  3 recording session(s):
    1. 2026-04-25 13:01:19 -> 2026-04-25 18:30:00 (156 clips)
    2. 2026-04-26 08:00:00 -> 2026-04-26 22:15:00 (200 clips)
    3. 2026-04-27 07:45:00 -> 2026-04-28 12:00:00 (164 clips)

=== By date ===
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
  --output ./video \
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
| `--scan` | off | Scan SD card and show available date/time ranges |
| `--date DATE` | — | Export day (see above) |
| `--from-time HH:MM` | `00:00` | Start time on `--date` |
| `--to-time HH:MM` | `23:59:59` | End time on `--date` (exclusive) |
| `--from DATETIME` | — | Range start (inclusive) |
| `--to DATETIME` | — | Range end (exclusive) |

Run `python3 import_70mai.py --help` for the built-in reference.

## Output

Merged files are written to:

```
video/
├── Normal/
│   ├── Front/NO_20260425-130119_131019_F.mp4
│   └── Back/
├── Event/
└── Parking/
```

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

## Notes

- Front camera: 3840x2160, Back camera: 1920x1080
- GPS data stays in `GPSData*.txt` on the SD card and is not merged
- Full import of all types and cameras needs ~360 GB free disk space
