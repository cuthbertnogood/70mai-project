#!/usr/bin/env python3
"""Localhost web Dashboard for Autopilot."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from autopilot_control import (
    peek_control,
    read_run_state,
    write_run_state,
)

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = asdict(row)
    for key in ("trip_start", "trip_end"):
        val = data.get(key)
        if val is not None and hasattr(val, "isoformat"):
            data[key] = val.isoformat(sep=" ", timespec="seconds")
    return data


def _row_field(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _sum_sd_trips(
    trips: list[dict[str, Any]],
    *,
    statuses: set[str] | None = None,
) -> dict[str, float | int]:
    total_sec = 0.0
    total_bytes = 0
    total_clips = 0
    for trip in trips:
        status = str(trip.get("import_status") or "none")
        if statuses is not None and status not in statuses:
            continue
        total_sec += float(trip.get("duration_sec") or 0.0)
        total_bytes += int(trip.get("size_bytes") or 0)
        total_clips += int(trip.get("clip_count") or 0)
    return {
        "hours_sec": total_sec,
        "bytes": total_bytes,
        "clips": total_clips,
    }


def _volume_from_sum(totals: dict[str, float | int]) -> dict[str, Any]:
    return _volume_block(
        hours_sec=float(totals.get("hours_sec") or 0.0),
        bytes_total=int(totals.get("bytes") or 0),
        clips=int(totals.get("clips") or 0),
    )


def _volume_block(
    *,
    hours_sec: float = 0.0,
    bytes_total: int = 0,
    clips: int = 0,
    chunks: int = 0,
) -> dict[str, Any]:
    from import_70mai import format_duration
    from publish_all_70mai import format_gb

    out: dict[str, Any] = {
        "hours_sec": round(hours_sec, 1),
        "hours": format_duration(hours_sec) if hours_sec > 0 else "—",
        "bytes": bytes_total,
        "size": format_gb(bytes_total) if bytes_total > 0 else "—",
    }
    if clips > 0:
        out["clips"] = clips
    if chunks > 0:
        out["chunks"] = chunks
    return out


def _node_state(
    node_id: str,
    *,
    active_ids: set[str],
    passed: dict[str, Any],
) -> str:
    if node_id in active_ids:
        return "active"
    has_passed = (
        float(passed.get("hours_sec") or 0) > 0
        or int(passed.get("bytes") or 0) > 0
        or int(passed.get("clips") or 0) > 0
        or int(passed.get("chunks") or 0) > 0
    )
    if node_id == "sd":
        return "done" if has_passed else "wait"
    return "done" if has_passed else "wait"


def build_pipeline_payload(
    *,
    sd_card: dict[str, Any],
    rows: list[Any],
    usage: dict[str, int],
    live: dict[str, Any] | None,
    processes: list[dict[str, Any]],
    temp_dir: Path,
) -> dict[str, Any]:
    """Pipeline diagram data: SD → copy → merge → compose → upload."""
    trips = sd_card.get("trips") if isinstance(sd_card.get("trips"), list) else []
    sd_all = _sum_sd_trips(trips)
    imported = _sum_sd_trips(trips, statuses={"imported", "uploaded"})

    compose_rows = [
        r
        for r in rows
        if str(_row_field(r, "status") or "") in ("upload", "done")
    ]
    compose_sec = sum(float(_row_field(r, "duration_sec") or 0.0) for r in compose_rows)
    compose_chunks = len(
        {
            (str(_row_field(r, "record_type")), int(_row_field(r, "chunk_index") or 0))
            for r in compose_rows
        }
    )

    upload_stats: dict[str, Any] | None = None
    try:
        from autopilot_dashboard import summarize_youtube_upload_stats

        upload_stats = summarize_youtube_upload_stats(rows, temp_dir)
    except Exception:
        upload_stats = None

    phase = str((live or {}).get("phase") or "")
    proc_roles = {str(p.get("role") or "") for p in processes}
    import_active = phase == "import" or bool(
        proc_roles & {"import", "prefetch"}
    )
    active_ids: set[str] = set()
    if import_active:
        active_ids.update({"copy", "merge"})
    if phase == "compose":
        active_ids.add("compose")
    if phase == "upload":
        active_ids.add("upload")

    merged_bytes = int(usage.get("merged") or 0)
    composed_bytes = int(usage.get("composed") or 0)

    upload_passed = _volume_block()
    if upload_stats:
        upload_passed = _volume_block(
            hours_sec=float(upload_stats.get("footage_sec") or 0.0),
            bytes_total=int(upload_stats.get("upload_bytes") or 0),
            chunks=int(upload_stats.get("n_videos") or 0),
        )

    partial = _sum_sd_trips(trips, statuses={"partial"})
    partial_note = ""
    if int(partial.get("clips") or 0) > 0 and import_active:
        from import_70mai import format_duration

        partial_note = (
            f"частично {format_duration(float(partial['hours_sec']))} "
            f"({int(partial['clips'])} клипов)"
        )

    nodes: list[dict[str, Any]] = [
        {
            "id": "sd",
            "label": "SD карта",
            "purpose": "Исходники: минутные клипы Front/Back",
            "flow": "флешка 70mai",
            "state": _node_state(
                "sd", active_ids=active_ids, passed=_volume_from_sum(sd_all)
            ),
            "passed": _volume_from_sum(sd_all),
            "on_disk": _volume_block(bytes_total=int(sd_all["bytes"])),
            "arrow_after": "не трогаем оригинал",
        },
        {
            "id": "copy",
            "label": "copy",
            "purpose": "Снять клипы с флешки на SSD",
            "flow": "SD → SSD staging",
            "state": _node_state(
                "copy", active_ids=active_ids, passed=_volume_from_sum(imported)
            ),
            "passed": _volume_from_sum(imported),
            "on_disk": _volume_block(),
            "arrow_after": "staging, потом удаляется",
            "detail": partial_note,
        },
        {
            "id": "merge",
            "label": "merge",
            "purpose": "Склейка ~10 мин Front/Back MP4",
            "flow": "клипы → merged",
            "state": _node_state(
                "merge", active_ids=active_ids, passed=_volume_from_sum(imported)
            ),
            "passed": _volume_from_sum(imported),
            "on_disk": _volume_block(bytes_total=merged_bytes),
            "arrow_after": "синхрон Front+Back",
            "detail": partial_note,
        },
        {
            "id": "compose",
            "label": "compose",
            "purpose": "Вертикальный 2-cam ролик ~2h",
            "flow": "merged → trip MP4",
            "state": _node_state(
                "compose",
                active_ids=active_ids,
                passed=_volume_block(
                    hours_sec=compose_sec, chunks=compose_chunks
                ),
            ),
            "passed": _volume_block(
                hours_sec=compose_sec, chunks=compose_chunks
            ),
            "on_disk": _volume_block(bytes_total=composed_bytes),
            "arrow_after": "resumable PUT",
        },
        {
            "id": "upload",
            "label": "upload",
            "purpose": "Публикация на YouTube",
            "flow": "Mac → YouTube",
            "state": _node_state(
                "upload", active_ids=active_ids, passed=upload_passed
            ),
            "passed": upload_passed,
            "on_disk": _volume_block(),
        },
    ]

    return {
        "nodes": nodes,
        "active_ids": sorted(active_ids),
    }


def build_status_payload(
    *,
    temp_dir: Path,
    video_dir: Path,
    types: list[str],
    source: Path | None,
    min_free_gb: float,
) -> dict[str, Any]:
    from autopilot_dashboard import (
        chunk_summary_counts,
        free_disk_gb,
        list_pipeline_processes,
        resolve_live_status,
    )

    run_state = read_run_state(temp_dir)
    sd = None
    usage_total = usage_merged = usage_composed = 0
    usage: dict[str, int] = {"merged": 0, "composed": 0, "total": 0}

    def _format_gb(n: float) -> str:
        return f"{n / 1e9:.1f} GB"

    try:
        from publish_all_70mai import autopilot_disk_usage, find_sd_card, format_gb

        sd = find_sd_card()
        _format_gb = format_gb
        usage = autopilot_disk_usage(video_dir, temp_dir, types=types)
        usage_merged = int(usage.get("merged", 0))
        usage_composed = int(usage.get("composed", 0))
        usage_total = int(usage.get("total", 0))
    except Exception:
        pass

    if sd and not run_state.get("sd_path"):
        run_state = {**run_state, "sd_path": str(sd)}

    rows: list[Any] = []
    chunks_done = chunk_total = trips_done = 0
    live = resolve_live_status(temp_dir, rows=rows)
    failures: list[str] = []

    try:
        from autopilot_dashboard import Dashboard

        dash = Dashboard(
            temp_dir=temp_dir,
            video_dir=video_dir,
            check_disk=Path("."),
            min_free_gb=min_free_gb,
            source=source or sd,
            types=types,
            enabled=False,
        )
        dash.reload_plan_if_changed()
        if dash.rows:
            rows = dash.rows
            try:
                dash._refresh_from_publish_state()
                dash._refresh_from_status()
            except Exception:
                pass
            live = resolve_live_status(temp_dir, rows=rows)
            chunks_done, chunk_total, trips_done = chunk_summary_counts(rows)
        try:
            from autopilot_dashboard import collect_failure_lines

            failures = collect_failure_lines(temp_dir, source=source or sd)
        except Exception:
            failures = []
    except Exception:
        pass

    sd_card = {"present": False, "trips": []}
    try:
        from autopilot_sd_table import build_sd_card_payload

        sd_card = build_sd_card_payload(
            source or sd, types, video_dir=video_dir, temp_dir=temp_dir
        )
    except Exception:
        pass

    procs = list_pipeline_processes(temp_dir=temp_dir)
    proc_payload = [
        {
            "pid": p.pid,
            "role": p.role,
            "uptime_sec": p.etime_sec,
            "tip": p.tip,
        }
        for p in procs
    ]
    pipeline = build_pipeline_payload(
        sd_card=sd_card,
        rows=rows,
        usage=usage,
        live=live,
        processes=proc_payload,
        temp_dir=temp_dir,
    )

    return {
        "run": run_state,
        "diagnostics": _read_diagnostics(temp_dir),
        "sd_present": sd is not None,
        "sd_path": str(sd) if sd else None,
        "sd_card": sd_card,
        "live": live,
        "pipeline": pipeline,
        "summary": {
            "chunks_done": chunks_done,
            "chunk_total": chunk_total,
            "trips_done": trips_done,
            "trip_total": len(rows),
        },
        "disk": {
            "free_gb": round(free_disk_gb(Path(".")), 1),
            "video_total_gb": _format_gb(usage_total),
            "merged_gb": _format_gb(usage_merged),
            "composed_gb": _format_gb(usage_composed),
        },
        "rows": [_row_to_dict(r) for r in rows],
        "failures": failures,
        "processes": proc_payload,
        "pending_control": peek_control(temp_dir),
    }


def _dashboard_html_bytes() -> bytes:
    return _DASHBOARD_HTML.encode("utf-8")


def build_file_map_payload(
    source: Path | None,
    types: list[str],
    *,
    video_dir: Path,
    temp_dir: Path,
) -> dict[str, Any]:
    try:
        from autopilot_file_map import build_file_map_payload as _build_file_map

        sd = source
        if sd is None:
            try:
                from publish_all_70mai import find_sd_card

                sd = find_sd_card()
            except Exception:
                pass
        return _build_file_map(
            sd,
            types,
            video_dir=video_dir,
            temp_dir=temp_dir,
        )
    except Exception:
        return {"present": False, "total": 0, "counts": {}, "groups": []}


def _reload_dashboard_module():
    """Pick up HTML/API changes without restarting the Autopilot process."""
    import importlib

    import autopilot_file_map as file_map
    import autopilot_sd_table as sd_table

    importlib.reload(sd_table)
    sd_table.clear_sd_table_cache()
    importlib.reload(file_map)
    file_map.clear_file_map_cache()
    return importlib.reload(sys.modules[__name__])


def _read_diagnostics(temp_dir: Path) -> dict[str, Any]:
    path = temp_dir / "autopilot_diagnostics.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Autopilot</title>
  <style>
    :root { font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf3; }
    body { margin: 0; padding: 1rem 1.25rem 2rem; max-width: 1680px; }
    h1 { margin: 0 0 .25rem; font-size: 1.4rem; }
    h2 { margin: 0 0 .5rem; font-size: 1rem; color: #8b9bb4; font-weight: 600; }
    .sub { color: #8b9bb4; margin-bottom: .75rem; }
    .pipeline-panel { background: #1a2332; border-radius: 8px; padding: .75rem 1rem; margin-bottom: 1rem; }
    .pipeline-panel h2 { margin: 0 0 .6rem; }
    .pipeline-flow { display: flex; align-items: stretch; gap: 0; overflow-x: auto; padding-bottom: .25rem; }
    .pl-node { flex: 1 1 0; min-width: 9.5rem; background: #243044; border-radius: 6px; padding: .55rem .65rem; border: 2px solid transparent; }
    .pl-node.st-sd { border-color: #4a5768; }
    .pl-node.st-copy { border-color: #f5b041; }
    .pl-node.st-merge { border-color: #7fd1ff; }
    .pl-node.st-compose { border-color: #e59866; }
    .pl-node.st-upload { border-color: #58d68d; }
    .pl-node.active { box-shadow: 0 0 0 1px currentColor; }
    .pl-node.st-sd.active { border-color: #8b9bb4; color: #e7ecf3; }
    .pl-node.st-copy.active { border-color: #f5b041; color: #f5b041; }
    .pl-node.st-merge.active { border-color: #7fd1ff; color: #7fd1ff; }
    .pl-node.st-compose.active { border-color: #e59866; color: #e59866; }
    .pl-node.st-upload.active { border-color: #58d68d; color: #58d68d; }
    .pl-node.done .pl-title::before { content: '✓ '; color: #58d68d; }
    .pl-node.active .pl-title::before { content: '► '; }
    .pl-node.wait .pl-title::before { content: '· '; color: #8b9bb4; }
    .pl-title { font-weight: 700; font-size: .88rem; margin-bottom: .2rem; }
    .pl-purpose { font-size: .72rem; color: #8b9bb4; line-height: 1.35; margin-bottom: .35rem; min-height: 2.2em; }
    .pl-flow { font-size: .7rem; color: #5dade2; margin-bottom: .3rem; }
    .pl-vol { font-size: .72rem; line-height: 1.4; }
    .pl-vol span { color: #8b9bb4; }
    .pl-detail { font-size: .68rem; color: #f5b041; margin-top: .25rem; }
    .pl-arrow { flex: 0 0 auto; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 0 .15rem; color: #566573; font-size: 1.1rem; min-width: 2.5rem; }
    .pl-arrow-label { font-size: .62rem; color: #8b9bb4; text-align: center; line-height: 1.2; max-width: 4.5rem; margin-bottom: .15rem; }
    @media (max-width: 900px) {
      .pipeline-flow { flex-direction: column; align-items: stretch; }
      .pl-arrow { flex-direction: row; justify-content: flex-start; padding: .15rem 0; min-width: 0; }
      .pl-arrow-label { max-width: none; margin: 0 .35rem 0 0; text-align: left; }
    }
    .layout { display: grid; grid-template-columns: 1fr minmax(320px, 420px); gap: 1.25rem; align-items: start; }
    @media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }
    .sd-panel { background: #1a2332; border-radius: 8px; padding: .75rem 1rem; position: sticky; top: .5rem; max-height: calc(100vh - 2rem); overflow: auto; }
    .sd-meta { font-size: .78rem; color: #8b9bb4; margin-bottom: .6rem; line-height: 1.45; }
    .sd-table { font-size: .78rem; }
    .sd-table th, .sd-table td { padding: .3rem .35rem; }
    .sd-table tr.imp-uploaded td { color: #58d68d; }
    .sd-table tr.imp-imported td { color: #7fd1ff; }
    .sd-table tr.imp-partial td, .sd-table tr.imp-pending td { color: #f5b041; }
    .sd-table tr.imp-failed td { color: #ec7063; }
    .sd-table tr.imp-none td { color: #8b9bb4; }
    .dot { display: inline-block; width: .5rem; height: .5rem; border-radius: 50%; margin-right: .35rem; background: currentColor; }
    .sd-legend { font-size: .72rem; color: #8b9bb4; margin-top: .5rem; display: flex; flex-wrap: wrap; gap: .5rem; }
    .sd-legend span.imp-uploaded { color: #58d68d; }
    .sd-legend span.imp-imported { color: #7fd1ff; }
    .sd-legend span.imp-partial, .sd-legend span.imp-pending { color: #f5b041; }
    .sd-legend span.imp-none { color: #8b9bb4; }
    .blockmap-panel { margin-top: 1.25rem; background: #1a2332; border-radius: 8px; padding: .75rem 1rem; }
    .blockmap-legend { font-size: .78rem; color: #8b9bb4; margin-bottom: .6rem; display: flex; flex-wrap: wrap; gap: .65rem; }
    .blockmap-legend span { display: inline-flex; align-items: center; gap: .25rem; }
    .bm-swatch { display: inline-block; width: 9px; height: 9px; border-radius: 1px; background: #4a5768; }
    .blockmap-legend .st-oncard .bm-swatch { background: #4a5768; }
    .blockmap-legend .st-planned .bm-swatch { background: #f5b041; }
    .blockmap-legend .st-merged .bm-swatch { background: #7fd1ff; }
    .blockmap-legend .st-composed .bm-swatch { background: #e59866; }
    .blockmap-legend .st-uploaded .bm-swatch { background: #58d68d; }
    .blockmap-legend .st-error .bm-swatch { background: #ec7063; }
    .bm-group { margin-bottom: .75rem; }
    .bm-head { font-size: .78rem; color: #8b9bb4; margin-bottom: .25rem; }
    .bm-grid { display: flex; flex-wrap: wrap; gap: 1px; margin: .3rem 0 .2rem; }
    .bm-grid i { display: block; width: 9px; height: 9px; background: #4a5768; border-radius: 1px; cursor: default; }
    .bm-grid i.st-planned { background: #f5b041; }
    .bm-grid i.st-merged { background: #7fd1ff; }
    .bm-grid i.st-composed { background: #e59866; }
    .bm-grid i.st-uploaded { background: #58d68d; }
    .bm-grid i.st-error { background: #ec7063; }
    .bm-empty { font-size: .82rem; color: #8b9bb4; }
    .bar { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
    button { border: 0; border-radius: 6px; padding: .45rem .9rem; cursor: pointer; font-weight: 600; }
    button.stop { background: #c0392b; color: #fff; }
    button.skip { background: #d68910; color: #111; }
    button.repair { background: #2874a6; color: #fff; }
    button.profile { background: #8e44ad; color: #fff; }
    button.quit { background: #566573; color: #fff; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: .75rem; margin-bottom: 1rem; }
    .card { background: #1a2332; border-radius: 8px; padding: .75rem 1rem; }
    .card .k { color: #8b9bb4; font-size: .8rem; }
    .card .v { font-size: 1.1rem; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #243044; }
    th { color: #8b9bb4; font-weight: 600; }
    .phase-compose { color: #5dade2; }
    .phase-upload { color: #58d68d; }
    .phase-import { color: #f5b041; }
    .phase-done { color: #58d68d; }
    .phase-stopped, .phase-error { color: #ec7063; }
    .failures { margin-top: 1rem; background: #1a2332; border-radius: 8px; padding: .75rem 1rem; font-size: .82rem; color: #f5b7b1; }
    a { color: #5dade2; }
    #msg { min-height: 1.2rem; color: #f8c471; margin-bottom: .5rem; }
  </style>
</head>
<body>
  <h1>Autopilot</h1>
  <div class="sub" id="subtitle">Загрузка…</div>
  <section class="pipeline-panel" id="pipeline-panel">
    <h2>Конвейер</h2>
    <div class="pipeline-flow" id="pipeline"></div>
  </section>
  <div id="msg"></div>
  <div class="bar">
    <button class="stop" id="btn-stop">Stop</button>
    <button class="skip" id="btn-skip">Skip chunk</button>
    <button class="repair" id="btn-repair">Repair</button>
    <button class="profile" id="btn-profile">Профилировать хост</button>
    <button class="quit" id="btn-quit">Quit</button>
  </div>
  <div class="cards" id="cards"></div>
  <div class="layout">
    <div class="main-col">
  <table>
    <thead><tr><th>Ролик</th><th>Тип</th><th>Trip</th><th>Статус</th><th>Прогресс</th><th>YouTube</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="failures" id="failures" hidden></div>
    </div>
    <aside class="sd-panel">
      <h2>Флешка (SD)</h2>
      <div class="sd-meta" id="sd-meta">—</div>
      <table class="sd-table">
        <thead><tr><th>Тип</th><th>Поездка</th><th>Длит.</th><th>Место</th><th>Клипы</th></tr></thead>
        <tbody id="sd-trips"></tbody>
      </table>
      <div class="sd-legend" id="sd-legend" hidden>
        <span class="imp-uploaded"><i class="dot"></i>загружено</span>
        <span class="imp-imported"><i class="dot"></i>импортировано</span>
        <span class="imp-pending"><i class="dot"></i>в плане</span>
        <span class="imp-partial"><i class="dot"></i>частично</span>
        <span class="imp-none"><i class="dot"></i>не импортировано</span>
      </div>
    </aside>
  </div>
  <section class="blockmap-panel">
    <h2>Карта файлов на флешке</h2>
    <div class="blockmap-legend" id="bm-legend"></div>
    <div id="bm-groups"></div>
  </section>
  <script>
    const msg = document.getElementById('msg');
    const phaseLabels = {
      waiting_card: 'Ожидание SD-карты',
      running: 'Прогон',
      restarting: 'Рестарт после сбоя',
      done: 'Готово',
      stopped: 'Остановлено',
      error: 'Ошибка',
      quitting: 'Выход…',
    };
    async function send(action) {
      msg.textContent = '…';
      const r = await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action}),
      });
      const j = await r.json();
      msg.textContent = j.message || (r.ok ? 'OK' : 'Ошибка');
    }
    document.getElementById('btn-stop').onclick = () => send('stop');
    document.getElementById('btn-skip').onclick = () => send('skip');
    document.getElementById('btn-repair').onclick = () => send('repair');
    document.getElementById('btn-profile').onclick = () => send('profile');
    document.getElementById('btn-quit').onclick = () => send('quit');
    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    const bmStatusLabels = {
      oncard: 'на карте',
      planned: 'в плане',
      merged: 'смержен',
      composed: 'сжат',
      uploaded: 'залит',
      error: 'ошибка',
    };
    const bmStatusOrder = ['uploaded', 'composed', 'merged', 'planned', 'oncard', 'error'];
    function plVolLine(label, vol) {
      if (!vol) return '';
      const parts = [];
      if (vol.hours && vol.hours !== '—') parts.push(vol.hours);
      if (vol.size && vol.size !== '—') parts.push(vol.size);
      if (vol.clips) parts.push(vol.clips + ' клип.');
      if (vol.chunks) parts.push(vol.chunks + ' рол.');
      if (!parts.length) return '';
      return `<div class="pl-vol"><span>${esc(label)}:</span> ${esc(parts.join(' · '))}</div>`;
    }
    function renderPipeline(pipeline) {
      const el = document.getElementById('pipeline');
      const panel = document.getElementById('pipeline-panel');
      if (!pipeline || !pipeline.nodes || !pipeline.nodes.length) {
        if (panel) panel.hidden = true;
        el.innerHTML = '';
        return;
      }
      if (panel) panel.hidden = false;
      el.innerHTML = pipeline.nodes.map((node, idx) => {
        const st = node.state || 'wait';
        const passed = plVolLine('прошло', node.passed);
        const onDisk = plVolLine('сейчас', node.on_disk);
        const detail = node.detail ? `<div class="pl-detail">${esc(node.detail)}</div>` : '';
        const card = `<div class="pl-node st-${esc(node.id)} ${esc(st)}">
          <div class="pl-title">${esc(node.label)}</div>
          <div class="pl-purpose">${esc(node.purpose || '')}</div>
          <div class="pl-flow">${esc(node.flow || '')}</div>
          ${passed}${onDisk}${detail}
        </div>`;
        if (idx >= pipeline.nodes.length - 1) return card;
        const arrow = node.arrow_after
          ? `<div class="pl-arrow"><span class="pl-arrow-label">${esc(node.arrow_after)}</span>→</div>`
          : '<div class="pl-arrow">→</div>';
        return card + arrow;
      }).join('');
    }
    function renderFileMap(fm) {
      const legend = document.getElementById('bm-legend');
      const groups = document.getElementById('bm-groups');
      if (!fm || !fm.present) {
        legend.innerHTML = '';
        groups.innerHTML = '<div class="bm-empty">Карта не подключена</div>';
        return;
      }
      const counts = fm.counts || {};
      legend.innerHTML = bmStatusOrder.filter(k => counts[k]).map(k =>
        `<span class="st-${esc(k)}"><i class="bm-swatch"></i>${esc(bmStatusLabels[k] || k)} ${counts[k]}</span>`
      ).join('');
      groups.innerHTML = (fm.groups || []).map(g => {
        const blocks = (g.blocks || []).map(b =>
          `<i class="st-${esc(b.st)}" title="${esc(b.n)} · ${esc(b.t)} · ${esc(b.s)} · ${esc(bmStatusLabels[b.st] || b.st)}"></i>`
        ).join('');
        const noun = g.count === 1 ? 'файл' : 'файлов';
        return `<div class="bm-group"><div class="bm-head">${esc(g.record_type)} · ${esc(g.camera)} — ${g.count} ${noun}</div><div class="bm-grid">${blocks}</div></div>`;
      }).join('') || '<div class="bm-empty">нет файлов</div>';
    }
    function render(data) {
      const run = data.run || {};
      const phase = run.phase || 'running';
      document.getElementById('subtitle').textContent =
        (phaseLabels[phase] || phase) + (run.message ? ' — ' + run.message : '');
      renderPipeline(data.pipeline);
      const canQuit = ['waiting_card','done','stopped','error'].includes(phase);
      const canControl = ['running','restarting','waiting_card'].includes(phase);
      document.getElementById('btn-quit').disabled = !canQuit;
      document.getElementById('btn-stop').disabled = !canControl || phase === 'waiting_card';
      document.getElementById('btn-skip').disabled = !canControl || phase === 'waiting_card';
      document.getElementById('btn-repair').disabled = !canControl || phase === 'waiting_card';
      const diagnostics = data.diagnostics || {};
      const profileButton = document.getElementById('btn-profile');
      profileButton.disabled = ['running','restarting','quitting'].includes(phase) ||
        diagnostics.status === 'running';
      const trace = diagnostics.trace || [];
      const lastTrace = trace.length ? trace[trace.length - 1] : null;
      const live = data.live || {};
      const cards = [
        ['SD', data.sd_present ? (data.sd_path || 'да') : 'нет'],
        ['Фаза', live.phase || '—'],
        ['Ролики', `${data.summary.chunks_done}/${data.summary.chunk_total}`],
        ['Trips', `${data.summary.trips_done}/${data.summary.trip_total}`],
        ['Диск свободно', `${data.disk.free_gb} GB`],
        ['Деталь', live.detail || '—'],
        ['Диагностика', diagnostics.message || diagnostics.status || '—'],
        ['Последний этап', lastTrace ? `${lastTrace.stage} ${lastTrace.elapsed_sec || ''}s` : '—'],
      ];
      document.getElementById('cards').innerHTML = cards.map(([k,v]) =>
        `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div></div>`
      ).join('');
      const tbody = document.getElementById('rows');
      tbody.innerHTML = (data.rows || []).map(r => {
        const yt = r.youtube_url ? `<a href="${esc(r.youtube_url)}" target="_blank">watch</a>` : '—';
        const roll = r.chunk_display_index ? `р${r.chunk_display_index}/${r.chunk_total}` : '?';
        return `<tr>
          <td>${esc(roll)}</td>
          <td>${esc(r.record_type)}</td>
          <td>${esc(r.label)}</td>
          <td class="phase-${esc(r.status)}">${esc(r.status)}</td>
          <td>${esc(r.progress || '—')}</td>
          <td>${yt}</td>
        </tr>`;
      }).join('');
      const fail = document.getElementById('failures');
      if (data.failures && data.failures.length) {
        fail.hidden = false;
        fail.innerHTML = '<strong>Сбои</strong><br>' + data.failures.map(esc).join('<br>');
      } else {
        fail.hidden = true;
      }
      const sd = data.sd_card || {};
      const sdMeta = document.getElementById('sd-meta');
      const sdLegend = document.getElementById('sd-legend');
      if (!sd.present) {
        sdMeta.textContent = 'Карта не подключена';
        document.getElementById('sd-trips').innerHTML = '';
        sdLegend.hidden = true;
      } else {
        sdLegend.hidden = !(sd.trips || []).length;
        const d = sd.disk || {};
        sdMeta.innerHTML = [
          esc(sd.path || ''),
          d.total ? `свободно ${esc(d.free || '—')} / ${esc(d.total)}` : '',
          sd.video_total && sd.video_total !== '—' ? `видео на карте: ${esc(sd.video_total)}` : '',
          sd.updated_at ? `инвентарь: ${esc(sd.updated_at)}` : '',
        ].filter(Boolean).join('<br>');
        document.getElementById('sd-trips').innerHTML = (sd.trips || []).map(t =>
          `<tr class="imp-${esc(t.import_status || 'none')}" title="${esc(t.import_label || '')}">
            <td>${esc(t.record_type)}</td>
            <td title="${esc(t.start || '')} → ${esc(t.end || '')}"><i class="dot"></i>${esc(t.label)}</td>
            <td>${esc(t.duration)}</td>
            <td>${esc(t.size)}</td>
            <td>${esc(String(t.clip_count))}</td>
          </tr>`
        ).join('') || '<tr><td colspan="5">нет данных</td></tr>';
      }
    }
    async function tick() {
      try {
        const r = await fetch('/api/status', {cache: 'no-store'});
        render(await r.json());
      } catch (e) {
        document.getElementById('subtitle').textContent = 'Нет связи с Autopilot';
      }
    }
    async function tickFileMap() {
      try {
        const r = await fetch('/api/filemap', {cache: 'no-store'});
        renderFileMap(await r.json());
      } catch (e) {
        document.getElementById('bm-groups').innerHTML =
          '<div class="bm-empty">Нет данных карты файлов</div>';
      }
    }
    setInterval(tick, 1000);
    setInterval(tickFileMap, 15000);
    tick();
    tickFileMap();
  </script>
</body>
</html>
"""


