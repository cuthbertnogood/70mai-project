"""Hot-reloadable dashboard screen (text layout).

Edit this file while `./scripts/autopilot_dashboard.sh` is running — the main
process watches mtime and reloads/restarts so you do not need Ctrl+C.
"""
from __future__ import annotations

import shutil
from typing import Any


def render(dash: Any) -> None:
    """Paint one dashboard frame to dash._tty."""
    import autopilot_dashboard as d

    if not dash.enabled or dash._tty is None:
        return

    chunks_done, chunk_total, trips_done = d.chunk_summary_counts(dash.rows)
    done = trips_done
    fail = sum(1 for r in dash.rows if r.status == "fail")
    total = len(dash.rows)
    free = d.free_disk_gb(dash.check_disk)
    from publish_all_70mai import autopilot_disk_usage, format_gb

    usage = autopilot_disk_usage(dash.video_dir, dash.temp_dir)
    active_rows = [
        r
        for r in dash.rows
        if r.status in ("compose", "upload", "import", "stall", "oauth")
    ]
    if dash._youtube_ok is True:
        yt_net = "YT net OK"
    elif dash._youtube_ok is False:
        yt_net = f"YT net OFF ({dash._youtube_detail})"
    else:
        yt_net = "YT net …"
    if dash._upload_health == "error":
        yt_upload = f"UPLOAD ERR: {dash._upload_health_detail}"
    elif dash._upload_health == "retry":
        yt_upload = f"upload retry: {dash._upload_health_detail}"
    elif dash._upload_health == "upload":
        yt_upload = "upload…"
    elif dash._upload_health == "ok":
        yt_upload = f"upload OK {dash._upload_health_detail}"
    else:
        yt_upload = "upload —"
    try:
        term_cols = shutil.get_terminal_size().columns
        term_rows = shutil.get_terminal_size().lines
    except OSError:
        term_cols = 100
        term_rows = 40
    two_col = d._use_two_col_trips(term_cols)
    trip_cols = 2 if two_col else 1
    trip_col_w = d._trip_list_col_width(term_cols)

    st = d.resolve_live_status(dash.temp_dir, rows=dash.rows)
    procs = d.list_pipeline_processes(temp_dir=dash.temp_dir)
    prefetch = d.resolve_prefetch_import(dash.temp_dir, procs)
    # Age alone: old status.json must not drive ► markers (procs may be zombies).
    stale = d._status_is_stale(st)
    import_alive = any(p.role in ("import", "prefetch") for p in procs)
    log_fallback = None
    if import_alive:
        log_fallback = d._import_progress_from_log(
            dash.temp_dir, video_dir=dash.video_dir
        )
    if stale:
        active_rows = []
    active_key = None if stale else d._status_active_key(dash.rows, st)

    summary = f"YouTube {chunks_done}/{chunk_total}"
    if chunk_total and chunks_done < chunk_total:
        summary += f" ({done}/{total} поездок)"
    if fail:
        summary += f"  fail:{fail}"
    pending_chunks = max(0, chunk_total - chunks_done)
    if pending_chunks > 0:
        summary += f"  todo:{pending_chunks}р"
    if active_rows:
        parts = []
        seen_active_chunks: set[tuple[str, int]] = set()
        for ar in active_rows:
            ck = (ar.record_type, ar.chunk_index)
            if ck in seen_active_chunks:
                continue
            seen_active_chunks.add(ck)
            pct = ar.percent
            if ar.status == "upload" and pct is None:
                pct = d._read_upload_percent(
                    dash.temp_dir,
                    ar.record_type,
                    ar.chunk_index,
                    ar.trip_index,
                )
            stage = d._stage_label(
                ar.status,
                percent=pct,
                stalled=ar.stalled,
                detail=(
                    d._import_row_progress(
                        dash.temp_dir, record_type=ar.record_type
                    )
                    if ar.status == "import"
                    else ""
                ),
            )
            roll = d.chunk_display_num(ar)
            parts.append(f"{stage} {roll} {d._trip_display(ar)}")
            if len(parts) >= 2:
                break
        summary += "  |  " + " · ".join(parts)
    elif prefetch and log_fallback:
        summary += "  |  " + d.format_prefetch_stage(prefetch, log_fallback)
    elif prefetch:
        summary += "  |  " + d.format_prefetch_stage(prefetch)
    elif log_fallback:
        bits = []
        if log_fallback.get("copy"):
            bits.append(f"copy {log_fallback['copy']}")
        if log_fallback.get("copy_detail"):
            bits.append(log_fallback["copy_detail"])
        if log_fallback.get("merge"):
            bits.append(f"merge {log_fallback['merge']}")
        if log_fallback.get("merge_detail"):
            bits.append(log_fallback["merge_detail"])
        summary += "  |  " + (" · ".join(bits) if bits else "import …")
    elif stale:
        summary += "  |  idle"
    elif st and st.get("phase") == "import":
        pct = st.get("percent")
        detail = str(st.get("detail") or "").strip()
        stage = "import"
        if isinstance(pct, (int, float)):
            stage = f"import {float(pct):.0f}%"
        summary += f"  |  {stage}"
        if detail:
            summary += f" {detail[:40]}"
    else:
        summary += "  |  wait"

    disk_line = (
        f"disk {free:.0f}G free (min {dash.min_free_gb:.0f})  ·  "
        f"video {format_gb(usage['total'])}  ·  {yt_net}  ·  {yt_upload}"
    )

    lines: list[str] = []
    for hl in d._wrap_line(summary, term_cols):
        lines.append(hl)
    for cl in d._format_pipeline_block(
        st,
        dash.rows,
        temp_dir=dash.temp_dir,
        video_dir=dash.video_dir,
        stale=stale,
        log_fallback=log_fallback,
        import_alive=import_alive,
        procs=procs,
        prefetch=prefetch,
    ):
        for hl in d._wrap_line(cl, term_cols):
            lines.append(hl)
    meta_bits: list[str] = []
    age = d._status_age_line(st)
    if age:
        meta_bits.append(age)
    meta_bits.extend(d._format_pipeline_processes(procs, prefetch=prefetch))
    for hl in d._wrap_line("  ·  ".join(meta_bits), term_cols):
        lines.append(hl)
    for hl in d._wrap_line(disk_line, term_cols):
        lines.append(hl)
    if dash.source is not None:
        try:
            from card_identity import format_sd_clip_summary

            sd_line = format_sd_clip_summary(dash.source)
            if sd_line:
                for hl in d._wrap_line(sd_line, term_cols):
                    lines.append(hl)
        except OSError:
            pass

    show_rows, collapse_note = d._visible_rows(
        dash.rows, term_rows=term_rows, total=total, columns=trip_cols
    )
    if collapse_note:
        lines.append(collapse_note)

    trip_lines: list[str] = []
    for i, row in enumerate(show_rows, start=1):
        size_b = d._row_compose_bytes(
            dash.temp_dir, row, active_key=active_key
        )
        is_prefetch_row = (
            prefetch is not None
            and prefetch.chunk_index > 0
            and row.record_type == prefetch.record_type
            and row.chunk_index == prefetch.chunk_index
            and row.status == "pending"
        )
        is_active = (not stale) and row.status in (
            "compose",
            "upload",
            "import",
            "stall",
        )
        if is_prefetch_row:
            marker = "►"
            stage = d.format_prefetch_stage(prefetch, log_fallback)
        else:
            marker = "►" if is_active else " "
            stage = row.progress if row.progress != "—" else d._stage_label(row.status)
            if (not stale) and row.status == "import":
                stage = d._import_row_progress(
                    dash.temp_dir, record_type=row.record_type
                )
            if stale and row.status in ("compose", "upload", "import", "stall"):
                stage = "ожидание"
        num = d.chunk_display_num(row)
        trip_lines.append(
            d._trip_compact_line(
                marker=marker,
                num=num,
                trip=d._trip_display(row),
                dur=d._fmt_dur_short(row.duration_sec),
                stage=stage,
                size=d._fmt_gb(size_b),
                youtube=d._youtube_for_column(row.youtube_url, 11),
                width=trip_col_w,
            )
        )
    lines.extend(d._two_column_pack(trip_lines, term_cols=term_cols))
    lines.extend(d.format_local_files_block(dash.rows, term_cols=term_cols))
    lines.extend(d.format_pipeline_legend(term_cols=term_cols))
    for leg in d._STATUS_LEGEND:
        lines.extend(d._wrap_line(leg, term_cols))
    for row in dash.rows:
        if row.status in ("fail", "stall", "oauth") and row.reason not in ("—", ""):
            num = d.chunk_display_num(row)
            lines.extend(
                d._wrap_line(
                    f"⚠ {num} {d._trip_display(row)}: "
                    f"{d._short_reason(row.reason)}",
                    term_cols,
                )
            )
    lines.extend(d.format_parking_merge_hint(
        dash.temp_dir,
        term_cols=term_cols,
        import_alive=import_alive,
        video_dir=dash.video_dir,
    ))
    lines.extend(d.format_failures_block(
        dash.temp_dir,
        term_cols=term_cols,
        source=getattr(dash, "source", None),
    ))
    block = "\n".join(lines)
    out = dash._tty
    if dash._alt_screen:
        out.write("\033[H\033[J")
    elif dash._lines:
        out.write(f"\033[{dash._lines}A\033[J")
    out.write(block)
    if not block.endswith("\n"):
        out.write("\n")
    out.flush()
    dash._lines = len(lines)

