#!/usr/bin/env python3
"""Summarize the structured host trace from an Autopilot run."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def report(rows: list[dict]) -> str:
    stages: list[dict] = []
    samples = [r for r in rows if r.get("event") == "sample"]
    for row in rows:
        if row.get("event") == "stage_start":
            continue
        elif row.get("event") == "stage_end":
            stages.append(row)

    lines = ["# Host performance trace", ""]
    if stages:
        lines += ["## Stages", "", "| Stage | Chunk | Seconds | Exit |", "|---|---:|---:|---:|"]
        for row in stages:
            lines.append(
                f"| {row.get('stage', '?')} | {row.get('chunk_index') or '—'} | "
                f"{row.get('elapsed_sec', '—')} | {row.get('returncode', '—')} |"
            )
    else:
        lines += ["No completed stages found.", ""]

    if samples:
        cpus = [float(r["process_cpu_pct"]) for r in samples if "process_cpu_pct" in r]
        rss = [float(r["process_rss_mb"]) for r in samples if "process_rss_mb" in r]
        lines += [
            "",
            "## Host samples",
            f"- Samples: **{len(samples)}**",
            f"- Process tree CPU: peak **{max(cpus):.1f}%**, median **{statistics.median(cpus):.1f}%**"
            if cpus
            else "- Process tree CPU: unavailable",
            f"- Process tree RSS: peak **{max(rss):.1f} MB**"
            if rss
            else "- Process tree RSS: unavailable",
        ]
        free = [r.get("disk_free_gb") for r in samples if r.get("disk_free_gb") is not None]
        if free:
            lines.append(f"- Disk free: minimum **{min(free):.2f} GB**")
    lines += [
        "",
        "Interpretation: high CPU with low disk/network progress points to Compose; "
        "low CPU with changing disk usage points to Import; low CPU and long elapsed "
        "time with no local progress points to Upload/network.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Autopilot host timing trace")
    parser.add_argument(
        "trace",
        type=Path,
        nargs="?",
        default=Path("video/Output/.publish_tmp/perf_trace.jsonl"),
    )
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    text = report(load(args.trace))
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
