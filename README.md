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

## Notes

- Front camera: 3840x2160, Back camera: 1920x1080
- GPS data stays in `GPSData*.txt` on the SD card and is not merged
- Full import of all types and cameras needs ~360 GB free disk space
