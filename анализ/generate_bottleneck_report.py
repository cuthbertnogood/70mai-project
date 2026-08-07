#!/usr/bin/env python3
"""Build bottleneck analysis from perf_monitor.jsonl + bench summaries.

Usage:
  python3 анализ/generate_bottleneck_report.py
  python3 анализ/generate_bottleneck_report.py --watch  # refresh every 60s
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP = PROJECT_ROOT / "video" / "Output" / ".publish_tmp"
MONITOR_JSONL = TEMP / "perf_monitor.jsonl"
BENCH_BALANCED = TEMP / "bench_1h" / "summary.md"
BENCH_BALANCED_TIMING = TEMP / "bench_1h" / "timing.jsonl"
BENCH_HEVC = TEMP / "bench_1h_hevc" / "summary.md"
BENCH_HEVC_TIMING = TEMP / "bench_1h_hevc" / "timing.jsonl"
OUT = PROJECT_ROOT / "анализ" / "bottleneck_report.md"


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_timing(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in load_jsonl(path):
        ph = row.get("phase")
        if ph and ph not in out:
            out[ph] = row
    return out


def metric_stats(rows: list[dict], key: str) -> dict | None:
    vals: list[float] = []
    for row in rows:
        m = (row.get("metrics") or {}).get(key)
        if m and m.get("avg") is not None:
            vals.append(float(m["avg"]))
    if not vals:
        return None
    return {
        "n": len(vals),
        "avg": round(statistics.mean(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "last": round(vals[-1], 2),
    }


def phase_timeline(rows: list[dict]) -> list[str]:
    lines = []
    seen: set[tuple] = set()
    for row in rows:
        ph = row.get("phase")
        detail = row.get("detail") or ""
        pct = row.get("percent")
        key = (ph, detail)
        if key in seen:
            continue
        seen.add(key)
        ts = row.get("ts", "")[:19]
        lines.append(f"- `{ts}` **{ph}** {detail}" + (f" ({pct:.0f}%)" if pct else ""))
    return lines[-15:]


def parse_bench_note(note: str) -> dict:
    out: dict[str, str] = {}
    for part in (note or "").split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def build_report() -> str:
    monitor = load_jsonl(MONITOR_JSONL)
    bal = load_timing(BENCH_BALANCED_TIMING)
    hevc = load_timing(BENCH_HEVC_TIMING)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Bottleneck report — autopilot",
        f"**Обновлено:** {now}",
        "",
        "## Live monitor (`perf_monitor.jsonl`)",
        f"- Снимков: **{len(monitor)}**",
    ]

    if monitor:
        last = monitor[-1]
        lines += [
            f"- Текущая фаза: **{last.get('phase')}** — {last.get('detail')}",
            f"- Тип: {last.get('record_type')} chunk {last.get('chunk_index')}",
        ]
        for key, label in (
            ("copy_ok_sec", "Copy (сек/клип, tail avg)"),
            ("merge_mb_s", "Merge (MB/s, tail avg)"),
            ("encode_Nx", "Encode (Nx realtime)"),
            ("upload_MB_s", "Upload (MB/s)"),
        ):
            st = metric_stats(monitor, key)
            if st:
                lines.append(
                    f"- {label}: avg **{st['avg']}** (min {st['min']}, max {st['max']}, n={st['n']})"
                )

        lines += ["", "### Timeline (последние переходы)", *phase_timeline(monitor)]
    else:
        lines.append("- Нет данных — запусти `./scripts/monitor_autopilot_perf.py`")

    lines += ["", "## Bench 1h: balanced vs hevc", ""]
    lines.append("| Phase | balanced (s) | hevc (s) | Δ | hevc note |")
    lines.append("|-------|-------------|----------|---|-----------|")

    for ph in ("compose", "upload"):
        b = bal.get(ph, {})
        h = hevc.get(ph, {})
        bs, hs = b.get("elapsed_sec"), h.get("elapsed_sec")
        delta = ""
        if bs and hs:
            delta = f"{(hs - bs) / bs * 100:+.0f}%"
        lines.append(
            f"| {ph} | {bs or '—'} | {hs or '—'} | {delta or '—'} | {h.get('note', '—')[:60]} |"
        )

    bal_comp = parse_bench_note(bal.get("compose", {}).get("note", ""))
    hevc_comp = parse_bench_note(hevc.get("compose", {}).get("note", ""))
    if bal_comp.get("bytes") and hevc_comp.get("bytes"):
        bb, hb = int(bal_comp["bytes"]), int(hevc_comp["bytes"])
        lines += [
            "",
            f"- balanced output: **{bb/1e9:.2f} GB**",
            f"- hevc output: **{hb/1e9:.2f} GB** ({bb/hb:.2f}× smaller)" if hb else "",
        ]

    lines += [
        "",
        "## Выводы (авто)",
        "",
    ]

    conclusions = []
    try:
        import sys

        lib = PROJECT_ROOT / "lib"
        if str(lib) not in sys.path:
            sys.path.insert(0, str(lib))
        from compose_70mai import hevc_encoder_available

        if not hevc_encoder_available():
            conclusions.append(
                "- **HEVC:** `hevc_videotoolbox` недоступен — профиль `hevc` → H.264 fallback; "
                "для upload используй `youtube` (3 Mbps) или `balanced` (5 Mbps)"
            )
    except Exception:
        pass
    merge_st = metric_stats(monitor, "merge_mb_s")
    copy_st = metric_stats(monitor, "copy_ok_sec")
    if merge_st and copy_st and copy_st["avg"] > 0:
        if merge_st["avg"] > 150:
            conclusions.append("- **Import:** merge быстрый (~{:.0f} MB/s); copy доминирует (~{:.0f}s/клип)".format(merge_st["avg"], copy_st["avg"]))
        else:
            conclusions.append("- **Import:** merge замедлен — возможно disk contention с параллельным compose bench")

    if bal.get("upload") and hevc.get("upload"):
        bu, hu = bal["upload"]["elapsed_sec"], hevc["upload"]["elapsed_sec"]
        if hu < bu:
            conclusions.append(f"- **Upload:** hevc быстрее на {(bu-hu)/bu*100:.0f}% ({hu}s vs {bu}s)")
    elif bal.get("upload"):
        conclusions.append(
            f"- **Upload (balanced baseline):** {bal['upload']['elapsed_sec']}s — главное узкое место при текущей сети"
        )

    if bal.get("compose") and not hevc.get("compose"):
        conclusions.append("- **Compose bench hevc:** ещё идёт — см. `bench_1h_hevc/compose.log`")
    elif hevc.get("compose") and bal.get("compose"):
        bc, hc = bal["compose"]["elapsed_sec"], hevc["compose"]["elapsed_sec"]
        conclusions.append(f"- **Compose:** hevc {hc}s vs balanced {bc}s")

    if not conclusions:
        conclusions.append("- Собираем данные — отчёт обновится по мере bench/monitor")

    lines.extend(conclusions)
    lines += [
        "",
        "## Артефакты",
        f"- Monitor: `{MONITOR_JSONL}`",
        f"- Balanced bench: `{BENCH_BALANCED_TIMING}`",
        f"- HEVC bench: `{BENCH_HEVC_TIMING}`",
        f"- Performance: `анализ/performance_report.md`",
        "",
        "Перегенерация: `python3 анализ/generate_bottleneck_report.py`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Refresh every 60s")
    parser.add_argument("-o", "--output", type=Path, default=OUT)
    args = parser.parse_args()

    while True:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        text = build_report()
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
        if not args.watch:
            break
        time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
