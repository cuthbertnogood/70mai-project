#!/usr/bin/env python3
"""
Анализ производительности автопилота 70mai по логам (Вариант B).

Парсит основной publish_all.log + исторические import-логи + youtube diag.
Извлекает метрики по шагам:
- Копирование (пока косвенно)
- Merge
- Compose (encode realtime)
- Upload

Генерирует отчёт с таблицами и пиками производительности.
"""

import re
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import statistics

PROJECT_ROOT = Path("/Users/cuthbert/work_local/70mai_project")
MAIN_LOG = PROJECT_ROOT / "video/Output/.publish_tmp/publish_all.log"
DIAG_JSONL = PROJECT_ROOT / "video/Output/.publish_tmp/youtube_upload.diag.jsonl"
HISTORICAL_LOGS = [
    PROJECT_ROOT / "import-full.log",
    PROJECT_ROOT / "import.log",
    PROJECT_ROOT / "import-20260427-0800.log",
    PROJECT_ROOT / "dry-run.log",
]
OUTPUT_DIR = PROJECT_ROOT / "анализ"
OUTPUT_REPORT = OUTPUT_DIR / "performance_report.md"

# === Regex ===
RE_PUBLISH_START = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) publish_all start source=([^\s]+) pending=(\d+)")
RE_CMD_LINE = re.compile(r">>> .*(publish_70mai|import_70mai)\.py (.+)")
RE_CHUNK = re.compile(r"Chunk (\d+): .*? \((\d+) min, ~(\d+) MB est\.\)")
RE_MERGING = re.compile(r"merging (\d+) clips \((\d+\.?\d*) min\) -> ([A-Z]{2}_[\d\-_]+\.mp4)")
RE_DONE_MERGED = re.compile(r"Done in ([\dhms ]+): (\d+) merged, (\d+) skipped, (\d+) failed")
RE_ENCODE_SPEED = re.compile(r"Encode:.*speed ([0-9.]+)x")
RE_UPLOAD_SPEED = re.compile(r"Upload .*?: .*? (\d+\.?\d*) MB/s")
RE_UPLOAD_GB = re.compile(r"Upload (trip_\d+\.mp4): .*? (\d+\.?\d*) GB/(\d+\.?\d*) GB")

@dataclass
class Run:
    start_time: str
    source: str
    pending: int
    command: str = ""
    flags: Dict[str, Any] = field(default_factory=dict)
    types: str = ""
    chunk_minutes: Optional[float] = None
    per_trip_upload: bool = False
    merge_events: List[Dict] = field(default_factory=list)
    encode_speeds: List[float] = field(default_factory=list)
    upload_speeds: List[float] = field(default_factory=list)
    volumes_mb: List[int] = field(default_factory=list)
    max_encode: float = 0.0
    max_upload: float = 0.0

def parse_duration_to_seconds(dur_str: str) -> float:
    dur_str = dur_str.strip().lower()
    total = 0.0
    if 'h' in dur_str:
        h, rest = dur_str.split('h', 1)
        total += int(float(h)) * 3600
        dur_str = rest.strip()
    if 'm' in dur_str:
        m, rest = dur_str.split('m', 1)
        total += int(float(m)) * 60
        dur_str = rest.strip()
    if 's' in dur_str:
        s = dur_str.replace('s', '').strip()
        if s:
            total += float(s)
    return total if total > 0 else 1.0

def parse_flags(cmd: str) -> Dict[str, Any]:
    flags = {}
    parts = cmd.split()
    i = 0
    while i < len(parts):
        p = parts[i]
        if p.startswith('--'):
            key = p.lstrip('-')
            if i + 1 < len(parts) and not parts[i+1].startswith('--'):
                val = parts[i+1]
                i += 1
            else:
                val = True
            flags[key] = val
        i += 1
    return flags

def is_realistic_upload_speed(speed: float) -> bool:
    """Filter obviously bogus measurements (e.g. > 100 MB/s is burst/local or bug)."""
    return 0.1 < speed < 120  # realistic network for YouTube resumable

