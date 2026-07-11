#!/usr/bin/env python3
"""Generate card session reports (MD + CSV) on SD and in project отчеты/."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from import_70mai import format_duration, scan_clips
from plan_estimate import DEFAULT_SESSION_GAP, build_plan
from publish_all_70mai import (
    DEFAULT_TEMP_DIR,
    DEFAULT_VIDEO_DIR,
    IMPORT_CHUNK_MINUTES,
    aggregate_plan,
    find_sd_card,
    load_merged_publish_state,
)
from publish_70mai import get_trip_state, trip_uploaded
from publish_state import youtube_watch_url

REPORTS_ROOT = _ROOT / "отчеты"
SD_REPORTS_SUBDIR = ".70mai/reports"

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s")
PUBLISH_START = re.compile(
    r"publish_all start source=(\S+) pending=(\d+)"
)
AUTOPILOT_DONE = re.compile(r"Autopilot done")
UPLOADED = re.compile(
    r"Uploaded: https://youtu\.be/(\w+) \(([^,]+),\s*([^)]+)\)"
)
DONE_IN = re.compile(r"Done in ([\dhms ]+): (\d+) merged")


@dataclass
class VideoRow:
    index: int
    record_type: str
    chunk_index: int
    trip_index: int
    start: datetime
    end: datetime
    duration_sec: float
    clip_count: int
    video_id: str | None
    youtube_url: str | None
    uploaded: bool


@dataclass
class PeriodRow:
    record_type: str
    period_start: datetime
    period_end: datetime
    clip_front: int
    clip_back: int
    duration_2cam_sec: float
    youtube_videos: int
    youtube_uploaded: int
    status: str


@dataclass
class ProcessingStats:
    log_first: datetime | None = None
    log_last_done: datetime | None = None
    publish_sessions: int = 0
    import_wall_sec: float = 0.0
    upload_wall_sec: float = 0.0
    upload_events: list[tuple[datetime, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _parse_log_ts(line: str) -> datetime | None:
    m = LOG_TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")


def _parse_duration_token(text: str) -> float:
    """Parse '58m 17s' or '1h 38m 13s' to seconds."""
    text = text.strip()
    if not text:
        return 0.0
    hours = minutes = seconds = 0.0
    for part in text.split():
        if part.endswith("h"):
            hours = float(part[:-1])
        elif part.endswith("m"):
            minutes = float(part[:-1])
        elif part.endswith("s"):
            seconds = float(part[:-1])
    return hours * 3600 + minutes * 60 + seconds


def count_sd_clips(source: Path, record_type: str) -> tuple[int, int]:
    front = len(scan_clips(source, [record_type], ["Front"], warn=False))
    back = len(scan_clips(source, [record_type], ["Back"], warn=False))
    return front, back


def card_slug_from_period(start: datetime, end: datetime, volume: str) -> str:
    vol = re.sub(r"[^A-Za-z0-9_-]+", "_", volume).strip("_") or "sd"
    return f"{start:%Y%m%d}_{end:%Y%m%d}_{vol}"


def collect_video_rows(
    source: Path,
    types: list[str],
    temp_dir: Path,
    state_on_sd: bool,
    ffprobe: str,
) -> tuple[list[VideoRow], dict[str, float], int, int]:
    state = load_merged_publish_state(
        source, types, temp_dir, state_on_sd=state_on_sd, quiet=True
    )
    _trips, chunks, dur_by_type, total, pending = aggregate_plan(
        source,
        types,
        temp_dir,
        state_on_sd=state_on_sd,
        ffprobe=ffprobe,
        chunk_minutes=120,
        session_gap=DEFAULT_SESSION_GAP,
    )
    rows: list[VideoRow] = []
    idx = 0
    for chunk in chunks:
        for trip_idx, trip in enumerate(chunk.trips, start=1):
            idx += 1
            entry = get_trip_state(state, chunk.record_type, chunk.index, trip_idx)
            vid = entry.get("video_id") if entry else None
            url = (entry or {}).get("youtube_url") or youtube_watch_url(vid)
            uploaded = trip_uploaded(state, chunk.record_type, chunk.index, trip_idx)
            rows.append(
                VideoRow(
                    index=idx,
                    record_type=chunk.record_type,
                    chunk_index=chunk.index,
                    trip_index=trip_idx,
                    start=trip.start,
                    end=trip.end,
                    duration_sec=trip.duration_sec,
                    clip_count=getattr(trip, "clip_count", 0),
                    video_id=vid,
                    youtube_url=url,
                    uploaded=uploaded,
                )
            )
    return rows, dur_by_type, total, pending


def collect_period_rows(
    source: Path,
    types_done: list[str],
    types_all: list[str],
    dur_done: dict[str, float],
    video_rows: list[VideoRow],
    ffprobe: str,
) -> list[PeriodRow]:
    rows: list[PeriodRow] = []
    for record_type in types_all:
        front_n, back_n = count_sd_clips(source, record_type)
        if front_n == 0 and back_n == 0:
            continue
        trips, _chunks, dur_map, _t, _p = build_plan(
            source,
            [record_type],
            chunk_minutes=120,
            chunk_mode="trips",
            session_gap=DEFAULT_SESSION_GAP,
            ffprobe=ffprobe,
        )
        if not trips:
            period_start = period_end = datetime.min
            dur_2cam = 0.0
        else:
            period_start = min(t.start for t in trips)
            period_end = max(t.end for t in trips)
            dur_2cam = dur_map.get(record_type, 0.0)
        yt_total = 1 if record_type in ("Event", "Parking") and trips else len(trips)
        uploaded = sum(
            1 for v in video_rows if v.record_type == record_type and v.uploaded
        )
        if record_type in types_done and uploaded == yt_total and yt_total > 0:
            status = "готово (YouTube)"
        elif record_type in types_done:
            status = f"частично ({uploaded}/{yt_total} на YouTube)"
        else:
            status = "не обработано"
        rows.append(
            PeriodRow(
                record_type=record_type,
                period_start=period_start,
                period_end=period_end,
                clip_front=front_n,
                clip_back=back_n,
                duration_2cam_sec=dur_2cam or dur_done.get(record_type, 0.0),
                youtube_videos=yt_total,
                youtube_uploaded=uploaded,
                status=status,
            )
        )
    return rows


def parse_processing_stats(log_path: Path, source: Path) -> ProcessingStats:
    stats = ProcessingStats()
    if not log_path.is_file():
        stats.notes.append(f"Лог не найден: {log_path}")
        return stats
    src = str(source.resolve())
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        ts = _parse_log_ts(line)
        if ts is None:
            continue
        if src not in line and "publish_all start" not in line:
            continue
        if PUBLISH_START.search(line):
            stats.publish_sessions += 1
            if stats.log_first is None:
                stats.log_first = ts
        if AUTOPILOT_DONE.search(line):
            stats.log_last_done = ts
        m = UPLOADED.search(line)
        if m:
            stats.upload_events.append((ts, m.group(1), m.group(3).strip()))
            stats.upload_wall_sec += _parse_duration_token(m.group(3))
        m = DONE_IN.search(line)
        if m:
            stats.import_wall_sec += _parse_duration_token(m.group(1))
    if stats.log_first and stats.log_last_done:
        wall = (stats.log_last_done - stats.log_first).total_seconds()
        stats.notes.append(
            f"Календарное окно по логу: {format_duration(wall)} "
            f"({stats.log_first:%Y-%m-%d %H:%M} → {stats.log_last_done:%Y-%m-%d %H:%M})"
        )
        stats.notes.append(
            "Это не чистое CPU-время: между сессиями были паузы, OAuth, перезапуски watchdog."
        )
    stats.notes.append(
        f"Сессий autopilot (publish_all start): {stats.publish_sessions}"
    )
    stats.notes.append(
        f"Сумма длительностей upload из лога: {format_duration(stats.upload_wall_sec)}"
    )
    stats.notes.append(
        f"Сумма длительностей import (Done in): {format_duration(stats.import_wall_sec)}"
    )
    return stats


def plan_parking_section(source: Path, ffprobe: str) -> list[str]:
    trips, chunks, dur_by_type = build_plan(
        source,
        ["Parking"],
        chunk_minutes=120,
        chunk_mode="trips",
        session_gap=DEFAULT_SESSION_GAP,
        ffprobe=ffprobe,
    )
    if not trips:
        return ["На карте нет клипов Parking или ffprobe недоступен."]
    front_n, back_n = count_sd_clips(source, "Parking")
    dur = dur_by_type.get("Parking", 0.0)
    trip = trips[0]
    est_mb = chunks[0].est_mb if chunks else 0.0
    lines = [
        "## План: Parking (следующий этап)",
        "",
        "Parking обрабатывается **как Event**: все клипы → один 2-cam ролик на YouTube.",
        "",
        f"- **Клипы на SD:** Front {front_n}, Back {back_n} ({front_n + back_n} файлов)",
        f"- **Период записи:** {trip.start:%Y-%m-%d %H:%M} → {trip.end:%Y-%m-%d %H:%M}",
        f"- **2-cam длительность:** {format_duration(dur)}",
        f"- **YouTube:** **1 видео** (~{est_mb:.0f} MB est.)",
        "",
        "### Команда",
        "",
        "```bash",
        "./scripts/publish_all_70mai.sh --types Parking --skip-import",
        "# или полный цикл с merge:",
        "./scripts/publish_all_70mai.sh --types Parking",
        "```",
        "",
        "Import: Front+Back → по **1 merged-файлу** на камеру (как Event).",
        "Compose + upload: один trip `chunk_01/trip_01.mp4`.",
    ]
    return lines


def write_csv(path: Path, rows: list[VideoRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "index",
                "record_type",
                "chunk_index",
                "trip_index",
                "start",
                "end",
                "duration_sec",
                "duration",
                "clip_count",
                "video_id",
                "youtube_url",
                "uploaded",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.index,
                    r.record_type,
                    r.chunk_index,
                    r.trip_index,
                    r.start.isoformat(sep=" "),
                    r.end.isoformat(sep=" "),
                    f"{r.duration_sec:.1f}",
                    format_duration(r.duration_sec),
                    r.clip_count,
                    r.video_id or "",
                    r.youtube_url or "",
                    "yes" if r.uploaded else "no",
                ]
            )


def build_summary_md(
    *,
    source: Path,
    slug: str,
    generated: datetime,
    video_rows: list[VideoRow],
    period_rows: list[PeriodRow],
    processing: ProcessingStats,
    pending: int,
    dur_by_type: dict[str, float],
    out_project: Path,
    out_sd: Path | None,
    parking_lines: list[str],
) -> str:
    footage_start = min(r.start for r in video_rows) if video_rows else None
    footage_end = max(r.end for r in video_rows) if video_rows else None
    uploaded = sum(1 for r in video_rows if r.uploaded)
    total_dur = sum(r.duration_sec for r in video_rows if r.uploaded)

    lines = [
        "# Отчёт по SD-карте 70mai",
        "",
        f"- **Сгенерировано:** {generated:%Y-%m-%d %H:%M:%S}",
        f"- **Карта (mount):** `{source}`",
        f"- **Идентификатор сессии:** `{slug}`",
        f"- **Отчёты (проект):** `{out_project}`",
    ]
    if out_sd:
        lines.append(f"- **Отчёты (SD):** `{out_sd}`")
    lines.extend(
        [
            "",
            "## Сводка",
            "",
            f"| Показатель | Значение |",
            f"|------------|----------|",
            f"| Видео на YouTube | **{uploaded}/{len(video_rows)}** |",
            f"| Pending upload | {pending} |",
            f"| Длительность на YouTube | {format_duration(total_dur)} |",
        ]
    )
    if footage_start and footage_end:
        lines.append(
            f"| Период записи (Normal+Event) | "
            f"{footage_start:%Y-%m-%d %H:%M} → {footage_end:%Y-%m-%d %H:%M} |"
        )
    for rt, d in sorted(dur_by_type.items()):
        lines.append(f"| Длительность {rt} (2-cam) | {format_duration(d)} |")

    lines.extend(["", "## Периоды по типам записи", ""])
    lines.append(
        "| Тип | Начало | Конец | Front | Back | 2-cam | YouTube | Статус |"
    )
    lines.append(
        "|-----|--------|-------|-------|------|-------|---------|--------|"
    )
    for p in period_rows:
        lines.append(
            f"| {p.record_type} | {p.period_start:%Y-%m-%d %H:%M} | "
            f"{p.period_end:%Y-%m-%d %H:%M} | {p.clip_front} | {p.clip_back} | "
            f"{format_duration(p.duration_2cam_sec)} | "
            f"{p.youtube_uploaded}/{p.youtube_videos} | {p.status} |"
        )

    lines.extend(["", "## Время обработки (по логу autopilot)", ""])
    if processing.log_first:
        lines.append(
            f"- **Первая сессия в логе:** {processing.log_first:%Y-%m-%d %H:%M:%S}"
        )
    if processing.log_last_done:
        lines.append(
            f"- **Последний Autopilot done:** {processing.log_last_done:%Y-%m-%d %H:%M:%S}"
        )
    if processing.log_first and processing.log_last_done:
        wall = processing.log_last_done - processing.log_first
        lines.append(f"- **Календарный интервал:** {format_duration(wall.total_seconds())}")
    lines.append(f"- **Сессий publish_all:** {processing.publish_sessions}")
    lines.append(
        f"- **Сумма времени upload (из строк Uploaded):** "
        f"{format_duration(processing.upload_wall_sec)}"
    )
    lines.append(
        f"- **Сумма времени import (Done in):** "
        f"{format_duration(processing.import_wall_sec)}"
    )
    for note in processing.notes:
        lines.append(f"- _{note}_")

    lines.extend(["", "## Все видео на YouTube", ""])
    lines.append(
        "| № | Тип | Начало | Конец | Длит. | Клипов | YouTube | Статус |"
    )
    lines.append(
        "|---|-----|--------|-------|-------|--------|---------|--------|"
    )
    for r in video_rows:
        url = r.youtube_url or "—"
        if r.video_id and not r.youtube_url:
            url = f"https://youtu.be/{r.video_id}"
        st = "✓" if r.uploaded else "—"
        lines.append(
            f"| {r.index} | {r.record_type} | {r.start:%m-%d %H:%M} | "
            f"{r.end:%m-%d %H:%M} | {format_duration(r.duration_sec)} | "
            f"{r.clip_count} | [{r.video_id or '—'}]({url}) | {st} |"
        )

    lines.extend(["", *parking_lines])
    lines.extend(
        [
            "",
            "## Файлы в этой папке",
            "",
            "- `SUMMARY.md` — этот отчёт",
            "- `VIDEOS.csv` — таблица видео",
            "- `PERIOD.md` — только периоды",
            "- `PROCESSING.md` — только время обработки",
            "- `PLAN_PARKING.md` — план Parking",
            "- `CARD_INFO.json` — метаданные карты",
        ]
    )
    return "\n".join(lines) + "\n"


def write_period_md(path: Path, rows: list[PeriodRow]) -> None:
    lines = [
        "# Периоды записи на карте",
        "",
        "| Тип | Начало | Конец | Front | Back | 2-cam | YouTube | Статус |",
        "|-----|--------|-------|-------|------|-------|---------|--------|",
    ]
    for p in rows:
        lines.append(
            f"| {p.record_type} | {p.period_start:%Y-%m-%d %H:%M} | "
            f"{p.period_end:%Y-%m-%d %H:%M} | {p.clip_front} | {p.clip_back} | "
            f"{format_duration(p.duration_2cam_sec)} | "
            f"{p.youtube_uploaded}/{p.youtube_videos} | {p.status} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_processing_md(path: Path, stats: ProcessingStats) -> None:
    lines = ["# Время обработки", ""]
    if stats.log_first:
        lines.append(f"- Первая сессия: **{stats.log_first:%Y-%m-%d %H:%M:%S}**")
    if stats.log_last_done:
        lines.append(f"- Последний успешный финиш: **{stats.log_last_done:%Y-%m-%d %H:%M:%S}**")
    if stats.log_first and stats.log_last_done:
        wall = stats.log_last_done - stats.log_first
        lines.append(f"- Календарный интервал: **{format_duration(wall.total_seconds())}**")
    lines.append(f"- Сессий publish_all: **{stats.publish_sessions}**")
    lines.append(f"- Сумма upload: **{format_duration(stats.upload_wall_sec)}**")
    lines.append(f"- Сумма import: **{format_duration(stats.import_wall_sec)}**")
    lines.append("")
    for n in stats.notes:
        lines.append(f"- {n}")
    if stats.upload_events:
        lines.extend(["", "## Upload-события", ""])
        for ts, vid, elapsed in stats.upload_events:
            lines.append(f"- {ts:%Y-%m-%d %H:%M} — `{vid}` ({elapsed})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_all_outputs(
    out_dir: Path,
    *,
    summary_md: str,
    period_rows: list[PeriodRow],
    video_rows: list[VideoRow],
    processing: ProcessingStats,
    parking_lines: list[str],
    card_info: dict,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "SUMMARY.md").write_text(summary_md, encoding="utf-8")
    write_csv(out_dir / "VIDEOS.csv", video_rows)
    write_period_md(out_dir / "PERIOD.md", period_rows)
    write_processing_md(out_dir / "PROCESSING.md", processing)
    (out_dir / "PLAN_PARKING.md").write_text(
        "\n".join(parking_lines) + "\n", encoding="utf-8"
    )
    (out_dir / "CARD_INFO.json").write_text(
        json.dumps(card_info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate MD/CSV reports for a 70mai SD card session "
            "(project отчеты/ + SD .70mai/reports/). Does not run import/upload."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="SD mount (auto-detect if omitted)",
    )
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--temp-dir", type=Path, default=DEFAULT_TEMP_DIR)
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_TEMP_DIR / "publish_all.log",
        help="Autopilot log for processing-time stats",
    )
    parser.add_argument(
        "--types-done",
        nargs="+",
        default=["Normal", "Event"],
        metavar="TYPE",
        help="Types already processed in this session (default: Normal Event)",
    )
    parser.add_argument(
        "--types-plan",
        nargs="+",
        default=["Normal", "Event", "Parking"],
        metavar="TYPE",
        help="Types to show in period table (default: Normal Event Parking)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_ROOT,
        help=f"Project reports root (default: {REPORTS_ROOT})",
    )
    parser.add_argument(
        "--no-sd",
        action="store_true",
        help="Do not write reports to SD card",
    )
    parser.add_argument(
        "--ffprobe",
        default="ffprobe",
        help="ffprobe binary for duration probes",
    )
    args = parser.parse_args(argv)

    from project_env import ensure_venv_python

    ensure_venv_python()

    source = args.source
    if source is None:
        source = find_sd_card()
        if source is None:
            print("SD card not found. Use --source /Volumes/Untitled", file=sys.stderr)
            return 1
    source = source.resolve()

    video_rows, dur_by_type, _total, pending = collect_video_rows(
        source,
        args.types_done,
        args.temp_dir,
        state_on_sd=True,
        ffprobe=args.ffprobe,
    )
    if not video_rows:
        print("No trips found for types:", args.types_done, file=sys.stderr)
        return 1

    period_rows = collect_period_rows(
        source,
        args.types_done,
        args.types_plan,
        dur_by_type,
        video_rows,
        args.ffprobe,
    )
    processing = parse_processing_stats(args.log, source)
    parking_lines = plan_parking_section(source, args.ffprobe)

    footage_start = min(r.start for r in video_rows)
    footage_end = max(r.end for r in video_rows)
    slug = card_slug_from_period(footage_start, footage_end, source.name)
    generated = datetime.now()

    out_project = args.reports_dir / slug
    out_sd = None if args.no_sd else source / SD_REPORTS_SUBDIR / slug

    card_info = {
        "generated_at": generated.isoformat(timespec="seconds"),
        "source": str(source),
        "volume_name": source.name,
        "slug": slug,
        "types_done": args.types_done,
        "types_plan": args.types_plan,
        "footage_period": {
            "start": footage_start.isoformat(sep=" "),
            "end": footage_end.isoformat(sep=" "),
        },
        "youtube_uploaded": sum(1 for r in video_rows if r.uploaded),
        "youtube_total": len(video_rows),
        "pending_upload": pending,
        "reports_project": str(out_project.resolve()),
        "reports_sd": str(out_sd.resolve()) if out_sd else None,
    }

    summary_md = build_summary_md(
        source=source,
        slug=slug,
        generated=generated,
        video_rows=video_rows,
        period_rows=period_rows,
        processing=processing,
        pending=pending,
        dur_by_type=dur_by_type,
        out_project=out_project,
        out_sd=out_sd,
        parking_lines=parking_lines,
    )

    write_all_outputs(
        out_project,
        summary_md=summary_md,
        period_rows=period_rows,
        video_rows=video_rows,
        processing=processing,
        parking_lines=parking_lines,
        card_info=card_info,
    )
    if out_sd is not None:
        write_all_outputs(
            out_sd,
            summary_md=summary_md,
            period_rows=period_rows,
            video_rows=video_rows,
            processing=processing,
            parking_lines=parking_lines,
            card_info=card_info,
        )

    index_path = args.reports_dir / "README.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "\n".join(
            [
                "# Отчёты autopilot",
                "",
                f"Последний отчёт: **{slug}** ({generated:%Y-%m-%d %H:%M})",
                f"- Карта: `{source}`",
                f"- Папка: [`{slug}/`]({slug}/SUMMARY.md)",
                "",
                "Сгенерировать заново:",
                "",
                "```bash",
                "./scripts/generate_card_reports.sh",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"Reports written: {out_project}")
    if out_sd:
        print(f"Reports on SD:   {out_sd}")
    print(f"Open: {out_project / 'SUMMARY.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
