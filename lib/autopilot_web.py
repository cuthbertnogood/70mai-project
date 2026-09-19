#!/usr/bin/env python3
"""Localhost web Dashboard for Autopilot."""

from __future__ import annotations

import json
import re
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


_CLIP_SEC = 60.0
_SIZE_LABEL_RE = re.compile(r"([\d.]+)\s*(GB|MB|KB|B)\b", re.I)


def _parse_size_label(text: str) -> int:
    m = _SIZE_LABEL_RE.match(str(text or "").strip())
    if not m:
        return 0
    amount = float(m.group(1))
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    return int(amount * mult.get(m.group(2).upper(), 1))


def _aggregate_filemap(filemap: dict[str, Any]) -> dict[str, dict[str, int | float]]:
    totals: dict[str, dict[str, int | float]] = {}
    for group in filemap.get("groups") or []:
        for block in group.get("blocks") or []:
            status = str(block.get("st") or "oncard")
            bucket = totals.setdefault(status, {"clips": 0, "bytes": 0})
            bucket["clips"] = int(bucket["clips"]) + 1
            bucket["bytes"] = int(bucket["bytes"]) + _parse_size_label(
                str(block.get("s") or "")
            )
    return totals


def _vol_from_clips_bytes(clips: int, bytes_total: int) -> dict[str, Any]:
    return _volume_block(
        hours_sec=clips * _CLIP_SEC,
        bytes_total=bytes_total,
        clips=clips,
    )


def _summary_node_state(passed_clips: int, remaining_clips: int) -> str:
    if passed_clips > 0 and remaining_clips <= 0:
        return "done"
    return "wait"


def _pipeline_node(
    node_id: str,
    label: str,
    purpose: str,
    flow: str,
    *,
    state: str,
    passed: dict[str, Any],
    remaining: dict[str, Any] | None = None,
    on_disk: dict[str, Any] | None = None,
    arrow_after: str = "",
    detail: str = "",
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "label": label,
        "purpose": purpose,
        "flow": flow,
        "state": state,
        "passed": passed,
    }
    if remaining is not None:
        node["remaining"] = remaining
    if on_disk is not None:
        node["on_disk"] = on_disk
    if arrow_after:
        node["arrow_after"] = arrow_after
    if detail:
        node["detail"] = detail
    return node


def build_pipeline_summary(
    *,
    filemap: dict[str, Any],
    usage: dict[str, int],
) -> dict[str, Any]:
    """Whole-card pipeline: passed / remaining per stage (filemap clip statuses)."""
    by_status = _aggregate_filemap(filemap)
    counts = filemap.get("counts") if isinstance(filemap.get("counts"), dict) else {}
    total_clips = int(filemap.get("total") or 0)
    total_bytes = sum(int(v.get("bytes") or 0) for v in by_status.values())

    def _clips_bytes(statuses: tuple[str, ...]) -> tuple[int, int]:
        clips = sum(int(by_status.get(s, {}).get("clips") or 0) for s in statuses)
        bytes_total = sum(int(by_status.get(s, {}).get("bytes") or 0) for s in statuses)
        return clips, bytes_total

    sd_passed = _vol_from_clips_bytes(total_clips, total_bytes)
    merge_passed_c, merge_passed_b = _clips_bytes(("merged", "composed", "uploaded"))
    merge_remain_c, merge_remain_b = _clips_bytes(("oncard", "planned"))
    compose_passed_c, compose_passed_b = _clips_bytes(("composed", "uploaded"))
    compose_remain_c, compose_remain_b = _clips_bytes(("merged",))
    upload_passed_c, upload_passed_b = _clips_bytes(("uploaded",))
    upload_remain_c, upload_remain_b = _clips_bytes(("composed",))

    merged_bytes = int(usage.get("merged") or 0)
    composed_bytes = int(usage.get("composed") or 0)

    nodes = [
        _pipeline_node(
            "sd",
            "SD карта",
            "Исходники: минутные клипы Front/Back",
            "флешка 70mai",
            state="done" if total_clips > 0 else "wait",
            passed=sd_passed,
            on_disk=_volume_block(bytes_total=total_bytes),
            arrow_after="не трогаем оригинал",
        ),
        _pipeline_node(
            "copy",
            "copy",
            "Снять клипы с флешки на SSD",
            "SD → SSD staging",
            state=_summary_node_state(merge_passed_c, merge_remain_c),
            passed=_vol_from_clips_bytes(merge_passed_c, merge_passed_b),
            remaining=_vol_from_clips_bytes(merge_remain_c, merge_remain_b),
            arrow_after="staging, потом удаляется",
        ),
        _pipeline_node(
            "merge",
            "merge",
            "Склейка ~10 мин Front/Back MP4",
            "клипы → merged",
            state=_summary_node_state(merge_passed_c, merge_remain_c),
            passed=_vol_from_clips_bytes(merge_passed_c, merge_passed_b),
            remaining=_vol_from_clips_bytes(merge_remain_c, merge_remain_b),
            on_disk=_volume_block(bytes_total=merged_bytes),
            arrow_after="синхрон Front+Back",
        ),
        _pipeline_node(
            "compose",
            "compose",
            "Вертикальный 2-cam ролик ~2h",
            "merged → trip MP4",
            state=_summary_node_state(compose_passed_c, compose_remain_c),
            passed=_vol_from_clips_bytes(compose_passed_c, compose_passed_b),
            remaining=_vol_from_clips_bytes(compose_remain_c, compose_remain_b),
            on_disk=_volume_block(bytes_total=composed_bytes),
            arrow_after="resumable PUT",
        ),
        _pipeline_node(
            "upload",
            "upload",
            "Публикация на YouTube",
            "Mac → YouTube",
            state=_summary_node_state(upload_passed_c, upload_remain_c),
            passed=_vol_from_clips_bytes(upload_passed_c, upload_passed_b),
            remaining=_vol_from_clips_bytes(upload_remain_c, upload_remain_b),
        ),
    ]
    return {"title": "Вся карта", "nodes": nodes, "counts": counts}


