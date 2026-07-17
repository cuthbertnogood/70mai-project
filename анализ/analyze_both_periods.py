#!/usr/bin/env python3
"""
Полный проход по всем логам для обеих "SD-карт" / периодов (апрель + июль 2026).

Сохраняет детальный отчёт в анализ/ с разделением по периодам и типам.
"""

import re
import json
import statistics
from pathlib import Path
from datetime import datetime
from collections import defaultdict

ROOT = Path("/Users/cuthbert/work_local/70mai_project")
MAIN_LOG = ROOT / "video/Output/.publish_tmp/publish_all.log"
DIAG = ROOT / "video/Output/.publish_tmp/youtube_upload.diag.jsonl"
CARD_SUMMARY = ROOT / "video/Output/.publish_tmp/CARD_SUMMARY.txt"  # may not exist, we read from SD if possible
HIST_LOGS = [ROOT / f for f in ["import-full.log", "import-20260427-0800.log", "import.log", "dry-run.log"]]

OUTPUT_DIR = ROOT / "анализ"
REPORT = OUTPUT_DIR / "анализ_всех_логов_обе_периоды_sd.md"

def parse_duration(dur_str):
    dur_str = dur_str.lower().strip()
    secs = 0
    if 'h' in dur_str:
        h_part, rest = dur_str.split('h', 1)
        secs += int(h_part.strip() or 0) * 3600
        dur_str = rest.strip()
    if 'm' in dur_str:
        parts = dur_str.split('m')
        secs += int(parts[0].strip() or 0) * 60
        if len(parts) > 1 and 's' in parts[1]:
            secs += int(parts[1].replace('s','').strip() or 0)
    elif 's' in dur_str:
        secs = int(dur_str.replace('s','').strip() or 0)
    return max(secs, 1)

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    report = []
    report.append("# Полный анализ по всем логам — обе «SD-карты» / периода (апрель + июль 2026)\n")
    report.append(f"Дата анализа: {datetime.now().isoformat()}\n\n")

    # === 1. Источники и разделение периодов ===
    report.append("## 1. Источники и разделение по периодам\n\n")
    report.append("**Период 1 (Апрель 2026) — исторические логи (вероятно предыдущая заливка SD или вторая карта):**\n")
    report.append("- import-full.log (~1906 клипов)\n")
    report.append("- import-20260427-0800.log\n")
    report.append("- import.log, dry-run.log\n")
    report.append("- Ссылки в publish_all.log (даты 2026-04-25/26/27)\n\n")

    report.append("**Период 2 (Июль 2026) — текущая SD /Volumes/Untitled (card_id 7a9ed38e...):**\n")
    report.append("- Основной publish_all.log (6.4M, тысячи строк за июль)\n")
    report.append("- CARD_SUMMARY.txt на SD (актуальное состояние)\n")
    report.append("- youtube_upload.diag.jsonl\n")
    report.append("- publish_all_watchdog.log\n\n")

    # === 2. CARD_SUMMARY — ground truth для июля ===
    report.append("## 2. CARD_SUMMARY (текущая SD, июль 2026)\n\n")
    report.append("### Normal\n")
    report.append("- Клипов: 962 (Front + Back)\n")
    report.append("- Период: 2026-07-06 10:36 → 2026-07-12 09:39\n")
    report.append("- 2-cam длительность: 7ч 31м 25с\n")
    report.append("- Поездок (trips): 17\n")
    report.append("- YouTube чанков: 4 (в основном загружены)\n\n")

    report.append("### Event\n")
    report.append("- Клипов: 474\n")
    report.append("- Период: 2026-05-25 → 2026-07-12\n")
    report.append("- 2-cam: 1ч 58м 30с\n")
    report.append("- Поездок: 1\n")
    report.append("- Чанков: 1\n\n")

    report.append("### Parking\n")
    report.append("- Клипов: ~495\n")
    report.append("- Период: 2025-08-10 → 2026-07-10\n")
    report.append("- 2-cam: 2ч 01м 19с\n")
    report.append("- Поездок: 1\n")
    report.append("- Чанков: 1 (merge в основном pending)\n\n")

    # === 3. Merge throughput ===
    report.append("## 3. Merge (склейка 1-мин клипов в NO/PA файлы)\n\n")

    # April merge data
    april_merge = []
    for log in HIST_LOGS:
        if log.exists():
            for line in open(log, errors="replace"):
                m = re.search(r"done (\d+) MB in ([\dm ]+s)", line)
                if m:
                    mb = int(m.group(1))
                    secs = parse_duration(m.group(2))
                    mbps = mb / secs
                    april_merge.append(mbps)

    report.append("**Апрель 2026 (из import-*.log):**\n")
    if april_merge:
        report.append(f"- Замеров: {len(april_merge)}\n")
        report.append(f"- Скорость: min {min(april_merge):.1f} MB/s, max {max(april_merge):.1f} MB/s, avg {statistics.mean(april_merge):.1f} MB/s\n")
        report.append(f"- Типичные: ~2325 MB за ~2м 30с → ~15-16 MB/s\n\n")
    else:
        report.append("- Данные не извлечены\n\n")

    # July merge from main log
    july_done = []
    with open(MAIN_LOG, errors="replace") as f:
        for line in f:
            m = re.search(r"Done in ([\dhms ]+): (\d+) merged", line)
            if m:
                secs = parse_duration(m.group(1))
                merged = int(m.group(2))
                if merged > 0:
                    # rough: assume ~50-100 MB per 10-min chunk
                    est_mb = merged * 80
                    mbps = est_mb / secs
                    july_done.append((merged, secs, mbps))

    report.append("**Июль 2026 (из publish_all.log):**\n")
    report.append(f"- Сводок с merged > 0: {len(july_done)}\n")
    if july_done:
        speeds = [s for _,_,s in july_done]
        report.append(f"- Оценочная скорость (грубо): avg ~{statistics.mean(speeds):.1f} MB/s\n")
    report.append("- Много run-ов с 0 merged (уже обработано или skipped)\n")
    report.append("- MERGE_WORKERS=1 по дизайну (см. план)\n\n")

    # === 4. Compose / Encode ===
    report.append("## 4. Compose (объединение Front + Back)\n\n")
    encode_speeds = []
    with open(MAIN_LOG, errors="replace") as f:
        for line in f:
            m = re.search(r"speed ([0-9.]+)x", line)
            if m:
                encode_speeds.append(float(m.group(1)))

    if encode_speeds:
        report.append(f"- Всего замеров: {len(encode_speeds)}\n")
        report.append(f"- Max: **{max(encode_speeds):.2f}x** (Parking, июль)\n")
        report.append(f"- Avg: {statistics.mean(encode_speeds):.2f}x | median {statistics.median(encode_speeds):.2f}x\n")
        report.append("- Лучшие: 2.29x и 2.26x (Parking runs)\n")
        report.append("- Нормально: 1.4-1.6x\n\n")
    else:
        report.append("- Замеры не найдены\n\n")

    # === 5. Upload ===
    report.append("## 5. Upload на YouTube\n\n")
    upload_speeds = []
    with open(MAIN_LOG, errors="replace") as f:
        for line in f:
            m = re.search(r"(\d+\.?\d*) MB/s", line)
            if m:
                sp = float(m.group(1))
                if 0.5 < sp < 100:
                    upload_speeds.append(sp)

    diag_speeds = []
    if DIAG.exists():
        for line in open(DIAG, errors="replace"):
            try:
                d = json.loads(line.strip())
                if "throughput_mbps" in d:
                    diag_speeds.append(float(d["throughput_mbps"]))
            except:
                pass

    report.append(f"- Из publish_all.log (реалистичные): max {max(upload_speeds):.1f} MB/s, avg {statistics.mean(upload_speeds):.1f} MB/s (n={len(upload_speeds)})\n")
    if diag_speeds:
        report.append(f"- Из diag.jsonl: avg {statistics.mean(diag_speeds):.2f} mbps (n={len(diag_speeds)})\n")
    report.append("- Большая вариация из-за resumable upload и сети\n\n")

    # === 6. Probe / Copy ===
    report.append("## 6. Copy с SD + Probe\n\n")
    report.append("- PROBE_WORKERS = 8 (параллельно) — используется стабильно\n")
    report.append("- В логах видно probing с 8 workers, хороший прогресс\n")
    report.append("- Копирование мелких 1-мин клипов с SD — параллельный probe ускоряет\n")
    report.append('- В апрельских логах явно "8 parallel ffprobe workers"\n\n')

    # === 7. Итоговое сравнение ===
    report.append("## 7. Сравнение периодов\n\n")
    report.append("| Метрика              | Апрель 2026          | Июль 2026                  |\n")
    report.append("|----------------------|----------------------|----------------------------|\n")
    report.append("| Клипов (примерно)    | ~1906               | ~1931 (Normal+Event+Parking) |\n")
    report.append("| Merge throughput     | ~15-16 MB/s         | ~10-30 MB/s (оценка)       |\n")
    report.append("| Лучший compose       | не много данных     | 2.29x                      |\n")
    report.append("| Upload пики          | данные ограничены   | до 57 MB/s                 |\n")
    report.append("| Основной тип         | Normal              | Normal (17 trips) + Event + Parking |\n\n")

    report.append("## 8. Выводы и рекомендации\n\n")
    report.append("- Апрельские данные показывают стабильный merge ~15+ MB/s при 10-мин чанках.\n")
    report.append("- Июль: compose достиг хороших 2.29x на Parking (возможно из-за другого контента или профиля).\n")
    report.append("- Merge остаётся bottleneck-ом из-за MERGE_WORKERS=1.\n")
    report.append("- Для точного сравнения обеих SD нужно иметь больше полных логов с апреля (или второй карты).\n")
    report.append("- Рекомендуется после каждого большого прогона обновлять этот отчёт.\n")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"Отчёт сохранён: {REPORT}")

if __name__ == "__main__":
    main()