class AutopilotWebServer:
    """Threaded HTTP server bound to loopback only."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        temp_dir: Path,
        video_dir: Path,
        types: list[str],
        min_free_gb: float,
        on_control: Callable[[str, dict[str, Any]], str],
        quit_event: threading.Event,
        manage_run_state: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._temp_dir = temp_dir
        self._video_dir = video_dir
        self._types = types
        self._min_free_gb = min_free_gb
        self._on_control = on_control
        self._quit_event = quit_event
        self._manage_run_state = manage_run_state
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._source: Path | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def set_source(self, source: Path | None) -> None:
        self._source = source

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                aw = _reload_dashboard_module()
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    body = aw._dashboard_html_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/status":
                    payload = aw.build_status_payload(
                        temp_dir=outer._temp_dir,
                        video_dir=outer._video_dir,
                        types=outer._types,
                        source=outer._source,
                        min_free_gb=outer._min_free_gb,
                    )
                    self._json(200, payload)
                    return
                if path == "/api/filemap":
                    payload = aw.build_file_map_payload(
                        outer._source,
                        outer._types,
                        video_dir=outer._video_dir,
                        temp_dir=outer._temp_dir,
                    )
                    self._json(200, payload)
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path != "/api/control":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._json(400, {"ok": False, "message": "invalid JSON"})
                    return
                action = str(data.get("action") or "")
                message = outer._on_control(action, data)
                self._json(200, {"ok": True, "message": message})

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        if self._manage_run_state:
            write_run_state(
                self._temp_dir,
                phase="waiting_card",
                message="ожидание 70mai SD",
                dashboard_url=self.url,
            )

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