def _resolve_current_phase(live: dict[str, Any], chunk_rows: list[Any]) -> str:
    phase = str(live.get("phase") or "")
    if phase in ("import", "compose", "upload"):
        return phase
    if phase in ("stall", "oauth"):
        for row in chunk_rows:
            st = str(_row_field(row, "status") or "")
            if st in ("upload", "oauth"):
                return "upload"
            if st in ("compose", "stall"):
                return "compose"
    if phase == "done":
        return "done"
    return ""


def _current_chunk_context(
    rows: list[Any], live: dict[str, Any] | None
) -> tuple[list[Any], dict[str, Any] | None]:
    if not live:
        return [], None
    phase = str(live.get("phase") or "")
    if phase not in ("import", "compose", "upload", "stall", "oauth", "done"):
        return [], None
    record_type = str(live.get("record_type") or "")
    chunk_index = int(live.get("chunk_index") or 0)
    if not record_type or not chunk_index:
        for row in rows:
            st = str(_row_field(row, "status") or "")
            if st in ("import", "compose", "upload", "stall", "oauth"):
                record_type = str(_row_field(row, "record_type") or "")
                chunk_index = int(_row_field(row, "chunk_index") or 0)
                break
    if not record_type or not chunk_index:
        return [], None
    chunk_rows = [
        r
        for r in rows
        if str(_row_field(r, "record_type") or "") == record_type
        and int(_row_field(r, "chunk_index") or 0) == chunk_index
    ]
    if not chunk_rows:
        return [], None
    return chunk_rows, {
        "record_type": record_type,
        "chunk_index": chunk_index,
        "phase": _resolve_current_phase(live, chunk_rows),
        "trip_index": int(live.get("trip_index") or 0),
    }


def _chunk_volume(chunk_rows: list[Any]) -> dict[str, Any]:
    total_sec = sum(float(_row_field(r, "duration_sec") or 0.0) for r in chunk_rows)
    total_clips = sum(int(_row_field(r, "clip_count") or 0) for r in chunk_rows)
    return _volume_block(hours_sec=total_sec, clips=total_clips)


