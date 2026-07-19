#!/usr/bin/env python3
"""Live TTY dashboard for autopilot trip progress (Status / Progress / Disk / YouTube)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

STATUS_FILENAME = "autopilot_status.json"
REASONS_FILENAME = "autopilot_trip_reasons.json"
LIVE_STATUS_PHASES = frozenset(
    {"compose", "upload", "stall", "import", "oauth"}
)


def trip_key(record_type: str, chunk_index: int, trip_index: int) -> str:
    return f"{record_type}:{chunk_index}:{trip_index}"


def reasons_path(temp_dir: Path) -> Path:
    return temp_dir / REASONS_FILENAME


def read_trip_reasons(temp_dir: Path) -> dict[str, dict]:
    path = reasons_path(temp_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_trip_reason(
    temp_dir: Path,
    *,
    record_type: str,
    chunk_index: int,
    trip_index: int,
    reason: str,
    phase: str = "",
) -> None:
    """Persist last stop/fail reason for a trip (shown in dashboard Reason column)."""
    if not reason:
        return
    key = trip_key(record_type, chunk_index, trip_index)
    reasons = read_trip_reasons(temp_dir)
    reasons[key] = {
        "reason": reason[:120],
        "phase": phase,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = reasons_path(temp_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reasons, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def clear_trip_reason(
    temp_dir: Path,
    *,
    record_type: str,
    chunk_index: int,
    trip_index: int,
) -> None:
    key = trip_key(record_type, chunk_index, trip_index)
    reasons = read_trip_reasons(temp_dir)
    if key not in reasons:
        return
    del reasons[key]
    try:
        path = reasons_path(temp_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reasons, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def status_path(temp_dir: Path) -> Path:
    return temp_dir / STATUS_FILENAME


def write_status(
    temp_dir: Path,
    *,
    record_type: str,
    chunk_index: int,
    trip_index: int,
    phase: str,
    detail: str = "",
    youtube_url: str | None = None,
    percent: float | None = None,
    output_bytes: int | None = None,
    stalled: bool = False,
    reason: str = "",
    card_id: str | None = None,
    session_start: str | None = None,
    conveyors: dict | None = None,
    stage_ahead: str | None = None,
    speed: float | None = None,
    speed_unit: str | None = None,
    eta: str | None = None,
    elapsed: str | None = None,
) -> None:
    """Atomic status update for the live dashboard (safe across processes)."""
    path = status_path(temp_dir)
    data = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "record_type": record_type,
        "chunk_index": chunk_index,
        "trip_index": trip_index,
        "phase": phase,
        "detail": detail,
        "youtube_url": youtube_url,
        "percent": percent,
        "output_bytes": output_bytes,
        "stalled": stalled,
        "reason": reason[:120] if reason else "",
    }
    if speed is not None:
        data["speed"] = speed
    if speed_unit:
        data["speed_unit"] = speed_unit
    if eta:
        data["eta"] = eta
    if elapsed:
        data["elapsed"] = elapsed
    if card_id:
        data["card_id"] = card_id
    if session_start:
        data["session_start"] = session_start
    if conveyors:
        data["conveyors"] = conveyors
    if stage_ahead:
        data["stage_ahead"] = stage_ahead
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        if reason:
            write_trip_reason(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk_index,
                trip_index=trip_index,
                reason=reason,
                phase=phase,
            )
    except OSError:
        pass


def write_import_status(
    temp_dir: Path,
    *,
    record_type: str,
    percent: float | None = None,
    detail: str = "",
    session_start: datetime | None = None,
    conveyors: dict | None = None,
    stage_ahead: str | None = None,
) -> None:
    """Dashboard progress while import_70mai merge is running (no trip indices yet)."""
    write_status(
        temp_dir,
        record_type=record_type,
        chunk_index=0,
        trip_index=0,
        phase="import",
        detail=detail,
        percent=percent,
        session_start=(
            session_start.isoformat(timespec="seconds") if session_start else None
        ),
        conveyors=conveyors,
        stage_ahead=stage_ahead,
    )


_MERGED_TS_RE = re.compile(
    r"(?:NO|EV|PA)_(\d{8})-(\d{6})_",
    re.IGNORECASE,
)


def _parse_import_session_start(st: dict) -> datetime | None:
    raw = st.get("session_start")
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            pass
    detail = str(st.get("detail") or "")
    match = _MERGED_TS_RE.search(detail)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _find_import_row(rows: list[TripRow], st: dict) -> TripRow | None:
    """Map import merge status onto a plan trip (by type + clip timestamp)."""
    record_type = str(st.get("record_type") or "")
    candidates = [r for r in rows if r.record_type == record_type and r.status != "done"]
    if not candidates:
        candidates = [r for r in rows if r.record_type == record_type]
    if not candidates:
        return None
    if record_type in ("Event", "Parking"):
        return candidates[0]
    ts = _parse_import_session_start(st)
    if ts is None:
        return candidates[0]
    for row in candidates:
        if row.trip_start is None or row.trip_end is None:
            continue
        if row.trip_start <= ts <= row.trip_end:
            return row
    return candidates[0]


def _status_active_key(rows: list[TripRow], st: dict | None) -> str | None:
    if not st:
        return None
    phase = str(st.get("phase") or "")
    if phase == "import":
        matched = _find_import_row(rows, st)
        return matched.key if matched else None
    return trip_key(
        st.get("record_type", ""),
        int(st.get("chunk_index") or 0),
        int(st.get("trip_index") or 0),
    )


def read_status(temp_dir: Path) -> dict | None:
    path = status_path(temp_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_MERGE_HB_RE = re.compile(
    r"… merging ((?:NO|EV|PA)_\S+\.mp4)"
    r"(?: batch (\d+)/(\d+))? \(([^)]+)\)",
)
_MERGE_BAR_RE = re.compile(
    r"Merge \[.*?\] (\d+)/(\d+) \(([\d.]+)%\)"
)
_MERGE_ARROW_RE = re.compile(r"→ ((?:NO|EV|PA)_\S+\.mp4)")
_PREFIX_TO_TYPE = {"NO": "Normal", "EV": "Event", "PA": "Parking"}


def _tail_text(path: Path, max_bytes: int = 96_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_import_progress_from_log(
    temp_dir: Path, *, max_age_sec: float = 120.0
) -> dict | None:
    """Infer live import merge progress from publish_all.log heartbeats."""
    text = _tail_text(temp_dir / "publish_all.log")
    if not text:
        return None
    last_hb: tuple[datetime, str, str, int | None, int | None] | None = None
    last_bar: tuple[datetime, int, int, float] | None = None
    last_arrow: tuple[datetime, str] | None = None
    for line in text.splitlines():
        match_ts = _LOG_TS_RE.match(line)
        if not match_ts:
            continue
        try:
            ts = datetime.strptime(match_ts.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        match = _MERGE_HB_RE.search(line)
        if match:
            last_hb = (
                ts,
                match.group(1),
                match.group(4),
                int(match.group(2)) if match.group(2) else None,
                int(match.group(3)) if match.group(3) else None,
            )
        match = _MERGE_BAR_RE.search(line)
        if match:
            last_bar = (
                ts,
                int(match.group(1)),
                int(match.group(2)),
                float(match.group(3)),
            )
        match = _MERGE_ARROW_RE.search(line)
        if match:
            last_arrow = (ts, match.group(1))

    filename = ""
    elapsed_note = ""
    latest_ts: datetime | None = None
    if last_hb:
        latest_ts, filename, elapsed_note = last_hb[0], last_hb[1], last_hb[2]
    elif last_arrow:
        latest_ts, filename = last_arrow
    elif last_bar:
        latest_ts = last_bar[0]
    else:
        return None
    if (datetime.now() - latest_ts).total_seconds() > max_age_sec:
        return None

    detail_parts: list[str] = []
    percent: float | None = None
    if last_bar:
        percent = last_bar[3]
        detail_parts.append(f"{last_bar[1]}/{last_bar[2]}")
    if filename:
        detail_parts.append(filename)
    if elapsed_note:
        detail_parts.append(elapsed_note)

    prefix = filename[:2].upper() if filename else "NO"
    record_type = _PREFIX_TO_TYPE.get(prefix, "Normal")
    session_start = None
    match = _MERGED_TS_RE.search(filename) if filename else None
    if match:
        try:
            session_start = datetime.strptime(
                match.group(1) + match.group(2), "%Y%m%d%H%M%S"
            ).isoformat(timespec="seconds")
        except ValueError:
            session_start = None

    return {
        "ts": latest_ts.isoformat(timespec="seconds"),
        "record_type": record_type,
        "chunk_index": 0,
        "trip_index": 0,
        "phase": "import",
        "detail": " · ".join(detail_parts),
        "youtube_url": None,
        "percent": percent,
        "output_bytes": None,
        "stalled": False,
        "reason": "",
        "session_start": session_start,
    }


def resolve_live_status(temp_dir: Path) -> dict | None:
    """Prefer compose/upload status file; fall back to import heartbeats in the log."""
    st = read_status(temp_dir)
    phase = str((st or {}).get("phase") or "")
    if phase in ("compose", "upload", "stall", "oauth"):
        return st
    log_st = read_import_progress_from_log(temp_dir)
    if not log_st:
        return st
    if not st or phase != "import":
        return log_st
    try:
        st_ts = datetime.fromisoformat(str(st.get("ts") or ""))
        log_ts = datetime.fromisoformat(str(log_st.get("ts") or ""))
    except ValueError:
        return log_st
    return log_st if log_ts >= st_ts else st


def free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except OSError:
        return 0.0


def _merged_bytes_for_trip(video_dir: Path, record_type: str, start, end) -> int:
    from compose_70mai import scan_merged_clips
    from publish_70mai import MERGED_PRUNE_MARGIN

    lo = start - MERGED_PRUNE_MARGIN
    hi = end + MERGED_PRUNE_MARGIN
    total = 0
    for camera in ("Front", "Back"):
        try:
            clips = scan_merged_clips(
                video_dir, camera, record_type=record_type, probe=False
            )
        except OSError:
            continue
        for clip in clips:
            if clip.start >= lo and clip.end <= hi:
                try:
                    total += clip.path.stat().st_size
                except OSError:
                    pass
    return total


def _compose_trip_path(temp_dir: Path, chunk_index: int, trip_index: int) -> Path:
    return temp_dir / f"chunk_{chunk_index:02d}" / f"trip_{trip_index:02d}.mp4"


def _rel_path(path: Path, base: Path | None = None) -> str:
    base = (base or Path.cwd()).resolve()
    try:
        return str(path.resolve().relative_to(base))
    except ValueError:
        return str(path)


def _resolve_local_path(
    *,
    temp_dir: Path,
    video_dir: Path,
    record_type: str,
    chunk_index: int,
    trip_index: int,
    status: str,
    composed_bytes: int,
    merged_bytes: int,
    base: Path | None = None,
) -> str:
    """Best local path for this trip row (composed MP4 or merged source dirs)."""
    out = _compose_trip_path(temp_dir, chunk_index, trip_index)
    if out.is_file():
        return _rel_path(out, base)
    if status in ("compose", "upload", "stall") or composed_bytes > 0:
        return _rel_path(out, base)
    if merged_bytes > 0:
        root = _rel_path(video_dir / record_type, base)
        return f"{root}/Front+Back"
    if status == "done" and not out.is_file():
        return "—"
    return _rel_path(out, base)


def _compose_bytes(temp_dir: Path, chunk_index: int, trip_index: int) -> int:
    path = _compose_trip_path(temp_dir, chunk_index, trip_index)
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _row_compose_bytes(
    temp_dir: Path,
    row: TripRow,
    *,
    active_key: str | None = None,
) -> int:
    """Composed MP4 size for this dashboard row (not a reused temp path)."""
    if row.status == "done":
        return 0
    path = _compose_trip_path(temp_dir, row.chunk_index, row.trip_index)
    if not path.is_file():
        return 0
    # Normal and Event both use chunk_01/trip_01.mp4 — attribute file to active row only.
    if active_key and row.key != active_key:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _fmt_gb(n: int) -> str:
    if n <= 0:
        return "—"
    gb = n / (1024**3)
    if gb >= 1:
        return f"{gb:.1f}G"
    return f"{n / (1024**2):.0f}M"


# Six columns — compact defaults (do not inflate trip column to fill the terminal).
_COL_HEADERS = ("№", "Поездка", "Длит", "Этап", "Размер", "YouTube")

_STATUS_LEGEND = (
    "► активно  ·  ✓ готово  ·  · ждёт",
)

# status.json older than this → idle (no ghost ►), even if procs still listed.
_STALE_STATUS_SEC = 300


def _table_width(widths: tuple[int, ...]) -> int:
    """Rendered row width including border chars between columns."""
    n = len(widths)
    if n == 0:
        return 0
    return 2 + (n - 1) + sum(w + 2 for w in widths)


def _column_widths(term_cols: int) -> tuple[int, ...]:
    """Fit 6-column table into terminal; prefer Этап over empty Поездка padding."""
    term_cols = max(56, term_cols)
    # №, trip, dur, stage, size, yt
    widths = [6, 14, 8, 22, 6, 12]
    floors = [5, 10, 6, 14, 4, 8]
    caps = [7, 16, 9, 40, 7, 14]

    def fits(w: list[int]) -> bool:
        return _table_width(tuple(w)) <= term_cols

    while not fits(widths):
        shrunk = False
        for idx in (1, 5, 3, 2, 4, 0):
            if fits(widths):
                break
            if widths[idx] > floors[idx]:
                widths[idx] -= 1
                shrunk = True
        if not shrunk:
            break

    # Extra space → Этап (progress), then YouTube id — never balloon trip date.
    for idx in (3, 5, 2, 4):
        while widths[idx] < caps[idx] and fits(
            [*widths[:idx], widths[idx] + 1, *widths[idx + 1 :]]
        ):
            widths[idx] += 1

    return tuple(widths)


def _use_two_col_trips(term_cols: int) -> bool:
    """Two side-by-side trip columns when the terminal is wide enough."""
    return term_cols >= 88


def _fmt_dur_short(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, _secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{_secs}s"


def _trip_list_col_width(term_cols: int) -> int:
    """Inner cell width for the framed trip list (1 or 2 columns)."""
    if _use_two_col_trips(term_cols):
        col_w = max(28, (term_cols - 7) // 2)
        while col_w > 20 and _table_width((col_w, col_w)) > term_cols:
            col_w -= 1
        return col_w
    col_w = max(40, term_cols - 4)
    while col_w > 20 and _table_width((col_w,)) > term_cols:
        col_w -= 1
    return col_w


def _trip_compact_line(
    *,
    marker: str,
    num: str,
    trip: str,
    dur: str,
    stage: str,
    size: str,
    youtube: str,
    width: int,
) -> str:
    """One dense trip line for the two-column list."""
    stage_s = stage.replace("↑ YouTube ", "↑").replace("сборка F+B ", "F+B ")
    bits = [f"{marker}{num}", trip, dur, stage_s, size]
    if youtube and youtube not in ("—", "-"):
        bits.append(youtube)
    return _fit_text(" ".join(bits), width)


def _two_column_pack(items: list[str], *, term_cols: int, gap: str = " │ ") -> list[str]:
    """Pack trip lines into a framed 1- or 2-column box (┏━┳━┓ / ┃ ┃ / ┗━┻━┛)."""
    del gap  # separator is the table middle border
    if not items:
        return []
    col_w = _trip_list_col_width(term_cols)
    if _use_two_col_trips(term_cols):
        widths = (col_w, col_w)
        mid = (len(items) + 1) // 2
        left, right = items[:mid], items[mid:]
        out = [_table_top(widths)]
        for i in range(mid):
            l = left[i] if i < len(left) else ""
            r = right[i] if i < len(right) else ""
            out.append(_table_row((l, r), widths))
        out.append(_table_bottom(widths))
        return out
    widths = (col_w,)
    out = [_table_top(widths)]
    for item in items:
        out.append(_table_row((item,), widths))
    out.append(_table_bottom(widths))
    return out


def _wrap_line(text: str, width: int) -> list[str]:
    if width <= 0 or len(text) <= width:
        return [text]
    lines: list[str] = []
    rest = text
    while rest:
        if len(rest) <= width:
            lines.append(rest)
            break
        cut = rest.rfind(" | ", 0, width + 1)
        if cut < width // 3:
            cut = rest.rfind(" · ", 0, width + 1)
        if cut < width // 3:
            cut = width
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip(" |·")
    return lines


def _fit_text(text: str, width: int, *, tail: bool = False) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"
    if tail:
        return "…" + text[-(width - 1) :]
    return text[: max(0, width - 1)] + "…"


def _path_display(path: str) -> str:
    if path in ("—", "-", ""):
        return "—"
    if ".publish_tmp/" in path:
        return path.split(".publish_tmp/", 1)[1]
    for rec in ("Normal", "Event", "Parking"):
        needle = f"{rec}/"
        if needle in path:
            return path[path.index(needle) :]
    return path


def _path_for_column(path: str, width: int) -> str:
    if path in ("—", "-", ""):
        return "—"
    if len(path) <= width:
        return path
    compact = _path_display(path)
    if len(compact) <= width:
        return compact
    return _fit_text(compact, width, tail=True)


def _local_path_priority(row: TripRow) -> tuple[int, int]:
    phase_rank = {
        "upload": 0,
        "compose": 1,
        "stall": 2,
        "import": 3,
        "pending": 4,
        "fail": 5,
        "oauth": 6,
    }
    return (phase_rank.get(row.status, 50), row.overall_index or 0)


def _row_show_local_path(row: TripRow) -> bool:
    path = (row.local_path or "").strip()
    if path in ("—", "-", ""):
        return False
    if row.status == "done" and row.youtube_url:
        return False
    return True


def format_local_files_block(
    rows: list[TripRow],
    *,
    term_cols: int,
    limit: int = 8,
) -> list[str]:
    """Paths to composed MP4 / merged sources for trips not yet on YouTube."""
    candidates = sorted(
        (r for r in rows if _row_show_local_path(r)),
        key=_local_path_priority,
    )
    picked: list[TripRow] = []
    seen_paths: set[str] = set()
    for row in candidates:
        path = (row.local_path or "").strip()
        if path in ("—", "-", ""):
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        picked.append(row)
        if len(picked) >= limit:
            break
    if not picked:
        return []
    total = len(rows)
    out: list[str] = []
    out.extend(
        _wrap_line(
            "── Локальные файлы ──  open путь  (до загрузки на YouTube)",
            term_cols,
        )
    )
    for row in picked:
        num = row.overall_index or 0
        trip = _trip_display(row)
        path = row.local_path
        line = f"{num}/{total} {trip}  {path}"
        out.extend(_wrap_line(line, term_cols))
    hidden = len(candidates) - len(picked)
    if hidden > 0:
        out.extend(_wrap_line(f"… ещё {hidden} поездок с тем же путём", term_cols))
    return out


def _youtube_display(url: str | None) -> str:
    if not url:
        return "—"
    u = url.strip()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix) :]
    if u.startswith("youtu.be/"):
        vid = u.split("youtu.be/", 1)[1].split("?")[0].split("&")[0]
        return f"youtu.be/{vid}"
    if "watch?v=" in u:
        vid = u.split("watch?v=", 1)[1].split("&")[0]
        return f"youtu.be/{vid}"
    return u


def _youtube_for_column(url: str | None, width: int) -> str:
    disp = _youtube_display(url)
    if width < 19 and disp.startswith("youtu.be/"):
        vid = disp.split("/", 1)[1]
        if len(vid) <= width:
            return vid
        return _fit_text(vid, width, tail=True)
    if len(disp) <= width:
        return disp
    return _fit_text(disp, width, tail=True)


def _table_top(widths: tuple[int, ...]) -> str:
    return "┏" + "┳".join("━" * (w + 2) for w in widths) + "┓"


def _table_sep(widths: tuple[int, ...]) -> str:
    return "┣" + "┳".join("━" * (w + 2) for w in widths) + "┫"


def _table_bottom(widths: tuple[int, ...]) -> str:
    return "┗" + "┻".join("━" * (w + 2) for w in widths) + "┛"


def _table_row(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    padded = []
    for cell, w in zip(cells, widths):
        text = cell if len(cell) <= w else _fit_text(cell, w)
        padded.append(f" {text:<{w}} ")
    return "┃" + "┃".join(padded) + "┃"


def _short_reason(reason: str) -> str:
    """One-line summary for fail/stall footer (hide HTML blobs)."""
    if not reason or reason == "—":
        return "—"
    text = reason.strip()
    if text.startswith("upload:"):
        text = text[len("upload:") :].strip()
    if text.startswith("oauth:") or "invalid_grant" in text.lower():
        return "YouTube OAuth: повторный вход — см. publish_all.log"
    if "Upload chunk failed" in text:
        m = re.search(r"\((\d+)\)", text)
        code = m.group(1) if m else "?"
        return f"загрузка на YouTube: HTTP {code}"
    if text.startswith("ffmpeg"):
        return text[:80]
    return text[:80]


_FAIL_LOG_RE = re.compile(
    r"(?:ERROR:|✗ merge failed|Import failed|Import had merge failure|"
    r"\[repair\] retry|ffprobe validation:|forcing import rebuild)",
    re.I,
)


def _format_fail_ts(ts: str) -> str:
    """Normalize repair/log timestamps to `YYYY-MM-DD HH:MM:SS` (local)."""
    raw = (ts or "").strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        if len(raw) >= 19 and raw[4] == "-" and raw[10] == " " and raw[13] == ":":
            return raw[:19]
        return ""


def _split_log_ts(line: str) -> tuple[str, str]:
    """Return (`YYYY-MM-DD HH:MM:SS` or '', body)."""
    if len(line) > 20 and line[4] == "-" and line[10] == " " and line[13] == ":":
        return line[:19], line[20:].lstrip()
    return "", line


def collect_failure_lines(
    temp_dir: Path, *, limit: int = 6, source: Path | None = None
) -> list[str]:
    """Recent repair + import/merge errors for the dashboard «Сбои» footer."""
    repair_ranked: list[tuple[int, str]] = []
    log_items: list[str] = []
    _code_rank = {
        "bad_clip_skip": -20,
        "merge_short": 0,
        "merge_fb_mismatch": 1,
        "merge_stale": 2,
        "compose_gap": 3,
        "compose_part_stale": 4,
        "state_drift": 5,
        "rebuild_merge": 6,
    }

    try:
        from import_70mai import bad_clips_log_path, sd_bad_clips_log_path

        bad_paths = [bad_clips_log_path(temp_dir)]
        if source is not None:
            try:
                sd_path = sd_bad_clips_log_path(source)
                if sd_path not in bad_paths:
                    bad_paths.append(sd_path)
            except OSError:
                pass
        import json

        for bad_path in bad_paths:
            if not bad_path.is_file():
                continue
            for line in bad_path.read_text(encoding="utf-8").splitlines()[-12:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                name = str(entry.get("name") or "")
                reason = str(entry.get("reason") or "")
                action = str(entry.get("action") or "skip")
                where = "/".join(
                    p
                    for p in (
                        str(entry.get("record_type") or ""),
                        str(entry.get("camera") or ""),
                    )
                    if p
                )
                head = f"bad_clip {action}"
                if where:
                    head += f" {where}"
                if name:
                    head += f": {name}"
                if reason:
                    head += f" ({reason})"
                ts = _format_fail_ts(str(entry.get("ts") or ""))
                text = f"{ts} {head}" if ts else head
                repair_ranked.append((-20, text[:140]))
    except Exception:
        pass

    try:
        from pipeline_repair import read_recent_repairs

        for entry in read_recent_repairs(temp_dir, limit=16):
            action = str(entry.get("action") or "")
            code = str(entry.get("code") or "")
            detail = str(entry.get("detail") or "").strip()
            rec = str(entry.get("record_type") or "")
            cam = str(entry.get("camera") or "")
            where = "/".join(p for p in (rec, cam) if p)
            label = code or action or "repair"
            head = f"repair {label}"
            if where:
                head += f" {where}"
            if detail:
                head += f": {detail}"
            ts = _format_fail_ts(str(entry.get("ts") or ""))
            text = f"{ts} {head}" if ts else head
            text = text[:140]
            rank = _code_rank.get(code, 50)
            if action == "diagnosed":
                rank -= 10
            repair_ranked.append((rank, text))
    except Exception:
        pass

    log_text = _tail_text(temp_dir / "publish_all.log", max_bytes=160_000)
    for raw in log_text.splitlines():
        if not _FAIL_LOG_RE.search(raw):
            continue
        # Skip noisy probe/progress lines that mention "fail" in ETA.
        if "Merge [" in raw and "fail " in raw and "ERROR" not in raw:
            continue
        ts, body = _split_log_ts(raw.strip())
        body = body.strip()
        if not body:
            continue
        text = f"{ts} {body}" if ts else body
        text = text[:140]
        if text in log_items:
            continue
        log_items.append(text)

    repair_ranked.sort(key=lambda x: x[0])
    repair_items: list[str] = []
    for _, text in repair_ranked:
        if text not in repair_items:
            repair_items.append(text)

    half = max(2, limit // 2)
    repairs = repair_items[:half]
    logs = log_items[-(limit - len(repairs)) :]
    out = repairs + logs
    seen: set[str] = set()
    uniq: list[str] = []
    for text in out:
        if text in seen:
            continue
        seen.add(text)
        uniq.append(text)
    return uniq[-limit:]


def format_failures_block(
    temp_dir: Path,
    *,
    term_cols: int,
    limit: int = 5,
    source: Path | None = None,
) -> list[str]:
    """Footer lines: «Сбои» header (with bad-file counter) + recent failures."""
    fails = collect_failure_lines(temp_dir, limit=limit, source=source)
    bad_n = 0
    try:
        from import_70mai import count_bad_clip_records, count_bad_files_on_sd

        on_sd = count_bad_files_on_sd(source)
        in_log = count_bad_clip_records(temp_dir)
        bad_n = max(on_sd, in_log)
    except Exception:
        bad_n = 0
    header = "── Сбои ──"
    if bad_n:
        header += f"  битых файлов: {bad_n}"
    out: list[str] = []
    out.extend(_wrap_line(header, term_cols))
    if not fails:
        out.extend(_wrap_line("нет свежих сбоев", term_cols))
        return out
    for line in fails:
        out.extend(_wrap_line(f"⚠ {line}", term_cols))
    return out


_PART_DUR_CACHE: dict[str, tuple[float, float, float]] = {}  # path -> (mtime, size, dur)


def _probe_part_duration(path: Path, ffprobe: str) -> float | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _PART_DUR_CACHE.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == float(st.st_size):
        return cached[2]
    try:
        from import_70mai import probe_duration_safe

        dur = probe_duration_safe(path, ffprobe)
    except Exception:
        return None
    if dur is None:
        return None
    _PART_DUR_CACHE[key] = (st.st_mtime, float(st.st_size), float(dur))
    return float(dur)


def _sum_parking_part_durations(
    video_dir: Path | None, output_name: str
) -> tuple[float | None, int]:
    """Sum ffprobe durations of `_part_*.mp4` in the merge stage dir."""
    if not video_dir or not output_name:
        return None, 0
    stem = Path(output_name).stem
    try:
        from import_70mai import find_tool

        ffprobe = find_tool("ffprobe")
    except Exception:
        ffprobe = shutil.which("ffprobe") or "ffprobe"
    total = 0.0
    n = 0
    try:
        for stage_root in video_dir.glob("*/*/.merge_stage"):
            part_dir = stage_root / stem
            if not part_dir.is_dir():
                continue
            for part in sorted(part_dir.glob("_part_*.mp4")):
                dur = _probe_part_duration(part, ffprobe)
                if dur is None:
                    continue
                total += dur
                n += 1
            if n:
                return total, n
    except OSError:
        return None, 0
    return (total, n) if n else (None, 0)


def format_parking_merge_hint(
    temp_dir: Path,
    *,
    term_cols: int,
    import_alive: bool = False,
    video_dir: Path | None = None,
) -> list[str]:
    """Live Parking duration: сейчас Xs / цель ~7309s (short был ~6889s)."""
    detail = parse_merge_log_detail(temp_dir)
    cam = str((detail or {}).get("camera") or "")
    output = str((detail or {}).get("output") or "")
    is_parking = "Parking" in cam or output.startswith("PA_")
    if not is_parking and not import_alive:
        st = resolve_live_status(temp_dir)
        if not st or str(st.get("record_type") or "") != "Parking":
            return []
        is_parking = True
    if not is_parking:
        return []

    batch_cur = (detail or {}).get("batch_cur")
    batch_total = (detail or {}).get("batch_total")
    final_n = (detail or {}).get("final_parts")
    done_name = (detail or {}).get("last_done_name") or ""
    done_note = (detail or {}).get("last_done_note") or ""
    session_min = (detail or {}).get("session_min")
    try:
        target_sec = float(session_min) * 60.0 if session_min else 7309.0
    except (TypeError, ValueError):
        target_sec = 7309.0
    short_sec = 6889.0

    from import_70mai import MERGE_SHORT_STRIKE_LIMIT, read_merge_short_strikes

    now_sec, part_n = _sum_parking_part_durations(video_dir, output)
    strikes = 0
    if video_dir and output:
        stem = Path(output).stem
        try:
            for stage_root in video_dir.glob("*/*/.merge_stage"):
                sp = stage_root / stem
                if sp.is_dir():
                    strikes = read_merge_short_strikes(sp)
                    break
        except OSError:
            strikes = 0
    # Prefer live output file after final concat / DONE.
    if video_dir and output:
        out_path = None
        try:
            for cand in video_dir.glob(f"*/*/{output}"):
                if cand.is_file():
                    out_path = cand
                    break
        except OSError:
            out_path = None
        if out_path is not None:
            try:
                from import_70mai import find_tool, probe_duration_safe

                ffprobe = find_tool("ffprobe")
                od = probe_duration_safe(out_path, ffprobe)
            except Exception:
                od = None
            if od is not None:
                now_sec = float(od)

    def _fmt_sec(sec: float) -> str:
        from import_70mai import format_duration

        return f"{format_duration(sec)} ({sec:.0f}s)"

    now_txt = _fmt_sec(now_sec) if now_sec is not None else "…"
    target_txt = _fmt_sec(target_sec)
    pct = ""
    if now_sec is not None and target_sec > 0:
        pct = f" · {100.0 * now_sec / target_sec:.1f}%"

    strike_txt = (
        f" · short-strikes {strikes}/{MERGE_SHORT_STRIKE_LIMIT}"
        if strikes
        else ""
    )

    if batch_cur is not None and batch_total is not None and not final_n:
        tip = (
            f"Parking: part {batch_cur}/{batch_total}"
            f" ({part_n} parts on disk) · сейчас {now_txt} / цель {target_txt}"
            f"{pct}{strike_txt} · short был {_fmt_sec(short_sec)}"
        )
    elif final_n:
        tip = (
            f"Parking: final concat {final_n} parts · сейчас {now_txt} / "
            f"цель {target_txt}{pct}{strike_txt} · short был {_fmt_sec(short_sec)}"
        )
    elif done_name.startswith("PA_") and done_note:
        tip = (
            f"Parking DONE {done_name}: сейчас {now_txt} / цель {target_txt}"
            f"{pct}{strike_txt} · ({done_note})"
        )
    else:
        tip = (
            f"Parking: сейчас {now_txt} / цель {target_txt}{pct}{strike_txt} · "
            f"short был {_fmt_sec(short_sec)}"
        )
    return list(_wrap_line(tip, term_cols))


def _human_etime_seconds(sec: int) -> str:
    sec = max(0, int(sec))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _parse_ps_etime(etime: str) -> int:
    """Parse macOS/Linux ``ps etime`` ([[dd-]hh:]mm:ss) → seconds."""
    etime = etime.strip()
    if not etime:
        return 0
    days = 0
    rest = etime
    if "-" in etime:
        day_s, rest = etime.split("-", 1)
        try:
            days = int(day_s)
        except ValueError:
            days = 0
    parts = rest.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        return 0
    return days * 86400 + h * 3600 + m * 60 + s


_PROC_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("watchdog", re.compile(r"watch_publish_all_70mai", re.I)),
    ("autopilot", re.compile(r"publish_all_70mai\.py", re.I)),
    ("import", re.compile(r"import_70mai\.py", re.I)),
    ("compose", re.compile(r"compose_(?:70mai|2cam_70mai)\.py", re.I)),
    ("publish", re.compile(r"publish_70mai\.py", re.I)),
    ("ffmpeg", re.compile(r"(?:^|[/\s])ffmpeg(?:\s|$)", re.I)),
)


def _classify_proc(cmd: str) -> tuple[str, str] | None:
    """Return (role, short_tip) or None if not a pipeline process."""
    if "autopilot_dashboard" in cmd:
        return None
    if "Cursor Helper" in cmd or "extension-host" in cmd:
        return None
    for role, pat in _PROC_RULES:
        if not pat.search(cmd):
            continue
        m = re.search(
            r"(import_70mai\.py|publish_all_70mai\.py|publish_70mai\.py|"
            r"compose_[^\s]+\.py|watch_publish_all_70mai\.sh|ffmpeg)",
            cmd,
        )
        tip = m.group(1) if m else role
        if role == "ffmpeg":
            if "concat" in cmd:
                tip = "ffmpeg concat"
            elif "-i" in cmd:
                tip = "ffmpeg encode"
        return role, tip
    return None


@dataclass(frozen=True)
class PipelineProc:
    pid: int
    etime_sec: int
    role: str
    tip: str


def list_pipeline_processes() -> list[PipelineProc]:
    """Live OS processes related to autopilot (pid, runtime, role)."""
    try:
        out = subprocess.check_output(
            ["ps", "ax", "-o", "pid=,etime=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    found: list[PipelineProc] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        classified = _classify_proc(m.group(3))
        if not classified:
            continue
        role, tip = classified
        found.append(
            PipelineProc(
                pid=int(m.group(1)),
                etime_sec=_parse_ps_etime(m.group(2)),
                role=role,
                tip=tip,
            )
        )
    order = {
        "autopilot": 0,
        "import": 1,
        "compose": 2,
        "publish": 3,
        "ffmpeg": 4,
        "watchdog": 5,
    }
    found.sort(key=lambda p: (order.get(p.role, 9), -p.etime_sec, p.pid))
    return found


def _format_pipeline_processes(procs: list[PipelineProc], *, limit: int = 6) -> list[str]:
    if not procs:
        return ["proc: —"]
    bits = [
        f"{p.role}:{p.pid}/{_human_etime_seconds(p.etime_sec)}"
        for p in procs[:limit]
    ]
    line = "proc: " + " · ".join(bits)
    if len(procs) > limit:
        line += f" · +{len(procs) - limit}"
    return [line]


def _status_age_seconds(st: dict | None) -> int | None:
    if not st:
        return None
    raw = st.get("ts")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        t0 = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return max(0, int((datetime.now() - t0).total_seconds()))


def _status_is_stale(st: dict | None) -> bool:
    age = _status_age_seconds(st)
    return bool(age is not None and age >= _STALE_STATUS_SEC)


def _status_age_line(st: dict | None) -> str | None:
    sec = _status_age_seconds(st)
    if sec is None:
        return None
    phase = str(st.get("phase") or "?") if st else "?"
    if sec >= _STALE_STATUS_SEC:
        # Plain Russian: STALE ≠ current work; file simply wasn't updated.
        return (
            f"файл статуса устарел {_human_etime_seconds(sec)} "
            f"(последняя запись: {phase}) — не текущая работа"
        )
    return f"status: {phase} {_human_etime_seconds(sec)} ago"


_COPY_OK_RE = re.compile(
    r"\[copy\]\s+(?:(Front|Back)\s+)?\d+/\d+:\s+ok in (\d+)s",
)
_COPY_SIZE_RE = re.compile(r"\((\d+)\s*MB\)")
_COPY_LOG_RE = re.compile(
    r"\[copy\]\s+(?:(Front|Back)\s+)?(\d+/\d+):\s+(\S+)(?:\s+\(([^)]+)\))?",
)
_MERGE_CAMERA_LINE_RE = re.compile(
    r"Merging\s+((?:Normal|Event|Parking)/(?:Front|Back))",
)
_MERGE_LOG_RE = re.compile(
    r"\[merge\]\s+(.+)$",
)
_MERGE_BATCH_RE = re.compile(
    r"\[merge\]\s+(?:concat batch|part) (\d+)/(\d+)"
    r"(?: \((\d+) (?:inputs|clips)\))?",
)
_MERGE_FINAL_RE = re.compile(
    r"\[merge\]\s+final concat (\d+) parts",
)
_MERGE_HB_DETAIL_RE = re.compile(
    r"… merging ((?:NO|EV|PA)_\S+\.mp4)"
    r"(?: (?:batch|part) (\d+)/(\d+))? \(([^)]+)\)",
)
_MERGE_OUTPUT_RE = re.compile(r"→ ((?:NO|EV|PA)_\S+\.mp4)")
_MERGE_CAMERA_RE = re.compile(
    r"TOTAL:.*\| merging \| ([^|]+) \|",
)
_MERGE_SESSION_RE = re.compile(
    r"\[(\d+)/(\d+)\] session \d+/\d+ \| (\d+) clips, ([\d.]+) min",
)
_MERGE_DONE_RE = re.compile(
    r"\[merge\] DONE ((?:NO|EV|PA)_\S+\.mp4): (\d+) MB in (.+)$",
)

# {partial_path: {size, t, speed_mbps}}
_MERGE_SPEED_CACHE: dict[str, dict] = {}


def _parse_duration_token(text: str) -> float | None:
    """Parse import_70mai-style duration tokens (30s, 2m 15s, 1h 2m) → seconds."""
    text = text.strip()
    if not text:
        return None
    total = 0.0
    for amount, unit in re.findall(r"(\d+)\s*([dhms])", text, flags=re.I):
        n = int(amount)
        u = unit.lower()
        if u == "d":
            total += n * 86400
        elif u == "h":
            total += n * 3600
        elif u == "m":
            total += n * 60
        else:
            total += n
    return total if total > 0 else None


def _find_merge_partial(
    video_dir: Path | None, output_name: str, batch_idx: int | None
) -> Path | None:
    if not video_dir or not output_name:
        return None
    stem = Path(output_name).stem
    try:
        for stage_root in video_dir.glob("*/*/.merge_stage"):
            partial_dir = stage_root / stem
            if not partial_dir.is_dir():
                continue
            if batch_idx is not None:
                for name in (
                    f"_part_{batch_idx}.mp4",
                    f"_partial_{batch_idx}.mp4",
                ):
                    candidate = partial_dir / name
                    if candidate.is_file():
                        return candidate
            parts = sorted(partial_dir.glob("_part_*.mp4"))
            if parts:
                return parts[-1]
            partials = sorted(partial_dir.glob("_partial_*.mp4"))
            if partials:
                return partials[-1]
    except OSError:
        return None
    return None


def _sample_merge_speed_mbps(path: Path) -> float | None:
    """Estimate concat throughput from part/partial output (decimal MB/s)."""
    key = str(path.resolve())
    try:
        size = path.stat().st_size
        now = time.monotonic()
    except OSError:
        return None
    prev = _MERGE_SPEED_CACHE.get(key)
    if not prev:
        _MERGE_SPEED_CACHE[key] = {"size": size, "t": now}
        return None
    dt = now - float(prev.get("t") or 0)
    ds = size - int(prev.get("size") or 0)
    if dt < 0.4 or ds <= 0:
        return float(prev["speed"]) if prev.get("speed") is not None else None
    speed = ds / dt / 1_000_000
    _MERGE_SPEED_CACHE[key] = {"size": size, "t": now, "speed": speed}
    return speed


def parse_merge_log_detail(temp_dir: Path | None) -> dict | None:
    """Rich merge snapshot: output file, batch, inputs, camera, speed hints."""
    if temp_dir is None:
        return None
    text = _tail_text(temp_dir / "publish_all.log")
    if not text:
        return None

    output = ""
    camera = ""
    batch_cur = batch_total = inputs = None
    elapsed = ""
    session_clips = session_min = ""
    merge_done = merge_total = None
    last_done_speed: float | None = None
    batch_started_at: datetime | None = None
    final_parts: int | None = None
    last_done_name = ""
    last_done_note = ""

    for line in text.splitlines():
        m = _LOG_TS_RE.match(line)
        ts = None
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = None
        m = _MERGE_DONE_RE.search(line)
        if m:
            done_mb = float(m.group(2))
            done_sec = _parse_duration_token(m.group(3))
            if done_sec and done_sec > 0:
                last_done_speed = done_mb / done_sec
            last_done_name = m.group(1)
            last_done_note = f"{m.group(2)} MB in {m.group(3)}"
            if not output:
                output = m.group(1)
        m = _MERGE_CAMERA_RE.search(line)
        if m:
            camera = m.group(1).strip()
        m = _MERGE_CAMERA_LINE_RE.search(line)
        if m:
            camera = m.group(1).strip()
        m = _MERGE_SESSION_RE.search(line)
        if m:
            session_clips = m.group(3)
            session_min = m.group(4)
            merge_done = int(m.group(1)) - 1
            merge_total = int(m.group(2))
        m = _MERGE_OUTPUT_RE.search(line)
        if m:
            output = m.group(1)
        m = _MERGE_FINAL_RE.search(line)
        if m:
            final_parts = int(m.group(1))
        m = _MERGE_BATCH_RE.search(line)
        if m:
            batch_cur = int(m.group(1))
            batch_total = int(m.group(2))
            if m.group(3):
                inputs = int(m.group(3))
            if ts is not None:
                batch_started_at = ts
        m = _MERGE_HB_DETAIL_RE.search(line)
        if m:
            output = m.group(1)
            if m.group(2) and m.group(3):
                batch_cur = int(m.group(2))
                batch_total = int(m.group(3))
            elapsed = m.group(4).strip()

    if not output and batch_cur is None and final_parts is None:
        return None

    return {
        "output": output,
        "camera": camera,
        "batch_cur": batch_cur,
        "batch_total": batch_total,
        "inputs": inputs,
        "elapsed": elapsed,
        "session_clips": session_clips,
        "session_min": session_min,
        "merge_done": merge_done,
        "merge_total": merge_total,
        "final_parts": final_parts,
        "last_done_name": last_done_name,
        "last_done_note": last_done_note,
        "last_done_speed_mbps": last_done_speed,
        "batch_started_at": (
            batch_started_at.isoformat(timespec="seconds")
            if batch_started_at
            else None
        ),
        "active": batch_cur is not None or bool(elapsed) or final_parts is not None,
    }


def format_merge_detail(
    detail: dict | None,
    *,
    video_dir: Path | None = None,
) -> tuple[str, str | None]:
    """Return (short lane text, optional second detail line)."""
    if not detail:
        return "", None
    output = str(detail.get("output") or "").strip()
    short = Path(output).name[:36] if output else ""
    batch_cur = detail.get("batch_cur")
    batch_total = detail.get("batch_total")
    inputs = detail.get("inputs")
    elapsed = str(detail.get("elapsed") or "").strip()
    camera = str(detail.get("camera") or "").strip()

    speed: float | None = None
    partial = _find_merge_partial(
        video_dir,
        output,
        int(batch_cur) if batch_cur is not None else None,
    )
    if partial is not None:
        speed = _sample_merge_speed_mbps(partial)
        if speed is None:
            raw_start = detail.get("batch_started_at")
            if isinstance(raw_start, str) and raw_start.strip():
                try:
                    started = datetime.fromisoformat(raw_start.strip())
                    sec = max(1.0, (datetime.now() - started).total_seconds())
                    speed = partial.stat().st_size / sec / 1_000_000
                except (OSError, ValueError):
                    speed = None
    if speed is None:
        speed = detail.get("last_done_speed_mbps")

    parts: list[str] = []
    if batch_cur is not None and batch_total is not None:
        parts.append(f"part {batch_cur}/{batch_total}")
    if inputs is not None:
        parts.append(f"{inputs} клипов")
    if camera:
        parts.append(camera)
    if isinstance(speed, (int, float)) and speed > 0:
        parts.append(f"{speed:.1f} MB/s")
    if elapsed:
        parts.append(elapsed)
    if partial is not None:
        try:
            gb = partial.stat().st_size / 1_000_000_000
            parts.append(f"partial {gb:.2f} GB")
        except OSError:
            pass
    sess_clips = detail.get("session_clips")
    sess_min = detail.get("session_min")
    if sess_clips and sess_min and not parts:
        parts.append(f"{sess_clips} clips · {sess_min} min")

    detail_line = " · ".join(parts) if parts else None
    return short, detail_line


def _copy_speed_from_log(temp_dir: Path | None) -> float | None:
    """Recent SD→SSD copy throughput (decimal MB/s) from ok-in-Ns lines."""
    detail = parse_copy_log_detail(temp_dir)
    if not detail:
        return None
    speed = detail.get("avg_mbps")
    return float(speed) if isinstance(speed, (int, float)) else None


def _parse_copy_fraction(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)/(\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _camera_from_clip_name(name: str) -> str:
    """Infer Front/Back from 70mai clip / merge names (*F.MP4, *_F.mp4)."""
    upper = name.upper()
    if upper.endswith("F.MP4") or upper.endswith("_F.MP4") or "/_F" in upper:
        return "Front"
    if upper.endswith("B.MP4") or upper.endswith("_B.MP4") or "/_B" in upper:
        return "Back"
    if upper.endswith("F") or "_F/" in upper or upper.endswith("_F"):
        return "Front"
    if upper.endswith("B") or "_B/" in upper or upper.endswith("_B"):
        return "Back"
    return ""


def parse_copy_log_detail(temp_dir: Path | None) -> dict | None:
    """Rich copy snapshot: file, N/M, size, speed, camera, ETA."""
    if temp_dir is None:
        return None
    text = _tail_text(temp_dir / "publish_all.log", max_bytes=96_000)
    if not text:
        return None

    file_name = ""
    cur = total = None
    size_mb: float | None = None
    camera = ""
    started_at: datetime | None = None
    last_ok_sec: float | None = None
    speeds: list[float] = []
    ok_secs: list[float] = []
    last_start_mb = 126.0
    in_progress = False

    for line in text.splitlines():
        m_ts = _LOG_TS_RE.match(line)
        ts = None
        if m_ts:
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = None

        m_merge_cam = _MERGE_CAMERA_LINE_RE.search(line)
        if m_merge_cam:
            camera = m_merge_cam.group(1).strip()
        m_cam = _MERGE_CAMERA_RE.search(line)
        if m_cam:
            camera = m_cam.group(1).strip()

        if "[copy]" not in line:
            continue

        m_ok = _COPY_OK_RE.search(line)
        if m_ok:
            if m_ok.group(1):
                cam_only = m_ok.group(1)
                if cam_only and "/" not in camera:
                    camera = cam_only
                elif cam_only:
                    # Keep Type/Cam if we have it; else Front/Back
                    if not camera.endswith(cam_only):
                        camera = (
                            f"{camera.rsplit('/', 1)[0]}/{cam_only}"
                            if "/" in camera
                            else cam_only
                        )
            sec = float(m_ok.group(2))
            if sec > 0:
                ok_secs.append(sec)
                speeds.append(last_start_mb / sec)
                last_ok_sec = sec
            in_progress = False
            continue

        m = _COPY_LOG_RE.search(line)
        if not m or "ok in" in line:
            continue
        cam_tok = m.group(1) or ""
        frac = m.group(2)
        file_name = m.group(3)
        size_raw = m.group(4) or ""
        if cam_tok:
            if "/" in camera and camera.rsplit("/", 1)[-1] in ("Front", "Back"):
                camera = f"{camera.rsplit('/', 1)[0]}/{cam_tok}"
            elif "/" in camera:
                camera = f"{camera}/{cam_tok}" if not camera.endswith(cam_tok) else camera
            else:
                camera = cam_tok
        else:
            inferred = _camera_from_clip_name(file_name)
            if inferred:
                if "/" in camera:
                    camera = f"{camera.rsplit('/', 1)[0]}/{inferred}"
                else:
                    camera = inferred
        try:
            cur_s, total_s = frac.split("/", 1)
            cur, total = int(cur_s), int(total_s)
        except ValueError:
            cur = total = None
        sm = _COPY_SIZE_RE.search(f"({size_raw})" if size_raw else line)
        if sm:
            size_mb = float(sm.group(1))
            last_start_mb = size_mb
        elif "SD→SSD" in line:
            sm2 = _COPY_SIZE_RE.search(line)
            if sm2:
                size_mb = float(sm2.group(1))
                last_start_mb = size_mb
        if ts is not None:
            started_at = ts
        in_progress = True

    if not file_name and cur is None:
        return None

    if not camera and file_name:
        camera = _camera_from_clip_name(file_name)

    avg_mbps = sum(speeds[-5:]) / len(speeds[-5:]) if speeds else None
    avg_sec = sum(ok_secs[-5:]) / len(ok_secs[-5:]) if ok_secs else None
    elapsed = ""
    if in_progress and started_at is not None:
        sec = max(0, int((datetime.now() - started_at).total_seconds()))
        elapsed = _human_etime_seconds(sec)
    elif last_ok_sec is not None and not in_progress:
        elapsed = f"{int(last_ok_sec)}s"

    eta = ""
    if avg_sec and cur is not None and total is not None and cur < total:
        remain = total - cur + (1 if in_progress else 0)
        if remain > 0:
            eta = _human_etime_seconds(int(avg_sec * remain))

    return {
        "file": file_name,
        "cur": cur,
        "total": total,
        "size_mb": size_mb,
        "camera": camera,
        "avg_mbps": avg_mbps,
        "avg_sec": avg_sec,
        "elapsed": elapsed,
        "eta": eta,
        "in_progress": in_progress,
        "active": bool(file_name) or cur is not None,
    }


def format_copy_detail(detail: dict | None) -> tuple[str, str | None]:
    """Return (short lane text, optional second detail line)."""
    if not detail:
        return "", None
    file_name = str(detail.get("file") or "").strip()
    camera = str(detail.get("camera") or "").strip()
    # Prefer short cam label on the main lane: Front / Back / Parking/Front
    cam_short = camera
    if camera.endswith("/Front"):
        cam_short = "Front"
    elif camera.endswith("/Back"):
        cam_short = "Back"
    short = file_name[:36] if file_name else ""
    if cam_short and short:
        short = f"{cam_short} {short}"
    elif cam_short:
        short = cam_short
    cur = detail.get("cur")
    total = detail.get("total")
    size_mb = detail.get("size_mb")
    avg_mbps = detail.get("avg_mbps")
    elapsed = str(detail.get("elapsed") or "").strip()
    eta = str(detail.get("eta") or "").strip()

    parts: list[str] = []
    if camera:
        parts.append(camera)
    if cur is not None and total is not None:
        parts.append(f"{cur}/{total}")
        if total > 0:
            parts.append(f"{100.0 * float(cur) / float(total):.0f}%")
    if isinstance(size_mb, (int, float)) and size_mb > 0:
        parts.append(f"{size_mb:.0f} MB")
    parts.append("SD→SSD")
    if isinstance(avg_mbps, (int, float)) and avg_mbps > 0:
        parts.append(f"{avg_mbps:.1f} MB/s")
    if elapsed:
        parts.append(elapsed if detail.get("in_progress") else f"last {elapsed}")
    if eta:
        parts.append(f"ETA {eta}")
    return short, " · ".join(parts) if parts else None


_ENCODE_LOG_RE = re.compile(
    r"Encode:.*?\]\s*"
    r"(?P<pos>[^|]+?)\s*\((?P<pct>[\d.]+)%\)\s*\|\s*"
    r"(?P<elapsed>[^|]+?)\s+elapsed\s*\|\s*"
    r"ETA\s+(?P<eta>[^|]+?)\s*\|\s*"
    r"speed\s+(?P<speed>[\d.]+x|—)",
)
_UPLOAD_LOG_RE = re.compile(
    r"Upload\s+(\S+):\s*\[.*?\]\s*"
    r"([^\|]+?)\s*\|\s*([\d.]+)\s*MB/s\s*\|\s*"
    r"([^|]+?)\s+elapsed\s*\|\s*ETA\s+([^|]+?)\s*$",
)
_ENCODING_HB_RE = re.compile(
    r"(?:…|\.\.\.) encoding \(([^,]+),\s*([\d.]+)%(?:,\s*([\d.]+)x)?\)",
)


def _parse_status_ts(st: dict | None) -> datetime | None:
    if not isinstance(st, dict):
        return None
    raw = st.get("ts")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return None


def _compose_log_paths(temp_dir: Path) -> list[Path]:
    """*.log under temp_dir — newest mtime first (publish_all, *_rebuild, …)."""
    ranked: list[tuple[float, Path]] = []
    try:
        for path in temp_dir.glob("*.log"):
            ranked.append((path.stat().st_mtime, path))
    except OSError:
        return []
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in ranked]


def _compose_log_since(st: dict | None) -> datetime | None:
    """Drop Encode lines from an earlier compose session."""
    if not isinstance(st, dict):
        return None
    phase = str(st.get("phase") or "")
    if phase not in ("compose", "upload", "stall"):
        return None
    ts = _parse_status_ts(st)
    if ts is None:
        return None
    return ts - timedelta(minutes=45)


def parse_compose_log_detail(
    temp_dir: Path | None,
    *,
    since: datetime | None = None,
) -> dict | None:
    """Last Encode: / encoding heartbeat from temp_dir *.log files."""
    if temp_dir is None:
        return None
    best: dict | None = None
    best_ts: datetime | None = None
    for path in _compose_log_paths(temp_dir):
        text = _tail_text(path, max_bytes=96_000)
        if not text:
            continue
        encode_ctx: dict = {}
        for line in text.splitlines():
            match_ts = _LOG_TS_RE.match(line)
            line_ts: datetime | None = None
            if match_ts:
                try:
                    line_ts = datetime.strptime(
                        match_ts.group(1), "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    line_ts = None
            m = _ENCODE_LOG_RE.search(line)
            if m and line_ts is not None:
                speed_raw = m.group("speed")
                speed = None
                if speed_raw.endswith("x") and speed_raw != "—":
                    try:
                        speed = float(speed_raw[:-1])
                    except ValueError:
                        speed = None
                encode_ctx = {
                    "percent": float(m.group("pct")),
                    "position": m.group("pos").strip(),
                    "elapsed": m.group("elapsed").strip(),
                    "eta": m.group("eta").strip(),
                    "speed": speed,
                    "speed_unit": "x",
                    "action": "encode Front↑+Back↓",
                }
                if since is not None and line_ts < since:
                    continue
                entry = {**encode_ctx, "log_ts": line_ts, "log_file": path.name}
                if best_ts is None or line_ts >= best_ts:
                    best_ts = line_ts
                    best = entry
                continue
            m2 = _ENCODING_HB_RE.search(line)
            if m2 and line_ts is not None:
                speed = float(m2.group(3)) if m2.group(3) else None
                entry = {
                    "percent": float(m2.group(2)),
                    "position": encode_ctx.get("position"),
                    "elapsed": m2.group(1).strip(),
                    "eta": encode_ctx.get("eta"),
                    "speed": speed if speed is not None else encode_ctx.get("speed"),
                    "speed_unit": "x",
                    "action": encode_ctx.get("action") or "encode Front↑+Back↓",
                }
                if since is not None and line_ts < since:
                    continue
                entry["log_ts"] = line_ts
                entry["log_file"] = path.name
                if best_ts is None or line_ts >= best_ts:
                    best_ts = line_ts
                    best = entry
    return best


def parse_upload_log_detail(temp_dir: Path | None) -> dict | None:
    """Last Upload … MB/s line from publish_all.log."""
    if temp_dir is None:
        return None
    text = _tail_text(temp_dir / "publish_all.log", max_bytes=64_000)
    if not text:
        return None
    found: dict | None = None
    for line in text.splitlines():
        m = _UPLOAD_LOG_RE.search(line.rstrip())
        if not m:
            continue
        size_part = m.group(2).strip()
        pct_m = re.search(r"\((\d+)%\)", size_part)
        found = {
            "file": m.group(1),
            "bytes_txt": re.sub(r"\s*\(\d+%\)\s*$", "", size_part).strip(),
            "percent": float(pct_m.group(1)) if pct_m else None,
            "speed": float(m.group(3)),
            "speed_unit": "MB/s",
            "elapsed": m.group(4).strip(),
            "eta": m.group(5).strip(),
        }
    return found


def _log_matches_live_compose(st: dict, log_detail: dict) -> bool:
    """True when log telemetry belongs to the current compose session."""
    log_ts = log_detail.get("log_ts")
    if not isinstance(log_ts, datetime):
        return False
    st_ts = _parse_status_ts(st)
    if st_ts is not None:
        return log_ts >= st_ts - timedelta(minutes=2)
    return (datetime.now() - log_ts).total_seconds() <= 300.0


def format_compose_detail(
    st: dict | None,
    *,
    log_detail: dict | None = None,
    trip_label: str = "",
) -> tuple[str, str | None]:
    """Return (short lane text, optional second detail line) for compose."""
    st = st if isinstance(st, dict) else {}
    log_detail = log_detail or {}
    phase = str(st.get("phase") or "")
    live_compose = phase == "compose" and isinstance(st.get("percent"), (int, float))
    log_enrich = bool(log_detail) and (
        _log_matches_live_compose(st, log_detail)
        if live_compose
        else True
    )

    if live_compose:
        pct = float(st["percent"])
    elif isinstance(log_detail.get("percent"), (int, float)):
        pct = float(log_detail["percent"])
    elif isinstance(st.get("percent"), (int, float)):
        pct = float(st["percent"])
    else:
        pct = None

    out_b = st.get("output_bytes")
    speed = st.get("speed") if isinstance(st.get("speed"), (int, float)) else None
    if speed is None and log_enrich:
        speed = log_detail.get("speed")
    unit = str(
        st.get("speed_unit") or log_detail.get("speed_unit") or "x"
    )
    eta = str(st.get("eta") or (log_detail.get("eta") if log_enrich else "") or "").strip()
    elapsed = str(
        st.get("elapsed") or (log_detail.get("elapsed") if log_enrich else "") or ""
    ).strip()
    position = str(log_detail.get("position") or "").strip() if log_enrich else ""
    action = str(log_detail.get("action") or "").strip()
    detail_raw = str(st.get("detail") or "").strip()
    if not action and "Front" in detail_raw and "Back" in detail_raw:
        action = "encode Front↑+Back↓"
    elif not action:
        action = "encode Front↑+Back↓"
    record_type = str(st.get("record_type") or "").strip()
    chunk = st.get("chunk_index")
    trip = st.get("trip_index")

    short_bits: list[str] = []
    if isinstance(speed, (int, float)) and speed > 0:
        short_bits.append(f"{float(speed):.2f}x")
    if isinstance(pct, (int, float)):
        short_bits.append(f"{float(pct):.0f}%")
    if trip_label:
        short_bits.append(trip_label)
    short = " ".join(short_bits)

    parts: list[str] = []
    if record_type:
        parts.append(record_type)
    parts.append(action)
    if isinstance(chunk, int) and isinstance(trip, int) and (chunk or trip):
        parts.append(f"trip_{int(trip):02d}")
    if position:
        parts.append(position)
    if isinstance(pct, (int, float)):
        parts.append(f"{float(pct):.0f}%")
    if isinstance(out_b, (int, float)) and out_b > 0:
        mb = float(out_b) / (1024 * 1024)
        if mb >= 1024:
            parts.append(f"{mb / 1024:.2f} GB")
        else:
            parts.append(f"{mb:.0f} MB")
    if isinstance(speed, (int, float)) and speed > 0:
        if unit == "x":
            parts.append(f"{float(speed):.2f}x")
        else:
            parts.append(f"{float(speed):.1f} {unit}")
    if elapsed:
        parts.append(elapsed)
    if eta:
        parts.append(f"ETA {eta}")
    detail_line = " · ".join(parts) if parts else None
    return short, detail_line


def format_upload_detail(
    st: dict | None,
    *,
    log_detail: dict | None = None,
    trip_label: str = "",
    percent_override: float | None = None,
) -> tuple[str, str | None]:
    """Return (short lane text, optional second detail line) for upload."""
    st = st if isinstance(st, dict) else {}
    log_detail = log_detail or {}
    pct = percent_override
    if not isinstance(pct, (int, float)):
        pct = st.get("percent")
    if not isinstance(pct, (int, float)):
        pct = log_detail.get("percent")
    speed = st.get("speed")
    if not isinstance(speed, (int, float)):
        speed = log_detail.get("speed")
    unit = str(st.get("speed_unit") or log_detail.get("speed_unit") or "MB/s")
    eta = str(st.get("eta") or log_detail.get("eta") or "").strip()
    elapsed = str(st.get("elapsed") or log_detail.get("elapsed") or "").strip()
    bytes_txt = str(log_detail.get("bytes_txt") or "").strip()
    detail_raw = str(st.get("detail") or "").strip()
    if not bytes_txt and detail_raw:
        # "100.0 MB/1.2 GB · 2.5 MB/s" → keep size portion
        bytes_txt = detail_raw.split("·")[0].strip()
    record_type = str(st.get("record_type") or "").strip()
    file_name = str(log_detail.get("file") or "").strip()

    short_bits: list[str] = []
    if isinstance(pct, (int, float)):
        short_bits.append(f"{float(pct):.0f}%")
    if trip_label:
        short_bits.append(trip_label)
    short = " ".join(short_bits)

    parts: list[str] = []
    if record_type:
        parts.append(record_type)
    if file_name:
        parts.append(file_name[:28])
    if bytes_txt:
        parts.append(bytes_txt)
    elif isinstance(pct, (int, float)):
        parts.append(f"{float(pct):.0f}%")
    if isinstance(speed, (int, float)) and speed > 0:
        parts.append(f"{float(speed):.1f} {unit}")
    if elapsed:
        parts.append(elapsed)
    if eta:
        parts.append(f"ETA {eta}")
    detail_line = " · ".join(parts) if parts else None
    return short, detail_line


def _cameras_seen_in_log(temp_dir: Path | None) -> list[str]:
    """Unique Type/Cam in order of first seen; last entry = most recent activity."""
    if temp_dir is None:
        return []
    text = _tail_text(temp_dir / "publish_all.log", max_bytes=120_000)
    if not text:
        return []
    first: list[str] = []
    latest = ""
    for line in text.splitlines():
        cam = ""
        m = _MERGE_CAMERA_LINE_RE.search(line)
        if m:
            cam = m.group(1).strip()
        else:
            m2 = _MERGE_CAMERA_RE.search(line)
            if m2:
                cam = m2.group(1).strip()
        if not cam:
            # Also track from copy Front/Back tokens / filename suffix
            if "[copy]" in line and "ok in" not in line:
                m3 = _COPY_LOG_RE.search(line)
                if m3 and m3.group(1):
                    # Need record type from context — skip bare Front here
                    pass
            continue
        latest = cam
        if cam not in first:
            first.append(cam)
    if latest and (not first or first[-1] != latest):
        # Move latest to end so [-1] is the active camera session
        first = [c for c in first if c != latest] + [latest]
    return first


def _merge_output_exists(video_dir: Path | None, name: str) -> bool:
    if not video_dir or not name:
        return False
    base = Path(name).name
    for rt in ("Parking", "Event", "Normal"):
        for cam in ("Front", "Back"):
            path = video_dir / rt / cam / base
            try:
                if path.is_file() and path.stat().st_size > 1_000_000:
                    return True
            except OSError:
                continue
    return False


def _sibling_merge_name(output: str, camera: str) -> str:
    """PA_…_F.mp4 ↔ PA_…_B.mp4 for the requested camera."""
    out = output
    upper = out.upper()
    if upper.endswith("_F.MP4"):
        return out[:-6] + ("_F.mp4" if camera == "Front" else "_B.mp4")
    if upper.endswith("_B.MP4"):
        return out[:-6] + ("_B.mp4" if camera == "Back" else "_F.mp4")
    return out


_COVERAGE_CACHE: dict[str, tuple[float, dict]] = {}
_COVERAGE_TTL_SEC = 8.0
_MIN_COVERAGE = 0.98


def _ffprobe_duration_sec(path: Path) -> float | None:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nk=1:nw=1",
                str(path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=8,
        ).strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _trip_need_from_plan(
    temp_dir: Path | None, record_type: str
) -> tuple[float | None, object | None]:
    """Return (duration_sec, trip_start) for the single-video type trip."""
    if temp_dir is None:
        return None, None
    try:
        from plan_estimate import load_autopilot_plan

        chunks = load_autopilot_plan(temp_dir) or []
    except Exception:
        return None, None
    for chunk in chunks:
        if getattr(chunk, "record_type", "") != record_type:
            continue
        trips = getattr(chunk, "trips", None) or []
        if not trips:
            continue
        trip = trips[0]
        dur = float(getattr(trip, "duration_sec", 0) or 0)
        start = getattr(trip, "start", None)
        if dur > 0:
            return dur, start
    return None, None


def _camera_coverage(
    *,
    video_dir: Path | None,
    temp_dir: Path | None,
    record_type: str,
    camera: str,
    need_sec: float,
    trip_start,
    output_name: str = "",
    merge_d: dict | None = None,
    copy_d: dict | None = None,
) -> dict:
    """Live coverage for one camera vs trip need (≥98%)."""
    label = f"{record_type}/{camera}"
    result = {
        "label": label,
        "pct": 0.0,
        "have_sec": 0.0,
        "need_sec": need_sec,
        "source": "нет файла",
        "ok": False,
    }
    if need_sec <= 0 or video_dir is None:
        return result

    cache_key = f"{video_dir}:{label}:{output_name}"
    now = time.monotonic()
    cached = _COVERAGE_CACHE.get(cache_key)
    if cached and now - cached[0] < _COVERAGE_TTL_SEC:
        return dict(cached[1])

    have = 0.0
    source = "нет файла"

    # 1) Finished merge on SSD — plan_segments coverage (accurate)
    try:
        from compose_70mai import plan_segments, scan_merged_clips

        clips = scan_merged_clips(
            video_dir, camera, record_type=record_type, probe=True
        )
        if clips and trip_start is not None:
            try:
                segs = plan_segments(clips, trip_start, need_sec, 0.0)
                have = sum(seg.duration for seg in segs)
                source = "merge SSD"
            except (ValueError, OSError, RuntimeError):
                # Clip exists but does not cover this trip window
                have = 0.0
                source = "merge SSD (не покрывает поездку)"
        elif clips:
            dur = getattr(clips[0], "duration", None)
            if isinstance(dur, (int, float)) and dur > 0:
                have = float(dur)
                source = "merge SSD (длительность)"
    except Exception:
        pass

    # Active camera only: in-progress part/partial (ignore stale sibling leftovers)
    active_cam = ""
    if merge_d and merge_d.get("camera"):
        active_cam = str(merge_d["camera"]).rsplit("/", 1)[-1]
    elif copy_d and copy_d.get("camera"):
        active_cam = str(copy_d["camera"]).rsplit("/", 1)[-1]
    is_active = bool(active_cam) and active_cam == camera

    if have <= 0 and is_active and output_name:
        sibling = _sibling_merge_name(output_name, camera)
        batch = None
        if merge_d and merge_d.get("batch_cur") is not None:
            batch = int(merge_d["batch_cur"])
        partial = _find_merge_partial(video_dir, sibling, batch)
        if partial is None:
            partial = _find_merge_partial(video_dir, output_name, batch)
        if partial is not None:
            dur = _ffprobe_duration_sec(partial)
            if dur and dur > 0:
                have = dur
                source = f"partial {partial.name}"

    # Rough estimate from batch/copy while this camera is building
    if have <= 0 and is_active and merge_d:
        bc = merge_d.get("batch_cur")
        bt = merge_d.get("batch_total")
        if isinstance(bc, int) and isinstance(bt, int) and bt > 0:
            have = need_sec * (bc / bt)
            source = f"оценка merge {bc}/{bt}"
        elif copy_d and copy_d.get("cur") is not None and copy_d.get("total"):
            cur, total = int(copy_d["cur"]), int(copy_d["total"])
            if total > 0:
                have = need_sec * (cur / total)
                source = f"оценка copy {cur}/{total}"

    pct = min(100.0, 100.0 * have / need_sec) if need_sec > 0 else 0.0
    result = {
        "label": label,
        "pct": pct,
        "have_sec": have,
        "need_sec": need_sec,
        "source": source,
        "ok": have >= need_sec * _MIN_COVERAGE,
    }
    _COVERAGE_CACHE[cache_key] = (now, result)
    return result


def _format_coverage_line(cov: dict) -> str:
    pct = float(cov.get("pct") or 0)
    need = float(cov.get("need_sec") or 0)
    have = float(cov.get("have_sec") or 0)
    mark = "✓" if cov.get("ok") else "✗"
    need_m = need / 60.0
    have_m = have / 60.0
    src = str(cov.get("source") or "")
    return (
        f"{mark} {cov.get('label')}: {pct:.0f}% "
        f"({have_m:.0f}/{need_m:.0f}м) [{src}]"
    )



def format_compose_wait(
    *,
    temp_dir: Path | None,
    video_dir: Path | None = None,
    import_alive: bool = False,
    compose_on: bool = False,
    compose_done: bool = False,
    upload_on: bool = False,
    st: dict | None = None,
) -> tuple[str, list[str]]:
    """Explain why compose waits and how much import remains."""
    if compose_on or compose_done or upload_on:
        return "", []

    copy_d = parse_copy_log_detail(temp_dir) if import_alive or temp_dir else None
    merge_d = parse_merge_log_detail(temp_dir) if temp_dir else None
    cameras = _cameras_seen_in_log(temp_dir)

    record = str((st or {}).get("record_type") or "").strip()
    if not record and cameras:
        record = cameras[-1].split("/", 1)[0]
    if not record and copy_d and "/" in str(copy_d.get("camera") or ""):
        record = str(copy_d["camera"]).split("/", 1)[0]
    if not record:
        record = "Parking"

    need_sec, trip_start = _trip_need_from_plan(temp_dir, record)
    if need_sec is None and merge_d and merge_d.get("session_min"):
        try:
            need_sec = float(merge_d["session_min"]) * 60.0
        except (TypeError, ValueError):
            need_sec = None
    if need_sec is None:
        need_sec = 0.0

    out_name = str((merge_d or {}).get("output") or "")
    coverages = [
        _camera_coverage(
            video_dir=video_dir,
            temp_dir=temp_dir,
            record_type=record,
            camera=cam,
            need_sec=float(need_sec),
            trip_start=trip_start,
            output_name=out_name,
            merge_d=merge_d,
            copy_d=copy_d,
        )
        for cam in ("Front", "Back")
    ]

    need = [f"{record}/Front", f"{record}/Back"]
    ready = [c["label"] for c in coverages if c.get("ok")]

    current = ""
    if copy_d and copy_d.get("camera"):
        current = str(copy_d["camera"])
    elif merge_d and merge_d.get("camera"):
        current = str(merge_d["camera"])
    elif cameras:
        current = cameras[-1]
    if current and "/" not in current:
        current = f"{record}/{current}"

    if current in ready:
        ready = [r for r in ready if r != current]

    now_bits: list[str] = []
    eta_left: list[str] = []
    if copy_d and copy_d.get("cur") is not None and copy_d.get("total"):
        cur, total = int(copy_d["cur"]), int(copy_d["total"])
        now_bits.append(f"copy {cur}/{total}")
        if copy_d.get("eta"):
            eta_left.append(f"copy {copy_d['eta']}")
        elif copy_d.get("avg_sec"):
            left = max(0, total - cur + (1 if copy_d.get("in_progress") else 0))
            if left:
                eta_left.append(
                    f"copy {_human_etime_seconds(int(float(copy_d['avg_sec']) * left))}"
                )
    if merge_d and merge_d.get("batch_cur") is not None and merge_d.get("batch_total"):
        bc, bt = int(merge_d["batch_cur"]), int(merge_d["batch_total"])
        now_bits.append(f"merge batch {bc}/{bt}")
        left_b = max(0, bt - bc)
        batch_sec = 90.0
        es = _parse_duration_token(str(merge_d.get("elapsed") or ""))
        if es and es >= 10:
            batch_sec = float(es)
        if left_b:
            eta_left.append(
                f"merge {_human_etime_seconds(int(batch_sec * left_b))}"
            )

    remain_cams = [c for c in need if c not in ready]
    latest_cam = current or (cameras[-1] if cameras else "")
    back_not_started = (
        f"{record}/Back" in remain_cams
        and bool(latest_cam)
        and str(latest_cam).endswith("/Front")
    )

    extra = f"{record} Front+Back"
    both_ok = all(c.get("ok") for c in coverages)
    if both_ok:
        extra = f"{record} Front+Back ✓ готово к compose"

    lines = [
        f"условие: merge {record}/Front и {record}/Back на SSD "
        f"(покрытие ≥{_MIN_COVERAGE * 100:.0f}% поездки"
        + (f", нужно {_human_etime_seconds(int(need_sec))}" if need_sec else "")
        + ")",
        "покрытие: " + " · ".join(_format_coverage_line(c) for c in coverages),
    ]
    if ready:
        lines.append(f"уже готово: {', '.join(ready)}")
    if current or now_bits:
        lines.append(
            "сейчас: "
            + " · ".join(x for x in [current or None, " · ".join(now_bits) or None] if x)
        )
    left = []
    if remain_cams:
        left.append(" → ".join(remain_cams))
    if eta_left:
        left.append("текущая камера ≈ " + " + ".join(eta_left))
    if back_not_started:
        left.append("потом весь Back (~столько же)")
    if left:
        lines.append("осталось: " + " · ".join(left))
    elif not import_alive:
        lines.append("осталось: очередь до import/compose этой поездки")
    return extra, lines


def diagnose_pipeline_bottleneck(
    *,
    temp_dir: Path | None,
    copy_on: bool,
    merge_on: bool,
    compose_on: bool,
    upload_on: bool,
    copy_done: bool,
    merge_done: bool,
    compose_done: bool,
    video_done: bool,
    import_alive: bool,
    stale: bool,
    log_fallback: dict[str, str] | None,
    merge_detail_line: str | None,
    st: dict | None,
    procs: list,
    compose_detail_line: str | None = None,
    upload_detail_line: str | None = None,
) -> str | None:
    """One-line bottleneck / who-is-waiting diagnosis."""
    if stale and not import_alive and not any(
        (copy_on, merge_on, compose_on, upload_on)
    ):
        return "нет свежего статуса — смотри proc и publish_all.log"

    def _waiting() -> list[str]:
        out: list[str] = []
        if not copy_on and not copy_done:
            out.append("copy")
        if not merge_on and not merge_done:
            out.append("merge")
        if not compose_on and not compose_done:
            out.append("compose")
        if not upload_on and not video_done:
            out.append("upload")
        return out

    waiting = _waiting()
    copy_frac = (
        _parse_copy_fraction(log_fallback["copy"])
        if log_fallback and log_fallback.get("copy")
        else None
    )
    copy_mbps = _copy_speed_from_log(temp_dir)
    merge_mbps = None
    if merge_detail_line:
        m = re.search(r"([\d.]+)\s*MB/s", merge_detail_line)
        if m:
            merge_mbps = float(m.group(1))

    bottleneck = ""
    note = ""
    if import_alive:
        if copy_on and merge_on:
            if copy_mbps and merge_mbps and copy_mbps < merge_mbps * 0.6:
                bottleneck = f"copy SD→SSD (~{copy_mbps:.0f} MB/s)"
                note = f"merge ~{merge_mbps:.0f} MB/s, ждёт данные с SD"
            else:
                bottleneck = "copy+merge параллельно"
        elif merge_on and not copy_on:
            bottleneck = "merge concat"
            if copy_frac and copy_frac[0] < copy_frac[1]:
                note = f"ждёт copy ({copy_frac[0]}/{copy_frac[1]})"
        elif copy_on and not merge_on:
            bottleneck = f"copy SD→SSD (~{copy_mbps:.0f} MB/s)" if copy_mbps else "copy SD→SSD"
            if copy_frac:
                note = f"{copy_frac[0]}/{copy_frac[1]} — merge ждёт буфер"
    elif compose_on:
        bottleneck = "compose F+B"
        if compose_detail_line:
            m = re.search(r"([\d.]+)x", compose_detail_line)
            if m:
                note = f"encode ~{m.group(1)}x realtime"
            else:
                note = compose_detail_line[:48]
    elif upload_on:
        bottleneck = "upload YouTube"
        if upload_detail_line:
            m = re.search(r"([\d.]+)\s*MB/s", upload_detail_line)
            if m:
                note = f"~{m.group(1)} MB/s"
            else:
                note = upload_detail_line[:48]
    elif import_alive:
        bottleneck = "import"

    parts = [f"ожидание: {', '.join(waiting) if waiting else '—'}"]
    if bottleneck:
        parts.append(f"узкое место: {bottleneck}")
    if note:
        parts.append(note)
    return "  |  ".join(parts)


def _import_progress_from_log(
    temp_dir: Path | None,
    *,
    video_dir: Path | None = None,
) -> dict[str, str] | None:
    """When status.json is stale but import is alive, scrape publish_all.log."""
    if temp_dir is None:
        return None
    log_path = temp_dir / "publish_all.log"
    if not log_path.is_file():
        return None
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 96_000))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    copy_txt = ""
    merge_txt = ""
    for line in chunk.splitlines():
        if "[copy]" in line:
            m = _COPY_LOG_RE.search(line)
            if m and "ok in" not in line:
                bits = [
                    x
                    for x in (
                        m.group(1),
                        m.group(2),
                        (m.group(3) or "")[:36] or None,
                        m.group(4),
                    )
                    if x
                ]
                if bits:
                    copy_txt = " ".join(bits)
            elif "ok in" not in line:
                idx = line.find("[copy]")
                copy_txt = line[idx + 7 :].strip()[:56]
        if "[merge]" in line:
            m = _MERGE_LOG_RE.search(line)
            merge_txt = (m.group(1).strip() if m else line.strip())[:48]
    merge_detail = parse_merge_log_detail(temp_dir)
    merge_short, merge_extra = format_merge_detail(merge_detail, video_dir=video_dir)
    if merge_short:
        merge_txt = merge_short
    copy_detail = parse_copy_log_detail(temp_dir)
    copy_short, copy_extra = format_copy_detail(copy_detail)
    if copy_short:
        # Main lane: Front 17/248 name 126 MB
        if copy_detail and copy_detail.get("cur") is not None and copy_detail.get("total"):
            cam = ""
            raw_cam = str(copy_detail.get("camera") or "")
            if raw_cam.endswith("/Front") or raw_cam == "Front":
                cam = "Front "
            elif raw_cam.endswith("/Back") or raw_cam == "Back":
                cam = "Back "
            elif raw_cam:
                cam = f"{raw_cam} "
            # copy_short already has camera prefix — avoid doubling
            name = str(copy_detail.get("file") or copy_short)
            copy_txt = f"{cam}{copy_detail['cur']}/{copy_detail['total']} {name[:32]}"
            if copy_detail.get("size_mb"):
                copy_txt += f" {int(copy_detail['size_mb'])} MB"
        else:
            copy_txt = copy_short
    if not copy_txt and not merge_txt and not merge_extra and not copy_extra:
        return None
    out: dict[str, str] = {}
    if copy_txt:
        out["copy"] = copy_txt
    if copy_extra:
        out["copy_detail"] = copy_extra
    if merge_txt:
        out["merge"] = merge_txt
    if merge_extra:
        out["merge_detail"] = merge_extra
    return out or None


def _backfill_status_from_import_log(temp_dir: Path) -> bool:
    """If import is live but status.json is stale/missing --status-dir, write from log.

    Keeps autopilot_status.json fresh for this run without restarting import.
    """
    procs = list_pipeline_processes()
    if not any(p.role == "import" for p in procs):
        return False
    st = resolve_live_status(temp_dir)
    if st and str(st.get("phase") or "") == "import" and not _status_is_stale(st):
        return False
    snap = _import_progress_from_log(temp_dir, video_dir=temp_dir.parent)
    if not snap:
        return False
    copy_txt = snap.get("copy") or ""
    merge_txt = snap.get("merge") or ""
    merge_detail = snap.get("merge_detail") or ""
    detail = " · ".join(x for x in (copy_txt, merge_txt, merge_detail) if x)
    percent: float | None = None
    m = re.search(r"(\d+)/(\d+)", copy_txt)
    if m and int(m.group(2)) > 0:
        percent = 100.0 * int(m.group(1)) / int(m.group(2))
    conveyors = {
        "copy": {
            "active": bool(copy_txt),
            "file": copy_txt.split(" ", 1)[-1] if copy_txt else "",
            "chunk": "",
            "clip": copy_txt,
            "detail": copy_txt,
        },
        "merge": {
            "active": bool(merge_txt),
            "file": merge_txt,
            "chunk": "",
            "clip": merge_detail or merge_txt,
            "detail": merge_detail or merge_txt,
            "elapsed": "",
        },
    }
    try:
        write_import_status(
            temp_dir,
            record_type="Parking",
            percent=percent,
            detail=detail[:80],
            conveyors=conveyors,
            stage_ahead="copy" if copy_txt else "merge",
        )
    except OSError:
        return False
    return True


def _format_pipeline_block(
    st: dict | None,
    rows: list,
    *,
    temp_dir: Path | None = None,
    video_dir: Path | None = None,
    stale: bool = False,
    log_fallback: dict[str, str] | None = None,
    import_alive: bool = False,
    procs: list | None = None,
) -> list[str]:
    """One line per pipeline step: copy / merge / compose / upload + status."""
    phase = str((st or {}).get("phase") or "").strip()
    conveyors = (st or {}).get("conveyors") if isinstance(st, dict) else None
    if not isinstance(conveyors, dict):
        conveyors = {}
    copy = conveyors.get("copy") if isinstance(conveyors.get("copy"), dict) else {}
    merge = conveyors.get("merge") if isinstance(conveyors.get("merge"), dict) else {}
    detail = str((st or {}).get("detail") or "").strip()
    pct = (st or {}).get("percent") if isinstance(st, dict) else None
    pct_s = f"{float(pct):.0f}%" if isinstance(pct, (int, float)) else ""

    active_row = next(
        (
            r
            for r in rows
            if getattr(r, "status", "")
            in ("compose", "upload", "import", "stall", "oauth")
        ),
        None,
    )
    if stale:
        # Old status.json — do not show ghost "► compose".
        active_row = None
        phase = ""

    copy_on = bool(copy.get("active")) and not stale
    merge_on = bool(merge.get("active")) and not stale
    compose_on = (not stale) and (
        phase == "compose"
        or (active_row is not None and active_row.status == "compose")
    )
    upload_on = (not stale) and (
        phase == "upload"
        or (active_row is not None and active_row.status == "upload")
    )
    import_on = (not stale) and (phase == "import" or copy_on or merge_on)
    done_on = phase == "done" or (
        active_row is not None and active_row.status == "done"
    )

    copy_done = (not import_on and phase in ("compose", "upload", "done")) or (
        bool(copy) and not copy_on and merge_on
    )
    merge_done = phase in ("compose", "upload", "done")
    compose_done = phase in ("upload", "done") or (
        active_row is not None and active_row.status == "upload"
    )
    video_done = done_on

    def _short_lane(info: dict) -> str:
        file_name = str(info.get("file") or "").strip()
        chunk = str(info.get("chunk") or "").strip()
        bd = info.get("bytes_done")
        bt = info.get("bytes_total")
        parts: list[str] = []
        if chunk:
            parts.append(chunk)
        if file_name:
            parts.append(file_name[:28])
        if isinstance(bd, (int, float)) and isinstance(bt, (int, float)) and bt > 0:
            parts.append(f"{100.0 * float(bd) / float(bt):.0f}%")
        return " ".join(parts)

    copy_extra = _short_lane(copy) if copy_on else ""
    merge_extra = _short_lane(merge) if merge_on else ""
    copy_detail_line: str | None = None
    merge_detail_line: str | None = None
    if merge_on and merge:
        merge_short, merge_detail_line = format_merge_detail(
            {
                "output": merge.get("file") or "",
                "batch_cur": None,
                "batch_total": None,
                "inputs": None,
                "elapsed": merge.get("elapsed") or "",
                "camera": "",
            },
            video_dir=video_dir,
        )
        if merge_short:
            merge_extra = merge_short
    if pct_s and import_on and not merge_extra:
        merge_extra = pct_s
    elif pct_s and merge_on:
        merge_extra = f"{pct_s} {merge_extra}".strip()

    # Import runs copy ∥ merge — always prefer fresh log lines for both lanes.
    if log_fallback:
        copy_fb = log_fallback.get("copy") or ""
        if copy_fb:
            m = re.search(r"(\d+)/(\d+)", copy_fb)
            if m and int(m.group(1)) < int(m.group(2)):
                copy_on = True
                copy_extra = copy_fb
                copy_detail_line = log_fallback.get("copy_detail")
                copy_done = False
        if log_fallback.get("merge"):
            merge_on = True
            merge_extra = log_fallback["merge"]
            merge_detail_line = log_fallback.get("merge_detail")
            merge_done = False
        if copy_on or merge_on:
            import_on = True
            compose_on = False
            upload_on = False
            compose_done = False
            video_done = False
    else:
        if copy_on and temp_dir is not None and not copy_detail_line:
            cd = parse_copy_log_detail(temp_dir)
            copy_short, copy_detail_line = format_copy_detail(cd)
            if copy_short and not copy_extra:
                if cd and cd.get("cur") is not None and cd.get("total"):
                    copy_extra = f"{cd['cur']}/{cd['total']} {copy_short}"
                else:
                    copy_extra = copy_short
        if merge_on and temp_dir is not None and not merge_detail_line:
            md = parse_merge_log_detail(temp_dir)
            _, merge_detail_line = format_merge_detail(md, video_dir=video_dir)
            if md and md.get("output") and not merge_extra:
                merge_extra = Path(str(md["output"])).name[:36]

    compose_extra = ""
    compose_detail_line: str | None = None
    compose_wait_lines: list[str] = []
    if compose_on:
        trip_lbl = _trip_display(active_row) if active_row is not None else ""
        log_cd = (
            parse_compose_log_detail(
                temp_dir, since=_compose_log_since(st)
            )
            if temp_dir
            else None
        )
        compose_short, compose_detail_line = format_compose_detail(
            st, log_detail=log_cd, trip_label=trip_lbl
        )
        compose_extra = compose_short or (
            (detail[:48] if detail else "")
            or pct_s
            or trip_lbl
        )
        if not compose_detail_line and detail:
            compose_detail_line = detail[:72]
    elif not compose_done and not upload_on:
        compose_extra, compose_wait_lines = format_compose_wait(
            temp_dir=temp_dir,
            video_dir=video_dir,
            import_alive=import_alive or import_on,
            compose_on=compose_on,
            compose_done=compose_done,
            upload_on=upload_on,
            st=st,
        )

    upload_extra = ""
    upload_detail_line: str | None = None
    if upload_on:
        up_pct = None
        if active_row is not None and active_row.percent is not None:
            up_pct = float(active_row.percent)
        elif active_row is not None and temp_dir is not None:
            got = _read_upload_percent(temp_dir, active_row.trip_index)
            up_pct = float(got) if got is not None else None
        trip_lbl = _trip_display(active_row) if active_row is not None else ""
        log_ud = parse_upload_log_detail(temp_dir) if temp_dir else None
        upload_short, upload_detail_line = format_upload_detail(
            st,
            log_detail=log_ud,
            trip_label=trip_lbl,
            percent_override=up_pct,
        )
        upload_extra = upload_short or " ".join(
            x
            for x in (
                f"{up_pct:.0f}%" if isinstance(up_pct, (int, float)) else pct_s,
                trip_lbl,
            )
            if x
        )
        if not upload_detail_line and detail:
            upload_detail_line = detail[:72]

    def _line(name: str, *, on: bool, done: bool, extra: str = "") -> str:
        if on:
            status = f"► активно {extra}".strip() if extra else "► активно"
        elif done:
            status = "✓ готово"
        elif extra:
            status = f"· ждёт — {extra}"
        else:
            status = "· ждёт"
        return f"{name:<8} {status}"

    lines = [
        "этапы:",
        _line("copy", on=copy_on, done=copy_done, extra=copy_extra),
    ]
    if copy_on and copy_detail_line:
        lines.append(f"         {copy_detail_line}")
    lines.append(_line("merge", on=merge_on, done=merge_done, extra=merge_extra))
    if merge_on and merge_detail_line:
        lines.append(f"         {merge_detail_line}")
    lines.append(
        _line("compose", on=compose_on, done=compose_done, extra=compose_extra)
    )
    if compose_on and compose_detail_line:
        lines.append(f"         {compose_detail_line}")
    for wl in compose_wait_lines:
        lines.append(f"         {wl}")
    lines.append(
        _line(
            "upload",
            on=upload_on,
            done=video_done,
            extra=upload_extra,
        )
    )
    if upload_on and upload_detail_line:
        lines.append(f"         {upload_detail_line}")
    if log_fallback and (copy_on or merge_on) and stale:
        lines.append("источник: publish_all.log (status.json устарел)")
    elif stale:
        lines.append("idle — status.json устарел (см. proc)")
    diag = diagnose_pipeline_bottleneck(
        temp_dir=temp_dir,
        copy_on=copy_on,
        merge_on=merge_on,
        compose_on=compose_on,
        upload_on=upload_on,
        copy_done=copy_done,
        merge_done=merge_done,
        compose_done=compose_done,
        video_done=video_done,
        import_alive=import_alive,
        stale=stale,
        log_fallback=log_fallback,
        merge_detail_line=merge_detail_line,
        compose_detail_line=compose_detail_line,
        upload_detail_line=upload_detail_line,
        st=st,
        procs=procs or [],
    )
    if diag:
        lines.append(f"диагноз: {diag}")
    return lines


def _format_import_conveyors(st: dict) -> list[str]:
    """Backward-compatible alias (import-only lanes). Prefer _format_pipeline_block."""
    return _format_pipeline_block(st, [])


def _visible_rows(
    rows: list,
    *,
    term_rows: int,
    total: int,
    columns: int = 1,
) -> tuple[list, str | None]:
    """Collapse a long leading run of done trips so the table fits."""
    # Two columns ≈ half the vertical cost → show roughly 2× before collapsing.
    soft_cap = 14 * max(1, min(2, columns))
    if term_rows >= 36 or len(rows) <= soft_cap:
        return list(rows), None

    leading = 0
    for row in rows:
        if row.status == "done":
            leading += 1
        else:
            break
    if leading <= 3:
        return list(rows), None

    # Keep last done as context, hide the rest of the prefix.
    keep_from = leading - 1
    first_idx = rows[0].overall_index or 1
    last_hidden = rows[keep_from - 1].overall_index or keep_from
    note = f"✓ {first_idx}–{last_hidden}/{total} готово (свёрнуто)"
    return list(rows[keep_from:]), note


def _import_row_progress(
    temp_dir: Path,
    *,
    record_type: str = "",
) -> str:
    """Human-readable Этап text for a trip currently in import (no cryptic «10м»)."""
    snap = _import_progress_from_log(temp_dir, video_dir=temp_dir.parent)
    bits: list[str] = []
    if record_type in ("Parking", "Event", "Normal"):
        bits.append(record_type)
    bits.append("import")
    if snap:
        copy_txt = snap.get("copy") or ""
        frac = _parse_copy_fraction(copy_txt) if copy_txt else None
        if frac:
            bits.append(f"copy {frac[0]}/{frac[1]}")
        elif copy_txt:
            bits.append("copy…")
        md = parse_merge_log_detail(temp_dir)
        if md and md.get("batch_cur") is not None and md.get("batch_total"):
            bits.append(f"merge {md['batch_cur']}/{md['batch_total']}")
        elif snap.get("merge"):
            bits.append("merge…")
        speed = snap.get("copy_detail") or ""
        m = re.search(r"([\d.]+)\s*MB/s", speed)
        if m:
            bits.append(f"~{m.group(1)} MB/s")
    else:
        bits.append("SD→SSD / merge")
    return " · ".join(bits)


def _stage_label(
    status: str,
    *,
    percent: float | None = None,
    stalled: bool = False,
    overall_index: int | None = None,
    overall_total: int | None = None,
    detail: str = "",
) -> str:
    """Single human-readable stage for the Этап column (№ already shows N/M)."""
    if status == "done":
        return "✓"
    if status == "oauth":
        return "OAuth вход"
    if status == "fail":
        return "ошибка"
    if stalled or status == "stall":
        if percent is not None:
            return f"ЗАВИС {percent:.0f}%"
        return "ЗАВИС"
    if status == "upload":
        if percent is not None:
            return f"↑ YouTube {percent:.0f}%"
        return "↑ YouTube …"
    if status == "compose":
        if percent is not None:
            return f"сборка F+B {percent:.0f}%"
        return "сборка F+B"
    if status == "import":
        if detail:
            return detail
        if percent is not None:
            return f"import {percent:.0f}%"
        return "import SD→SSD/merge"
    if status == "pending":
        return "ожидание"
    return status


@dataclass
class TripRow:
    key: str
    record_type: str
    chunk_index: int
    trip_index: int
    label: str
    duration_sec: float
    clip_count: int = 0
    status: str = "pending"
    youtube_url: str | None = None
    disk: str = "—"
    progress: str = "—"
    reason: str = "—"
    local_path: str = "—"
    detail: str = ""
    percent: float | None = None
    stalled: bool = False
    trip_start: datetime | None = None
    trip_end: datetime | None = None
    overall_index: int = 0


def _trip_display(row: TripRow) -> str:
    if row.record_type == "Event":
        base = "все события"
    elif row.record_type == "Parking":
        base = "все parking"
    else:
        parts = row.label.split()
        if len(parts) >= 3 and parts[0] == "trip":
            return " ".join(parts[2:])
        return row.label
    if row.clip_count > 0:
        return f"{base} · {row.clip_count} clips"
    return base


def _read_upload_percent(temp_dir: Path, trip_index: int) -> float | None:
    """Best-effort upload % from resumable session file on disk."""
    from youtube_upload import load_upload_session

    session_path = temp_dir / f"trip_{trip_index:02d}.upload.json"
    saved = load_upload_session(session_path)
    if not saved:
        return None
    size = saved.get("size")
    offset = saved.get("offset", 0)
    if not size or int(size) <= 0:
        return None
    return min(99.0, float(offset) * 100.0 / float(size))


def _plan_mtime_for(temp_dir: Path) -> float | None:
    path = temp_dir / "autopilot_plan.json"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


@dataclass
class Dashboard:
    rows: list[TripRow] = field(default_factory=list)
    temp_dir: Path = Path("video/Output/.publish_tmp")
    video_dir: Path = Path("video/Output")
    check_disk: Path = Path(".")
    min_free_gb: float = 20.0
    source: Path | None = None
    types: list[str] = field(default_factory=lambda: ["Normal"])
    state_on_sd: bool = True
    enabled: bool = True
    refresh_interval: float = 1.0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _tty = None
    _lines: int = 0
    _alt_screen: bool = False
    _youtube_ok: bool | None = None
    _youtube_detail: str = "—"
    _youtube_checked_at: float = 0.0
    _upload_health: str = "unknown"
    _upload_health_detail: str = "no uploads yet"
    _upload_health_checked_at: float = 0.0
    _plan_mtime: float | None = None

    def reload_plan_if_changed(self) -> bool:
        """Rebuild trip rows when autopilot_plan.json is updated on disk."""
        from plan_estimate import load_autopilot_plan

        path = self.temp_dir / "autopilot_plan.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if self._plan_mtime is not None and mtime == self._plan_mtime:
            return False
        chunks = load_autopilot_plan(self.temp_dir)
        if not chunks:
            return False
        try:
            from publish_all_70mai import load_merged_publish_state

            state = load_merged_publish_state(
                self.source or Path("."),
                self.types,
                self.temp_dir,
                state_on_sd=False,
                quiet=True,
            )
        except OSError:
            state = {}
        refreshed = Dashboard.from_plan(
            chunks,
            state,
            temp_dir=self.temp_dir,
            video_dir=self.video_dir,
            check_disk=self.check_disk,
            min_free_gb=self.min_free_gb,
            enabled=self.enabled,
            source=self.source,
            types=self.types,
            state_on_sd=self.state_on_sd,
        )
        self.rows = refreshed.rows
        self._plan_mtime = mtime
        return True

    @classmethod
    def from_plan(
        cls,
        chunks: list,
        state: dict,
        *,
        temp_dir: Path,
        video_dir: Path,
        check_disk: Path,
        min_free_gb: float,
        enabled: bool = True,
        source: Path | None = None,
        types: list[str] | None = None,
        state_on_sd: bool = True,
    ) -> Dashboard:
        from import_70mai import format_duration
        from plan_estimate import SINGLE_VIDEO_TYPES
        from publish_70mai import get_upload_entry, is_row_uploaded
        from publish_state import youtube_watch_url

        rows: list[TripRow] = []
        for chunk in chunks:
            for trip_idx, trip in enumerate(chunk.trips, start=1):
                if chunk.record_type in SINGLE_VIDEO_TYPES:
                    label = (
                        "all events"
                        if chunk.record_type == "Event"
                        else "all parking"
                    )
                else:
                    label = f"trip {trip.index} {trip.start:%m-%d %H:%M}"
                key = f"{chunk.record_type}:{chunk.index}:{trip_idx}"
                uploaded = is_row_uploaded(
                    state, chunk.record_type, chunk.index, trip_idx
                )
                entry = get_upload_entry(
                    state, chunk.record_type, chunk.index, trip_idx
                )
                url = None
                if entry:
                    url = entry.get("youtube_url")
                    if not url and entry.get("video_id"):
                        url = youtube_watch_url(entry["video_id"])
                merged = _merged_bytes_for_trip(
                    video_dir, chunk.record_type, trip.start, trip.end
                )
                composed = _compose_bytes(temp_dir, chunk.index, trip_idx)
                if uploaded:
                    disk = "pruned" if merged == 0 else f"merged {_fmt_gb(merged)}"
                    status = "done"
                elif composed > 0:
                    disk = _fmt_gb(composed)
                    status = "pending"
                elif merged > 0:
                    disk = f"merged {_fmt_gb(merged)}"
                    status = "pending"
                else:
                    disk = "clean"
                    status = "pending"
                local_path = _resolve_local_path(
                    temp_dir=temp_dir,
                    video_dir=video_dir,
                    record_type=chunk.record_type,
                    chunk_index=chunk.index,
                    trip_index=trip_idx,
                    status=status,
                    composed_bytes=composed,
                    merged_bytes=merged,
                )
                rows.append(
                    TripRow(
                        key=key,
                        record_type=chunk.record_type,
                        chunk_index=chunk.index,
                        trip_index=trip_idx,
                        label=label,
                        duration_sec=trip.duration_sec,
                        clip_count=int(getattr(trip, "clip_count", 0) or 0),
                        status=status,
                        youtube_url=url,
                        disk=disk,
                        local_path=local_path,
                        trip_start=trip.start,
                        trip_end=trip.end,
                        detail=format_duration(trip.duration_sec),
                        overall_index=len(rows) + 1,
                    )
                )
        return cls(
            rows=rows,
            temp_dir=temp_dir,
            video_dir=video_dir,
            check_disk=check_disk,
            min_free_gb=min_free_gb,
            source=source,
            types=list(types or ["Normal"]),
            state_on_sd=state_on_sd,
            enabled=enabled,
            _plan_mtime=_plan_mtime_for(temp_dir),
        )

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self._tty = open("/dev/tty", "w", encoding="utf-8")
        except OSError:
            self.enabled = False
            return
        try:
            self._tty.write("\033[?1049h\033[H\033[J")
            self._tty.flush()
            self._alt_screen = True
        except OSError:
            self._alt_screen = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._tty is not None:
            try:
                if self._alt_screen:
                    self._tty.write("\033[?1049l")
                else:
                    self._tty.write("\n")
                self._tty.flush()
                self._tty.close()
            except OSError:
                pass
            self._tty = None
            self._alt_screen = False

    def _loop(self) -> None:
        while not self._stop.wait(self.refresh_interval):
            try:
                _backfill_status_from_import_log(self.temp_dir)
            except Exception:
                pass
            try:
                self.reload_plan_if_changed()
            except Exception:
                pass
            self._refresh_youtube_reachability()
            self._refresh_upload_health()
            self._refresh_from_publish_state()
            self._refresh_from_status()
            self.render()

    def _refresh_from_publish_state(self) -> None:
        """Reload uploaded trips + YouTube URLs from host publish state.

        Uses the local cache only so a busy/unresponsive SD cannot freeze the UI.
        Autopilot writes the same state to the host on every save.
        """
        if self.source is None:
            return
        st = resolve_live_status(self.temp_dir)
        # Stale status.json must not block marking uploaded trips as done.
        active_key = None if _status_is_stale(st) else _status_active_key(self.rows, st)
        active_phase = (
            ""
            if _status_is_stale(st) or not st
            else str(st.get("phase") or "")
        )
        try:
            from publish_all_70mai import load_merged_publish_state
            from publish_70mai import get_upload_entry, is_row_uploaded
            from publish_state import youtube_watch_url

            state = load_merged_publish_state(
                self.source,
                self.types,
                self.temp_dir,
                # Local only: a busy SD must not freeze the dashboard refresh loop.
                state_on_sd=False,
                quiet=True,
            )
        except OSError:
            return
        for row in self.rows:
            # Live compose/upload/stall — keep overlay from autopilot_status.json
            if (
                active_key == row.key
                and active_phase in ("compose", "upload", "stall", "import", "oauth")
            ):
                continue
            if not is_row_uploaded(
                state, row.record_type, row.chunk_index, row.trip_index
            ):
                continue
            entry = get_upload_entry(
                state, row.record_type, row.chunk_index, row.trip_index
            )
            url = None
            if entry:
                url = entry.get("youtube_url")
                if not url and entry.get("video_id"):
                    url = youtube_watch_url(entry["video_id"])
            row.status = "done"
            row.progress = "✓"
            row.percent = None
            row.stalled = False
            row.reason = "—"
            row.youtube_url = url
            merged = 0
            if row.trip_start is not None and row.trip_end is not None:
                merged = _merged_bytes_for_trip(
                    self.video_dir,
                    row.record_type,
                    row.trip_start,
                    row.trip_end,
                )
            row.disk = "pruned" if merged == 0 else f"merged {_fmt_gb(merged)}"
            row.local_path = _resolve_local_path(
                temp_dir=self.temp_dir,
                video_dir=self.video_dir,
                record_type=row.record_type,
                chunk_index=row.chunk_index,
                trip_index=row.trip_index,
                status="done",
                composed_bytes=0,
                merged_bytes=merged,
                base=self.check_disk,
            )

    def _refresh_youtube_reachability(self) -> None:
        now = time.monotonic()
        if now - self._youtube_checked_at < 30.0:
            return
        self._youtube_checked_at = now
        try:
            from youtube_upload import check_youtube_reachable

            ok, detail = check_youtube_reachable()
        except Exception as exc:
            ok, detail = False, str(exc)[:48]
        self._youtube_ok = ok
        self._youtube_detail = detail

    def _refresh_upload_health(self) -> None:
        now = time.monotonic()
        if now - self._upload_health_checked_at < 5.0:
            return
        self._upload_health_checked_at = now
        from youtube_upload_diagnostics import latest_upload_health

        health, detail = latest_upload_health(
            self.temp_dir / "youtube_upload.diag.jsonl"
        )
        self._upload_health = health
        self._upload_health_detail = detail

    def _refresh_from_status(self) -> None:
        st = resolve_live_status(self.temp_dir)
        reasons = read_trip_reasons(self.temp_dir)
        # Ignore ghost compose/upload from an old status.json.
        active_key = None if _status_is_stale(st) else _status_active_key(self.rows, st)
        for row in self.rows:
            composed = _row_compose_bytes(
                self.temp_dir, row, active_key=active_key
            )
            merged_bytes = 0
            if row.trip_start is not None and row.trip_end is not None:
                merged_bytes = _merged_bytes_for_trip(
                    self.video_dir,
                    row.record_type,
                    row.trip_start,
                    row.trip_end,
                )
            row.local_path = _resolve_local_path(
                temp_dir=self.temp_dir,
                video_dir=self.video_dir,
                record_type=row.record_type,
                chunk_index=row.chunk_index,
                trip_index=row.trip_index,
                status=row.status,
                composed_bytes=composed,
                merged_bytes=merged_bytes,
                base=self.check_disk,
            )
            saved = reasons.get(row.key, {})
            saved_reason = saved.get("reason", "") if isinstance(saved, dict) else ""
            saved_phase = saved.get("phase", "") if isinstance(saved, dict) else ""

            if active_key and row.key == active_key and st:
                phase = st.get("phase") or row.status
                if phase not in LIVE_STATUS_PHASES:
                    continue
                row.status = phase
                stalled = bool(st.get("stalled"))
                if stalled:
                    row.status = "stall"
                pct = st.get("percent")
                if isinstance(pct, (int, float)):
                    pct_f: float | None = float(pct)
                else:
                    pct_f = None
                if phase == "upload" and pct_f is None:
                    pct_f = _read_upload_percent(self.temp_dir, row.trip_index)
                if phase == "import":
                    try:
                        row.progress = _import_row_progress(
                            self.temp_dir, record_type=row.record_type
                        )
                    except Exception:
                        row.progress = "import SD→SSD/merge"
                else:
                    row.progress = _stage_label(
                        row.status,
                        percent=pct_f,
                        stalled=stalled,
                    )
                row.percent = pct_f
                row.stalled = stalled
                live_reason = (st.get("reason") or "").strip()
                if live_reason:
                    row.reason = live_reason
                elif stalled:
                    row.reason = "ffmpeg завис (нет прогресса)"
                elif row.status in ("compose", "upload", "import"):
                    row.reason = "—"
                else:
                    row.reason = saved_reason or "—"
                if phase == "upload" and st.get("youtube_url"):
                    row.youtube_url = st["youtube_url"]
                if phase in ("compose", "upload", "stall") and composed > 0:
                    row.disk = _fmt_gb(composed)
                elif phase == "import" and merged_bytes > 0:
                    row.disk = f"merged {_fmt_gb(merged_bytes)}"
                continue

            if row.status == "done":
                row.progress = "✓"
                row.percent = None
                row.stalled = False
                row.reason = "—"
            elif row.status == "fail" or saved_phase == "fail":
                row.status = "fail"
                row.progress = "ошибка"
                row.percent = None
                row.stalled = False
                row.reason = saved_reason or "ошибка"
            else:
                # Import maps across trips as merge advances; clear stale overlay.
                if row.status == "import":
                    row.status = "pending"
                row.progress = _stage_label(row.status)
                row.percent = None
                row.stalled = False
                row.reason = saved_reason or "—"

    def render(self) -> None:
        if not self.enabled or self._tty is None:
            return
        view = _load_dashboard_view()
        view.render(self)


def _load_dashboard_view():
    """Import/reload autopilot_dashboard_view.py when its mtime changes."""
    import importlib
    import sys

    import autopilot_dashboard_view as view

    path = Path(view.__file__).resolve()
    try:
        st = path.stat()
        fp = (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))
    except OSError:
        return view
    prev = getattr(_load_dashboard_view, "_fp", None)
    if prev is not None and fp != prev:
        view = importlib.reload(view)
        try:
            sys.stderr.write(f"[dashboard] view reloaded ({path.name})\n")
            sys.stderr.flush()
        except OSError:
            pass
        _load_dashboard_view._note = (  # type: ignore[attr-defined]
            f"view reload {datetime.now():%H:%M:%S}"
        )
    _load_dashboard_view._fp = fp  # type: ignore[attr-defined]
    return view


def _dashboard_watch_paths() -> list[Path]:
    here = Path(__file__).resolve()
    return [
        here,
        here.with_name("autopilot_dashboard_view.py"),
    ]


def _file_fingerprint(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size), int(getattr(st, "st_ino", 0)))
    except OSError:
        return (-1, -1, -1)


def _dashboard_supervisor(argv: list[str]) -> int:
    """Keep one long-lived parent; respawn --worker when dashboard sources change."""
    import signal
    import sys

    watch = [p.resolve() for p in _dashboard_watch_paths()]
    fingerprints: dict[str, tuple[int, int, int]] = {
        str(path): _file_fingerprint(path) for path in watch
    }

    worker_argv = [sys.executable, str(Path(__file__).resolve()), "--worker", *argv[1:]]
    child: subprocess.Popen | None = None

    def _stop_child() -> None:
        nonlocal child
        if child is None or child.poll() is not None:
            return
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)

    def _on_signal(signum: int, _frame: object) -> None:
        _stop_child()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    sys.stderr.write(
        "[dashboard] auto-reload: правьте lib/autopilot_dashboard_view.py "
        "(экран) или lib/autopilot_dashboard.py — воркер перезапустится сам\n"
    )
    sys.stderr.flush()

    while True:
        # Re-read fingerprints right before spawn so we don't miss edits
        # that landed during the previous worker shutdown.
        fingerprints = {str(path): _file_fingerprint(path) for path in watch}
        child = subprocess.Popen(worker_argv)  # noqa: S603 — controlled argv
        reload = False
        changed_name = ""
        while child.poll() is None:
            time.sleep(0.4)
            for path in watch:
                key = str(path)
                fp = _file_fingerprint(path)
                if fp != fingerprints.get(key, (-1, -1, -1)):
                    fingerprints[key] = fp
                    changed_name = path.name
                    reload = True
                    break
            if reload:
                sys.stderr.write(
                    f"\n[dashboard] файл изменён ({changed_name}) — перезапуск…\n"
                )
                sys.stderr.flush()
                _stop_child()
                time.sleep(0.2)
                break
        else:
            # Worker exited on its own (crash or clean exit).
            code = child.returncode if child is not None else 0
            if code == 0:
                return 0
            sys.stderr.write(
                f"\n[dashboard] воркер упал (exit {code}) — перезапуск через 1с…\n"
            )
            sys.stderr.flush()
            time.sleep(1.0)
            continue


def main() -> int:
    """Standalone dashboard: reads status/state from disk only (no autopilot process)."""
    import argparse
    import signal
    import sys

    # Parent watches files and respawns --worker on change (no manual restart).
    if "--worker" not in sys.argv:
        return _dashboard_supervisor(sys.argv)

    from plan_estimate import DEFAULT_SESSION_GAP
    from project_env import ensure_venv_python

    ensure_venv_python()

    from publish_all_70mai import (
        DEFAULT_TEMP_DIR,
        DEFAULT_TYPES,
        DEFAULT_VIDEO_DIR,
        IMPORT_CHUNK_MINUTES,
        aggregate_plan,
        find_sd_card,
        load_merged_publish_state,
        wait_for_sd,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Live autopilot dashboard (standalone). Prefers host-local "
            "autopilot_plan.json + publish_*.state.json so a busy SD import "
            "cannot freeze the UI. Use --scan-sd to rebuild the plan from the card."
        ),
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="70mai SD mount (auto-detect /Volumes if omitted)",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait until SD card is inserted before building the trip table",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=DEFAULT_TYPES,
        choices=["Normal", "Event", "Parking"],
        metavar="TYPE",
    )
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--temp-dir", type=Path, default=DEFAULT_TEMP_DIR)
    parser.add_argument(
        "--no-state-on-sd",
        action="store_true",
        help="Read publish state from host cache only (default for live refresh)",
    )
    parser.add_argument(
        "--scan-sd",
        action="store_true",
        help="Rebuild trip plan by scanning the SD (slow/hangs while import runs)",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=20.0,
        metavar="GB",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SEC",
        help="Refresh interval (default: 1)",
    )
    parser.add_argument(
        "--session-gap",
        type=float,
        default=DEFAULT_SESSION_GAP,
        help="Session gap for trip plan (match autopilot)",
    )
    args = parser.parse_args()

    from plan_estimate import load_autopilot_plan, save_autopilot_plan

    source: Path | None
    if args.wait:
        print("Waiting for SD card…", flush=True)
        source = wait_for_sd()
    elif args.source is not None:
        source = args.source.resolve()
    else:
        source = find_sd_card()

    ffprobe = shutil.which("ffprobe") or "ffprobe"

    chunks = None if args.scan_sd else load_autopilot_plan(args.temp_dir)
    if chunks:
        n_trips = sum(len(c.trips) for c in chunks)
        print(
            f"Loaded cached plan: {n_trips} trip(s) in {len(chunks)} chunk(s) "
            f"({args.temp_dir / 'autopilot_plan.json'})",
            flush=True,
        )
        if source is None:
            source = Path("/Volumes/Untitled")
    else:
        if source is None:
            print(
                "SD card not found and no cached plan — "
                "use --source /Volumes/Untitled, --wait, or run autopilot first",
                file=sys.stderr,
            )
            return 1
        print(
            "No cached plan — scanning SD (may stall while import is busy)…",
            flush=True,
        )
        _trips, chunks, _dur, _total, _pending = aggregate_plan(
            source,
            args.types,
            args.temp_dir,
            state_on_sd=not args.no_state_on_sd,
            ffprobe=ffprobe,
            chunk_minutes=IMPORT_CHUNK_MINUTES,
            session_gap=args.session_gap,
        )
        if chunks:
            save_autopilot_plan(
                args.temp_dir,
                source=source,
                types=args.types,
                chunks=chunks,
                chunk_minutes=IMPORT_CHUNK_MINUTES,
                session_gap=args.session_gap,
            )
            print(
                f"Cached plan written: {args.temp_dir / 'autopilot_plan.json'}",
                flush=True,
            )

    if not chunks:
        print("No trips in plan (empty SD or wrong --types?)", file=sys.stderr)
        return 1

    assert source is not None
    print("Loading local publish state…", flush=True)
    merged_state = load_merged_publish_state(
        source, args.types, args.temp_dir, state_on_sd=False, quiet=True
    )
    dashboard = Dashboard.from_plan(
        chunks,
        merged_state,
        temp_dir=args.temp_dir,
        video_dir=args.video_dir,
        check_disk=Path("."),
        min_free_gb=args.min_free_gb,
        enabled=True,
        source=source,
        types=args.types,
        state_on_sd=False,
    )
    dashboard.refresh_interval = args.interval
    dashboard.start()
    if not dashboard.enabled:
        print("Cannot open /dev/tty — run in a real terminal (not piped)", file=sys.stderr)
        return 1

    dashboard._refresh_youtube_reachability()
    dashboard._refresh_upload_health()
    dashboard._refresh_from_publish_state()
    dashboard._refresh_from_status()
    dashboard.render()

    stop = threading.Event()

    def _shutdown(*_args: object) -> None:
        dashboard.stop()
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
