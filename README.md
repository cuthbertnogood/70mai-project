# 70mai Dashcam Tools

Tools for importing and processing video from a 70mai dashcam SD card.

## Project structure

```
70mai_project/
├── video/          # Imported video (local only, not in git)
└── scripts/        # Import and processing scripts
```

## SD card layout (70mai)

Typical folders on the dashcam SD card:

- `Normal/` — continuous recording
- `Event/` — event-triggered clips
- `Parking/` — parking mode
- `Lapse/` — timelapse
- `GPSData*.txt` — GPS track data

## Usage

> Scripts coming soon: import from SD card and merge short clips into longer segments (~10 min).