def _live_stage_detail(
    live: dict[str, Any],
    stage: str,
    *,
    temp_dir: Path | None = None,
) -> str:
    conveyors = live.get("conveyors")
    if not isinstance(conveyors, dict):
        conveyors = {}
    if stage in ("copy", "merge"):
        lane = conveyors.get(stage)
        if not isinstance(lane, dict):
            return ""
        parts: list[str] = []
        chunk = str(lane.get("chunk") or "").strip()
        file_name = str(lane.get("file") or "").strip()
        if chunk:
            parts.append(chunk)
        if file_name:
            parts.append(file_name[:32])
        bd = lane.get("bytes_done")
        bt = lane.get("bytes_total")
        if isinstance(bd, (int, float)) and isinstance(bt, (int, float)) and bt > 0:
            parts.append(f"{100.0 * float(bd) / float(bt):.0f}%")
        # Prefer rich copy/merge detail (speed + ETA) from logs when available.
        try:
            from autopilot_dashboard import (
                format_copy_detail,
                format_merge_detail,
                parse_copy_log_detail,
                parse_merge_log_detail,
            )

            if stage == "copy" and temp_dir is not None:
                _short, detail = format_copy_detail(parse_copy_log_detail(temp_dir))
                if detail:
                    return detail
            if stage == "merge" and temp_dir is not None:
                _short, detail = format_merge_detail(parse_merge_log_detail(temp_dir))
                if detail:
                    return detail
        except Exception:
            pass
        return " · ".join(parts)

    try:
        from autopilot_dashboard import (
            format_compose_detail,
            format_upload_detail,
            parse_compose_log_detail,
            parse_upload_log_detail,
        )

        if stage == "compose":
            short, detail = format_compose_detail(
                live, log_detail=parse_compose_log_detail(temp_dir)
            )
            return detail or short or ""
        if stage == "upload":
            short, detail = format_upload_detail(
                live, log_detail=parse_upload_log_detail(temp_dir)
            )
            return detail or short or ""
    except Exception:
        pass

    pct = live.get("percent")
    detail = str(live.get("detail") or "").strip()
    bits: list[str] = []
    speed = live.get("speed")
    unit = str(live.get("speed_unit") or "").strip()
    eta = str(live.get("eta") or "").strip()
    if isinstance(pct, (int, float)):
        bits.append(f"{float(pct):.0f}%")
    if isinstance(speed, (int, float)) and speed > 0:
        if unit == "x" or not unit:
            bits.append(f"{float(speed):.2f}x")
        else:
            bits.append(f"{float(speed):.1f} {unit}")
    if eta:
        bits.append(f"ETA {eta}")
    if detail and not bits:
        bits.append(detail[:48])
    elif detail and len(bits) < 3:
        bits.append(detail[:40])
    return " · ".join(bits)


def _stage_speed_eta(
    live: dict[str, Any],
    stage: str,
    *,
    temp_dir: Path | None = None,
) -> tuple[str, str]:
    """Return (speed_txt, eta_txt) for the active pipeline node."""
    speed_txt = ""
    eta_txt = ""
    try:
        from autopilot_dashboard import (
            format_compose_detail,
            format_copy_detail,
            format_merge_detail,
            format_upload_detail,
            parse_compose_log_detail,
            parse_copy_log_detail,
            parse_merge_log_detail,
            parse_upload_log_detail,
        )

        detail = ""
        if stage == "copy" and temp_dir is not None:
            _, detail = format_copy_detail(parse_copy_log_detail(temp_dir))
        elif stage == "merge" and temp_dir is not None:
            _, detail = format_merge_detail(parse_merge_log_detail(temp_dir))
        elif stage == "compose":
            _, detail = format_compose_detail(
                live, log_detail=parse_compose_log_detail(temp_dir)
            )
        elif stage == "upload":
            _, detail = format_upload_detail(
                live, log_detail=parse_upload_log_detail(temp_dir)
            )
        detail = detail or ""
        # Extract speed / ETA tokens from the detail line.
        for part in detail.split("·"):
            token = part.strip()
            low = token.lower()
            if "mb/s" in low or token.endswith("x") and token[:-1].replace(".", "", 1).isdigit():
                speed_txt = token
            elif low.startswith("eta "):
                eta_txt = token[4:].strip() or token
    except Exception:
        pass

    if not speed_txt:
        speed = live.get("speed")
        unit = str(live.get("speed_unit") or "").strip()
        if isinstance(speed, (int, float)) and speed > 0:
            if unit == "x" or (stage == "compose" and not unit):
                speed_txt = f"{float(speed):.2f}x"
            else:
                speed_txt = f"{float(speed):.1f} {unit or 'MB/s'}"
    if not eta_txt:
        eta_txt = str(live.get("eta") or "").strip()
    return speed_txt, eta_txt


def _current_node_state(
    node_id: str, phase: str, active: set[str], done: set[str]
) -> str:
    if node_id in active:
        return "active"
    if node_id in done:
        return "done"
    return "wait"