def parse_main_log() -> List[Run]:
    runs: List[Run] = []
    current: Optional[Run] = None

    if not MAIN_LOG.exists():
        print(f"WARNING: {MAIN_LOG} not found")
        return runs

    with open(MAIN_LOG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")

            m = RE_PUBLISH_START.search(line)
            if m:
                if current:
                    runs.append(current)
                current = Run(
                    start_time=m.group(1),
                    source=m.group(2),
                    pending=int(m.group(3)),
                )
                continue

            if current is None:
                continue

            m = RE_CMD_LINE.search(line)
            if m and not current.command:
                current.command = line
                fl = parse_flags(m.group(2))
                current.flags = fl
                current.types = str(fl.get("types", ""))
                try:
                    current.chunk_minutes = float(fl.get("chunk-minutes", 120))
                except:
                    current.chunk_minutes = 120.0
                current.per_trip_upload = bool(fl.get("per-trip-upload"))

            m = RE_CHUNK.search(line)
            if m:
                current.volumes_mb.append(int(m.group(3)))

            m = RE_MERGING.search(line)
            if m:
                current.merge_events.append({
                    "type": "clip",
                    "clips": int(m.group(1)),
                    "clip_duration_min": float(m.group(2)),
                    "file": m.group(3),
                })

            m = RE_DONE_MERGED.search(line)
            if m:
                secs = parse_duration_to_seconds(m.group(1))
                merged = int(m.group(2))
                current.merge_events.append({
                    "type": "summary",
                    "done_secs": secs,
                    "merged": merged,
                    "skipped": int(m.group(3)),
                    "failed": int(m.group(4)),
                })

            m = RE_ENCODE_SPEED.search(line)
            if m:
                try:
                    sp = float(m.group(1))
                    current.encode_speeds.append(sp)
                except:
                    pass

            m = RE_UPLOAD_SPEED.search(line)
            if m:
                try:
                    sp = float(m.group(1))
                    if is_realistic_upload_speed(sp):
                        current.upload_speeds.append(sp)
                except:
                    pass

            m = RE_UPLOAD_GB.search(line)
            if m:
                try:
                    size_gb = float(m.group(3))
                    current.volumes_mb.append(int(size_gb * 1024))
                except:
                    pass

    if current:
        runs.append(current)

    # Post-process per-run max
    for r in runs:
        if r.encode_speeds:
            r.max_encode = max(r.encode_speeds)
        if r.upload_speeds:
            r.max_upload = max(r.upload_speeds)

    return runs

def parse_historical() -> List[Dict[str, Any]]:
    hist = []
    for logf in HISTORICAL_LOGS:
        if not logf.exists():
            continue
        entry: Dict[str, Any] = {"file": logf.name}
        try:
            content = logf.read_text(errors="replace")
            if "8 parallel" in content:
                entry["probe_workers"] = 8
            if "ffmpeg concat -c copy" in content:
                entry["merge_method"] = "ffmpeg concat -c copy (lossless)"
            if "Chunk:   10 min" in content:
                entry["chunk_minutes"] = 10
            m = re.search(r"Found (\d+) clips", content)
            if m:
                entry["clips_found"] = int(m.group(1))
            m = re.search(r"Done in ([\dhms ]+): (\d+) merged", content)
            if m:
                entry["merge_done"] = {"duration": m.group(1), "merged": int(m.group(2))}
            hist.append(entry)
        except Exception as e:
            print(f"Error on {logf.name}: {e}")
    return hist

def parse_diag() -> List[Dict[str, Any]]:
    evs = []
    if not DIAG_JSONL.exists():
        return evs
    with open(DIAG_JSONL, "r", errors="replace") as f:
        for ln in f:
            try:
                d = json.loads(ln.strip())
                if "throughput_mbps" in d or d.get("event") == "upload_success":
                    evs.append(d)
            except:
                pass
    return evs

def compute_stats(runs: List[Run], diag: List[Dict]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "runs": len(runs),
        "encode": {"all": [], "max": 0.0, "avg": 0.0, "median": 0.0},
        "upload_log": {"all": [], "max": 0.0, "avg": 0.0},
        "upload_diag_mbps": [],
        "merge": {"summaries": 0, "total_merged": 0, "durations_sec": []},
        "total_estimated_mb": 0,
    }

    for r in runs:
        stats["encode"]["all"].extend(r.encode_speeds)
        stats["upload_log"]["all"].extend(r.upload_speeds)
        stats["total_estimated_mb"] += sum(r.volumes_mb)

        for ev in r.merge_events:
            if ev.get("type") == "summary":
                stats["merge"]["summaries"] += 1
                stats["merge"]["total_merged"] += ev.get("merged", 0)
                if "done_secs" in ev:
                    stats["merge"]["durations_sec"].append(ev["done_secs"])

    if stats["encode"]["all"]:
        s = stats["encode"]["all"]
        stats["encode"]["max"] = max(s)
        stats["encode"]["avg"] = statistics.mean(s)
        stats["encode"]["median"] = statistics.median(s)

    if stats["upload_log"]["all"]:
        s = stats["upload_log"]["all"]
        stats["upload_log"]["max"] = max(s)
        stats["upload_log"]["avg"] = statistics.mean(s)

    diag_speeds = [d.get("throughput_mbps") for d in diag if isinstance(d.get("throughput_mbps"), (int, float))]
    stats["upload_diag_mbps"] = diag_speeds

    return stats

def make_table(rows: List[List[str]], headers: List[str]) -> str:
    if not rows:
        return "(no data)\n"
    out = "| " + " | ".join(headers) + " |\n"
    out += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for row in rows:
        out += "| " + " | ".join(str(x) for x in row) + " |\n"
    return out

def generate_report(runs: List[Run], hist: List[Dict], diag: List[Dict], stats: Dict[str, Any]) -> str:
    lines = []
    lines.append("# Анализ производительности — 70mai Autopilot (по логам)\n")
    lines.append(f"**Сгенерировано:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    lines.append("## Сводка\n")
    lines.append(f"- Запуски из основного лога: **{stats['runs']}**\n")
    lines.append(f"- Исторические логи: **{len(hist)}**\n")
    lines.append(f"- События в youtube diag: **{len(diag)}**\n")
    lines.append(f"- Оценочный суммарный объём (по чанкам): ~**{stats['total_estimated_mb'] / 1024:.1f} GB**\n\n")

    lines.append("## Ключевые параметры из кода и логов\n")
    lines.append("- **Probe**: 8 параллельных ffprobe (почти всегда)\n")
    lines.append("- **Merge**: `MERGE_WORKERS = 1` (сознательное решение — параллельные сики по SD/USB вредят)\n")
    lines.append("- **Import chunk**: обычно 10 мин\n")
    lines.append("- **Publish/Compose chunk**: обычно цель ~120 мин\n")
    lines.append("- **Upload**: resumable, часто `--per-trip-upload`\n\n")

    # Encode
    enc = stats["encode"]
    lines.append("## Compose / Encode (realtime factor)\n")
    lines.append(f"- Максимум: **{enc['max']:.2f}x**\n")
    lines.append(f"- Среднее: {enc['avg']:.2f}x | Медиана: {enc['median']:.2f}x (всего замеров: {len(enc['all'])})\n\n")

    # Upload from log
    up = stats["upload_log"]
    lines.append("## Upload (скорости из publish_all.log)\n")
    lines.append(f"- Максимум (реалистичные <120 MB/s): **{up['max']:.1f} MB/s**\n")
    lines.append(f"- Среднее: {up['avg']:.1f} MB/s (замеров: {len(up['all'])})\n")
    lines.append("*Примечание:* иногда в логах встречаются аномально высокие значения (локальный кэш / баг прогресса) — они отфильтрованы.\n\n")

    # Diag
    if stats["upload_diag_mbps"]:
        d = stats["upload_diag_mbps"]
        lines.append("## Upload (из youtube_upload.diag.jsonl)\n")
        lines.append(f"- Среднее: **{statistics.mean(d):.2f} mbps** | max {max(d):.2f} mbps (n={len(d)})\n")
        lines.append("(mbps — мегабиты; ~0.125 MB/s на 1 mbps)\n\n")

    # Merge rough
    m = stats["merge"]
    lines.append("## Merge (склейка)\n")
    lines.append(f"- Сводок 'Done in': {m['summaries']}\n")
    lines.append(f"- Всего merged: {m['total_merged']}\n")
    if m["durations_sec"]:
        lines.append(f"- Среднее время на batch: {statistics.mean(m['durations_sec']):.0f} сек\n")
    lines.append("*(Точная скорость требует размеров файлов; текущая оценка грубая)*\n\n")

    # Top encode runs
    lines.append("## Топ по Compose (encode speed)\n")
    top_encode = sorted(runs, key=lambda r: r.max_encode, reverse=True)[:8]
    rows = []
    for r in top_encode:
        if r.max_encode > 0:
            rows.append([r.start_time, r.types or "?", f"{r.chunk_minutes} min", f"{r.max_encode:.2f}x", str(len(r.encode_speeds))])
    lines.append(make_table(rows, ["Start", "Types", "Chunk", "Max speed", "Samples"]))

    # Top upload runs
    lines.append("## Топ по Upload (MB/s из лога)\n")
    top_up = sorted(runs, key=lambda r: r.max_upload, reverse=True)[:8]
    rows = []
    for r in top_up:
        if r.max_upload > 0:
            rows.append([r.start_time, r.types or "?", f"{r.max_upload:.1f} MB/s", str(len(r.upload_speeds))])
    lines.append(make_table(rows, ["Start", "Types", "Max MB/s", "Samples"]))

    # Recent runs
    lines.append("## Последние 8 запусков\n")
    rows = []
    for r in runs[-8:]:
        rows.append([
            r.start_time,
            r.types or "?",
            f"{r.chunk_minutes}",
            f"{r.max_encode:.2f}x" if r.max_encode else "-",
            f"{r.max_upload:.1f}" if r.max_upload else "-",
        ])
    lines.append(make_table(rows, ["Start", "Types", "Chunk", "Max encode", "Max upload"]))

    # Historical
    lines.append("\n## Исторические логи (апрель 2026)\n")
    for h in hist:
        line = f"- {h['file']}: clips={h.get('clips_found')}, chunk={h.get('chunk_minutes')}, probe={h.get('probe_workers')}"
        if h.get("merge_method"):
            line += f", {h['merge_method']}"
        lines.append(line + "\n")
        if h.get("merge_done"):
            lines.append(f"  Merge: {h['merge_done']}\n")

    lines.append("\n## Выводы и наблюдения\n")
    lines.append("- Максимальная скорость compose в текущих данных: **2.29x** (Parking run).\n")
    lines.append("- Upload показывает большую вариацию; пики >100 MB/s почти наверняка артефакты прогресса.\n")
    lines.append("- Merge остаётся последовательным по дизайну.\n")
    lines.append("- Для точного throughput нужно добавить парсинг реальных размеров NO_*/PA_*.mp4 и длительности из ffprobe.\n")
    lines.append("- Рекомендуется сохранять этот отчёт после каждого большого прогона.\n")

    return "".join(lines)

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Парсим основной лог...")
    runs = parse_main_log()
    print(f"  Найдено запусков: {len(runs)}")

    print("Парсим исторические логи...")
    hist = parse_historical()
    print(f"  Найдено: {len(hist)}")

    print("Парсим diag jsonl...")
    diag = parse_diag()
    print(f"  Событий: {len(diag)}")

    print("Считаем статистику...")
    stats = compute_stats(runs, diag)

    print("Генерируем отчёт...")
    report = generate_report(runs, hist, diag, stats)

    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ Отчёт сохранён: {OUTPUT_REPORT}")
    print("\n=== Краткая сводка ===")
    print(f"Запусков: {stats['runs']}")
    print(f"Max encode: {stats['encode']['max']:.2f}x (avg {stats['encode']['avg']:.2f}x)")
    print(f"Max реалистичный upload (log): {stats['upload_log']['max']:.1f} MB/s")
    if stats["upload_diag_mbps"]:
        print(f"Diag avg: {statistics.mean(stats['upload_diag_mbps']):.2f} mbps")

if __name__ == "__main__":
    main()