def build_pipeline_current(
    *,
    rows: list[Any],
    live: dict[str, Any] | None,
    processes: list[dict[str, Any]],
    temp_dir: Path | None = None,
) -> dict[str, Any]:
    """Active roll only — no cumulative ✓ from other chunks."""
    chunk_rows, ctx = _current_chunk_context(rows, live)
    if not ctx:
        return {"title": "Сейчас", "nodes": [], "idle": True}

    phase = str(ctx.get("phase") or "")
    proc_roles = {str(p.get("role") or "") for p in processes}
    if phase == "import" or (not phase and proc_roles & {"import", "prefetch"}):
        phase = "import"

    active: set[str] = set()
    done: set[str] = set()
    if phase == "import":
        active = {"copy", "merge"}
    elif phase == "compose":
        done = {"copy", "merge"}
        active = {"compose"}
    elif phase == "upload":
        done = {"copy", "merge", "compose"}
        active = {"upload"}
    elif phase == "done":
        done = {"copy", "merge", "compose", "upload"}

    chunk_vol = _chunk_volume(chunk_rows)
    first = chunk_rows[0]
    disp = int(_row_field(first, "chunk_display_index") or 0)
    total = int(_row_field(first, "chunk_total") or 0)
    record_type = str(ctx.get("record_type") or "")
    roll = f"р{disp}/{total}" if disp and total else f"chunk {ctx['chunk_index']}"
    title = f"{roll} {record_type}"

    live_dict = live if isinstance(live, dict) else {}

    def _active_fields(stage: str) -> dict[str, str]:
        if stage not in active:
            return {"detail": "", "speed": "", "eta": ""}
        detail = _live_stage_detail(live_dict, stage, temp_dir=temp_dir)
        speed, eta = _stage_speed_eta(live_dict, stage, temp_dir=temp_dir)
        return {"detail": detail, "speed": speed, "eta": eta}

    copy_f = _active_fields("copy")
    merge_f = _active_fields("merge")
    compose_f = _active_fields("compose")
    upload_f = _active_fields("upload")

    nodes = [
        _pipeline_node(
            "sd",
            "SD карта",
            "Исходники этого ролика",
            "флешка 70mai",
            state="done",
            passed=chunk_vol,
            arrow_after="не трогаем оригинал",
        ),
        _pipeline_node(
            "copy",
            "copy",
            "Снять клипы с флешки на SSD",
            "SD → SSD staging",
            state=_current_node_state("copy", phase, active, done),
            passed=chunk_vol if "copy" in done else _volume_block(),
            detail=copy_f["detail"],
            arrow_after="staging, потом удаляется",
        ),
        _pipeline_node(
            "merge",
            "merge",
            "Склейка ~10 мин Front/Back MP4",
            "клипы → merged",
            state=_current_node_state("merge", phase, active, done),
            passed=chunk_vol if "merge" in done else _volume_block(),
            detail=merge_f["detail"],
            arrow_after="синхрон Front+Back",
        ),
        _pipeline_node(
            "compose",
            "compose",
            "Вертикальный 2-cam ролик ~2h",
            "merged → trip MP4",
            state=_current_node_state("compose", phase, active, done),
            passed=chunk_vol if "compose" in done else _volume_block(),
            detail=compose_f["detail"],
            arrow_after="resumable PUT",
        ),
        _pipeline_node(
            "upload",
            "upload",
            "Публикация на YouTube",
            "Mac → YouTube",
            state=_current_node_state("upload", phase, active, done),
            passed=chunk_vol if "upload" in done else _volume_block(),
            detail=upload_f["detail"],
        ),
    ]
    # Attach speed/ETA onto active nodes for prominent UI.
    for node, fields in (
        (nodes[1], copy_f),
        (nodes[2], merge_f),
        (nodes[3], compose_f),
        (nodes[4], upload_f),
    ):
        if fields["speed"]:
            node["speed"] = fields["speed"]
        if fields["eta"]:
            node["eta"] = fields["eta"]

    return {
        "title": title,
        "nodes": nodes,
        "idle": not active and phase != "done",
        "phase": phase,
    }


def build_pipeline_payload(
    *,
    filemap: dict[str, Any],
    sd_card: dict[str, Any],
    rows: list[Any],
    usage: dict[str, int],
    live: dict[str, Any] | None,
    processes: list[dict[str, Any]],
    temp_dir: Path,
) -> dict[str, Any]:
    """Summary (whole card) + current (active roll) pipeline diagrams."""
    del sd_card  # kept for caller symmetry / future use
    return {
        "summary": build_pipeline_summary(filemap=filemap, usage=usage),
        "current": build_pipeline_current(
            rows=rows, live=live, processes=processes, temp_dir=temp_dir
        ),
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
    filemap = build_file_map_payload(
        source or sd,
        types,
        video_dir=video_dir,
        temp_dir=temp_dir,
    )
    pipeline = build_pipeline_payload(
        filemap=filemap,
        sd_card=sd_card,
        rows=rows,
        usage=usage,
        live=live,
        processes=proc_payload,
        temp_dir=temp_dir,
    )

    processing: dict[str, Any] = {"clips": [], "host": []}
    try:
        from autopilot_file_map import processing_snapshot

        processing = processing_snapshot(
            source or sd,
            types,
            temp_dir=temp_dir,
            video_dir=video_dir,
            live=live if isinstance(live, dict) else None,
            rows=rows,
        )
    except Exception:
        pass

    return {
        "run": run_state,
        "diagnostics": _read_diagnostics(temp_dir),
        "sd_present": sd is not None,
        "sd_path": str(sd) if sd else None,
        "sd_card": sd_card,
        "live": live,
        "pipeline": pipeline,
        "processing": processing,
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
        return {
            "present": False,
            "total": 0,
            "counts": {},
            "groups": [],
            "host": {
                "present": False,
                "copied": {"done": 0, "total": 0},
                "merged": {"files": 0, "clips": 0},
                "youtube": {"done": 0, "total": 0},
                "groups": [],
                "total": 0,
            },
        }


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
    .pipeline-panel.current { border: 1px solid #3d5166; }
    .pipeline-panel h2 { margin: 0 0 .6rem; }
    .pipeline-panel.idle { opacity: .75; }
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
    .pl-rate { font-size: .78rem; font-weight: 650; color: #f8c471; margin-top: .2rem; }
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
    .blockmap-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; align-items: start; }
    @media (max-width: 1100px) { .blockmap-layout { grid-template-columns: 1fr; } }
    .blockmap-panel { background: #1a2332; border-radius: 8px; padding: .75rem 1rem; min-width: 0; }
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
    .bm-grid i.processing { box-shadow: 0 0 0 1px #0f1419, 0 0 0 2px #f8c471; position: relative; z-index: 1; }
    .blockmap-legend .st-processing .bm-swatch { box-shadow: 0 0 0 1px #0f1419, 0 0 0 2px #f8c471; background: #4a5768; }
    .bm-empty { font-size: .82rem; color: #8b9bb4; }
    .host-stats { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: .75rem; }
    .host-stat { background: #243044; border-radius: 6px; padding: .45rem .7rem; min-width: 9rem; }
    .host-stat .k { font-size: .72rem; color: #8b9bb4; margin-bottom: .15rem; }
    .host-stat .v { font-size: 1rem; font-weight: 650; }
    .host-stat .v .dim { color: #8b9bb4; font-weight: 500; font-size: .85rem; }
    .bar { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: .5rem; }
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
  <div class="bar">
    <button class="stop" id="btn-stop">Stop</button>
    <button class="skip" id="btn-skip">Skip chunk</button>
    <button class="repair" id="btn-repair">Repair</button>
    <button class="profile" id="btn-profile">Профилировать хост</button>
    <button class="quit" id="btn-quit">Quit</button>
  </div>
  <div id="msg"></div>
  <section class="pipeline-panel current" id="pipeline-current-panel">
    <h2 id="pipeline-current-title">Сейчас</h2>
    <div class="pipeline-flow" id="pipeline-current"></div>
  </section>
  <section class="pipeline-panel" id="pipeline-summary-panel">
    <h2>Вся карта</h2>
    <div class="pipeline-flow" id="pipeline-summary"></div>
  </section>
  <div class="blockmap-layout">
  <section class="blockmap-panel">
    <h2>Карта файлов на флешке</h2>
    <div class="blockmap-legend" id="bm-legend"></div>
    <div id="bm-groups"></div>
  </section>
  <section class="blockmap-panel">
    <h2>Файлы на хосте (HDD)</h2>
    <div class="host-stats" id="bm-host-stats"></div>
    <div class="blockmap-legend" id="bm-host-legend"></div>
    <div id="bm-host-groups"></div>
    <h2 style="margin-top:.85rem">Compose / YouTube MP4</h2>
    <div class="blockmap-legend" id="bm-compose-legend"></div>
    <div id="bm-compose-groups"></div>
  </section>
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
    function renderPipelineBlock(el, panel, block, mode) {
      if (!el) return;
      if (!block || !block.nodes || !block.nodes.length) {
        if (panel) panel.hidden = mode !== 'summary';
        el.innerHTML = mode === 'current'
          ? '<div class="bm-empty">ожидание — нет активного ролика</div>'
          : '';
        if (panel && mode === 'current') panel.classList.add('idle');
        return;
      }
      if (panel) {
        panel.hidden = false;
        panel.classList.toggle('idle', !!block.idle);
      }
      const titleEl = mode === 'current' ? document.getElementById('pipeline-current-title') : null;
      if (titleEl && block.title) titleEl.textContent = 'Сейчас — ' + block.title;
      el.innerHTML = block.nodes.map((node, idx) => {
        const st = node.state || 'wait';
        const passed = plVolLine('прошло', node.passed);
        const remaining = mode === 'summary' && node.remaining
          ? plVolLine('осталось', node.remaining) : '';
        const onDisk = plVolLine('на диске', node.on_disk);
        const detail = node.detail ? `<div class="pl-detail">${esc(node.detail)}</div>` : '';
        const rateBits = [];
        if (node.speed) rateBits.push(node.speed);
        if (node.eta) rateBits.push('ETA ' + node.eta);
        const rate = rateBits.length
          ? `<div class="pl-rate">${esc(rateBits.join(' · '))}</div>` : '';
        const card = `<div class="pl-node st-${esc(node.id)} ${esc(st)}">
          <div class="pl-title">${esc(node.label)}</div>
          <div class="pl-purpose">${esc(node.purpose || '')}</div>
          <div class="pl-flow">${esc(node.flow || '')}</div>
          ${passed}${remaining}${onDisk}${rate}${detail}
        </div>`;
        if (idx >= block.nodes.length - 1) return card;
        const arrow = node.arrow_after
          ? `<div class="pl-arrow"><span class="pl-arrow-label">${esc(node.arrow_after)}</span>→</div>`
          : '<div class="pl-arrow">→</div>';
        return card + arrow;
      }).join('');
    }
    function renderPipeline(pipeline) {
      const p = pipeline || {};
      renderPipelineBlock(
        document.getElementById('pipeline-current'),
        document.getElementById('pipeline-current-panel'),
        p.current,
        'current',
      );
      renderPipelineBlock(
        document.getElementById('pipeline-summary'),
        document.getElementById('pipeline-summary-panel'),
        p.summary,
        'summary',
      );
    }
    function renderBlockGroups(el, groups) {
      el.innerHTML = (groups || []).map(g => {
        const blocks = (g.blocks || []).map(b => {
          const procLabel = b.proc || (b.active ? 'сейчас в работе' : '');
          const proc = procLabel ? ` · ${procLabel}` : '';
          const cls = `st-${esc(b.st)}${b.active ? ' processing' : ''}`;
          return `<i class="${cls}" data-n="${esc(b.n)}" data-rt="${esc(g.record_type)}" data-cam="${esc(g.camera)}" title="${esc(b.n)} · ${esc(b.t)} · ${esc(b.s)} · ${esc(bmStatusLabels[b.st] || b.st)}${esc(proc)}"></i>`;
        }).join('');
        const noun = g.count === 1 ? 'файл' : 'файлов';
        return `<div class="bm-group"><div class="bm-head">${esc(g.record_type)} · ${esc(g.camera)} — ${g.count} ${noun}</div><div class="bm-grid">${blocks}</div></div>`;
      }).join('') || '<div class="bm-empty">нет файлов</div>';
    }
    function applyProcessingHighlights(processing) {
      const clipSet = new Set((processing?.clips || []).map(c => `${c[0]}/${c[1]}/${c[2]}`));
      const hostSet = new Set(processing?.host || []);
      const hostLabel = processing?.host_label || 'сейчас в работе';
      document.querySelectorAll('#bm-groups .bm-grid i[data-n]').forEach(el => {
        const key = `${el.dataset.rt}/${el.dataset.cam}/${el.dataset.n}`;
        const on = clipSet.has(key);
        el.classList.toggle('processing', on);
      });
      document.querySelectorAll('#bm-host-groups .bm-grid i[data-n], #bm-compose-groups .bm-grid i[data-n]').forEach(el => {
        const on = hostSet.has(el.dataset.n);
        el.classList.toggle('processing', on);
        if (on) {
          const base = el.title.replace(/ · (сейчас в работе|copy\/merge|compose|upload)$/, '');
          el.title = base + ' · ' + hostLabel;
        }
      });
      const hostProc = document.getElementById('bm-host-processing');
      if (hostProc) {
        const n = processing?.host_count || hostSet.size || 0;
        const label = processing?.host_label || '';
        if (n > 0) {
          hostProc.hidden = false;
          hostProc.innerHTML =
            `<div class="k">В работе</div><div class="v">${esc(String(n))}` +
            (label ? `<span class="dim"> · ${esc(label)}</span>` : '') +
            `</div>`;
        } else {
          hostProc.hidden = true;
          hostProc.innerHTML = '';
        }
      }
      const hostProcLegend = document.getElementById('bm-host-proc-legend');
      if (hostProcLegend) {
        const n = processing?.host_count || hostSet.size || 0;
        const label = processing?.host_label || 'в работе';
        hostProcLegend.innerHTML = n
          ? `<i class="bm-swatch"></i>${esc(label)} ${n}`
          : `<i class="bm-swatch"></i>в работе`;
      }
    }
    function renderFileMap(fm) {
      const legend = document.getElementById('bm-legend');
      const groups = document.getElementById('bm-groups');
      const hostStats = document.getElementById('bm-host-stats');
      const hostLegend = document.getElementById('bm-host-legend');
      const hostGroups = document.getElementById('bm-host-groups');
      const composeLegend = document.getElementById('bm-compose-legend');
      const composeGroups = document.getElementById('bm-compose-groups');
      if (!fm || !fm.present) {
        if (legend) legend.innerHTML = '';
        if (groups) groups.innerHTML = '<div class="bm-empty">Карта не подключена</div>';
      } else {
        const counts = fm.counts || {};
        if (legend) {
          const items = bmStatusOrder.filter(k => counts[k]).map(k =>
            `<span class="st-${esc(k)}"><i class="bm-swatch"></i>${esc(bmStatusLabels[k] || k)} ${counts[k]}</span>`
          );
          items.push('<span class="st-processing"><i class="bm-swatch"></i>в работе</span>');
          legend.innerHTML = items.join('');
        }
        if (groups) renderBlockGroups(groups, fm.groups);
      }
      if (!hostStats || !hostLegend || !hostGroups) return;
      const host = (fm && fm.host) || {};
      const copied = host.copied || {};
      const merged = host.merged || {};
      const compose = host.compose || {};
      const youtube = host.youtube || {};
      const processing = host.processing || {};
      const hasHost = !!(host.present || merged.files || compose.files || youtube.total || copied.total);
      if (!hasHost) {
        hostStats.innerHTML = '';
        hostLegend.innerHTML = '';
        hostGroups.innerHTML = '<div class="bm-empty">Нет данных на хосте</div>';
        if (composeLegend) composeLegend.innerHTML = '';
        if (composeGroups) composeGroups.innerHTML = '<div class="bm-empty">нет compose MP4</div>';
        return;
      }
      const procCount = processing.count || 0;
      const procLabel = processing.label || '';
      hostStats.innerHTML = [
        `<div class="host-stat"><div class="k">Скопировано с флешки</div><div class="v">${esc(String(copied.done || 0))}<span class="dim"> / ${esc(String(copied.total || 0))} клип.</span></div></div>`,
        `<div class="host-stat"><div class="k">Смержено на диске</div><div class="v">${esc(String(merged.files || 0))}<span class="dim"> файл. · ${esc(String(merged.clips || 0))} клип.</span></div></div>`,
        `<div class="host-stat"><div class="k">Compose MP4</div><div class="v">${esc(String(compose.files || 0))}<span class="dim"> · ${esc(compose.size || '—')}</span></div></div>`,
        `<div class="host-stat"><div class="k">YouTube</div><div class="v">${esc(String(youtube.done || 0))}<span class="dim"> / ${esc(String(youtube.total || 0))} рол.</span></div></div>`,
        `<div class="host-stat" id="bm-host-processing"${procCount ? '' : ' hidden'}>` +
          (procCount
            ? `<div class="k">В работе</div><div class="v">${esc(String(procCount))}` +
              (procLabel ? `<span class="dim"> · ${esc(procLabel)}</span>` : '') +
              `</div>`
            : '') +
        `</div>`,
      ].join('');
      const hostCounts = {};
      (host.groups || []).forEach(g => (g.blocks || []).forEach(b => {
        hostCounts[b.st] = (hostCounts[b.st] || 0) + 1;
      }));
      const hostLegendItems = bmStatusOrder.filter(k => hostCounts[k]).map(k =>
        `<span class="st-${esc(k)}"><i class="bm-swatch"></i>${esc(bmStatusLabels[k] || k)} ${hostCounts[k]}</span>`
      );
      const procLegend = procCount
        ? `<span class="st-processing" id="bm-host-proc-legend"><i class="bm-swatch"></i>${esc(procLabel || 'в работе')} ${procCount}</span>`
        : `<span class="st-processing" id="bm-host-proc-legend"><i class="bm-swatch"></i>в работе</span>`;
      hostLegendItems.push(procLegend);
      hostLegend.innerHTML = hostLegendItems.join('');
      renderBlockGroups(hostGroups, host.groups);

      const cGroups = host.compose_groups || [];
      const composeCounts = {};
      cGroups.forEach(g => (g.blocks || []).forEach(b => {
        composeCounts[b.st] = (composeCounts[b.st] || 0) + 1;
      }));
      if (composeLegend) {
        const items = bmStatusOrder.filter(k => composeCounts[k]).map(k =>
          `<span class="st-${esc(k)}"><i class="bm-swatch"></i>${esc(bmStatusLabels[k] || k)} ${composeCounts[k]}</span>`
        );
        items.push('<span class="st-processing"><i class="bm-swatch"></i>в работе / upload</span>');
        composeLegend.innerHTML = items.join('') || '<span class="bm-empty">нет файлов</span>';
      }
      if (composeGroups) {
        if (cGroups.length) {
          // Relabel camera for display: "compose" → "2cam MP4"
          renderBlockGroups(composeGroups, cGroups.map(g => ({
            ...g,
            camera: '2cam MP4',
          })));
        } else {
          composeGroups.innerHTML = '<div class="bm-empty">нет compose / upload MP4</div>';
        }
      }
    }
    function render(data) {
      const run = data.run || {};
      const phase = run.phase || 'running';
      document.getElementById('subtitle').textContent =
        (phaseLabels[phase] || phase) + (run.message ? ' — ' + run.message : '');
      renderPipeline(data.pipeline);
      applyProcessingHighlights(data.processing);
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
        const g = document.getElementById('bm-groups');
        if (g) g.innerHTML = '<div class="bm-empty">Нет данных карты файлов</div>';
        const hg = document.getElementById('bm-host-groups');
        if (hg) hg.innerHTML = '<div class="bm-empty">Нет данных карты файлов</div>';
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

        _client_gone = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def handle(self) -> None:
                # Browser cancel / tab close mid-response is normal for polling.
                try:
                    super().handle()
                except _client_gone:
                    pass

            def _write_body(self, body: bytes) -> None:
                try:
                    self.wfile.write(body)
                except _client_gone:
                    pass

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self._write_body(body)
                except _client_gone:
                    pass

            def do_GET(self) -> None:
                aw = _reload_dashboard_module()
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    body = aw._dashboard_html_bytes()
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self._write_body(body)
                    except _client_gone:
                        pass
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
                try:
                    self.send_error(404)
                except _client_gone:
                    pass

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path != "/api/control":
                    try:
                        self.send_error(404)
                    except _client_gone:
                        pass
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                try:
                    raw = self.rfile.read(length) if length else b"{}"
                except _client_gone:
                    return
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._json(400, {"ok": False, "message": "invalid JSON"})
                    return
                action = str(data.get("action") or "")
                message = outer._on_control(action, data)
                self._json(200, {"ok": True, "message": message})

        class _DashboardHTTPServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                exc = sys.exc_info()[1]
                if isinstance(exc, _client_gone):
                    return
                super().handle_error(request, client_address)

        self._httpd = _DashboardHTTPServer((self._host, self._port), Handler)
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
