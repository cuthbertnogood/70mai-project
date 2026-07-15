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
from datetime import datetime
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
    r"… merging ((?:NO|EV|PA)_\S+\.mp4) \(([^)]+)\)"
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
    last_hb: tuple[datetime, str, str] | None = None
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
            last_hb = (ts, match.group(1), match.group(2))
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
        latest_ts, filename, elapsed_note = last_hb
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
    if status == "done":
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
    widths = [6, 14, 8, 18, 6, 12]
    floors = [5, 10, 6, 12, 4, 8]
    caps = [7, 18, 9, 28, 7, 14]

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


def _use_compact_table(term_cols: int) -> bool:
    return term_cols < 90


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
    ("upload", re.compile(r"publish_70mai\.py", re.I)),
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
        "upload": 3,
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


_COPY_LOG_RE = re.compile(
    r"\[copy\]\s+(\d+/\d+):\s+(\S+)(?:\s+\(([^)]+)\))?",
)
_MERGE_LOG_RE = re.compile(
    r"\[merge\]\s+(.+)$",
)


def _import_progress_from_log(temp_dir: Path | None) -> dict[str, str] | None:
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
            if m:
                bits = [m.group(1), m.group(2)[:40]]
                if m.group(3):
                    bits.append(m.group(3))
                # Prefer the "N/N: name" start line over "ok in Ns"
                if "ok in" not in line:
                    copy_txt = " ".join(bits)
            elif "ok in" not in line:
                # keep raw short fragment
                idx = line.find("[copy]")
                copy_txt = line[idx + 7 :].strip()[:56]
        if "[merge]" in line:
            m = _MERGE_LOG_RE.search(line)
            merge_txt = (m.group(1).strip() if m else line.strip())[:48]
    if not copy_txt and not merge_txt:
        return None
    return {"copy": copy_txt, "merge": merge_txt}


def _format_pipeline_block(
    st: dict | None,
    rows: list,
    *,
    temp_dir: Path | None = None,
    stale: bool = False,
    log_fallback: dict[str, str] | None = None,
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
    if pct_s and import_on and not merge_extra:
        merge_extra = pct_s
    elif pct_s and merge_on:
        merge_extra = f"{pct_s} {merge_extra}".strip()

    # Live import but stale/missing status.json → scrape publish_all.log
    if log_fallback and not copy_on and not merge_on:
        if log_fallback.get("copy"):
            copy_on = True
            copy_extra = log_fallback["copy"]
            copy_done = False
        if log_fallback.get("merge"):
            merge_on = True
            merge_extra = log_fallback["merge"]
            merge_done = False
        import_on = copy_on or merge_on
        compose_on = False
        upload_on = False
        compose_done = False
        video_done = False

    compose_extra = ""
    if compose_on:
        bits = []
        if detail:
            bits.append(detail[:48])
        elif pct_s:
            bits.append(pct_s)
        if active_row is not None:
            bits.append(_trip_display(active_row))
        compose_extra = " ".join(bits)

    upload_extra = ""
    if upload_on:
        up_pct = None
        if active_row is not None and active_row.percent is not None:
            up_pct = float(active_row.percent)
        elif active_row is not None and temp_dir is not None:
            got = _read_upload_percent(temp_dir, active_row.trip_index)
            up_pct = float(got) if got is not None else None
        bits = []
        if isinstance(up_pct, (int, float)):
            bits.append(f"{up_pct:.0f}%")
        elif pct_s:
            bits.append(pct_s)
        if active_row is not None:
            bits.append(_trip_display(active_row))
        upload_extra = " ".join(bits)

    def _line(name: str, *, on: bool, done: bool, extra: str = "") -> str:
        if on:
            status = f"► активно {extra}".strip() if extra else "► активно"
        elif done:
            status = "✓ готово"
        else:
            status = "· ждёт"
        return f"{name:<8} {status}"

    lines = [
        "этапы:",
        _line("copy", on=copy_on, done=copy_done, extra=copy_extra),
        _line("merge", on=merge_on, done=merge_done, extra=merge_extra),
        _line("compose", on=compose_on, done=compose_done, extra=compose_extra),
        _line(
            "upload",
            on=upload_on,
            done=video_done,
            extra=upload_extra,
        ),
    ]
    if log_fallback and (copy_on or merge_on) and stale:
        lines.append("источник: publish_all.log (status.json устарел)")
    elif stale:
        lines.append("idle — status.json устарел (см. proc)")
    return lines


def _format_import_conveyors(st: dict) -> list[str]:
    """Backward-compatible alias (import-only lanes). Prefer _format_pipeline_block."""
    return _format_pipeline_block(st, [])


def _visible_rows(rows: list, *, term_rows: int, total: int) -> tuple[list, str | None]:
    """Collapse a long leading run of done trips so the table fits."""
    if term_rows >= 36 or len(rows) <= 14:
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


def _stage_label(
    status: str,
    *,
    percent: float | None = None,
    stalled: bool = False,
    overall_index: int | None = None,
    overall_total: int | None = None,
) -> str:
    """Single human-readable stage (replaces separate Status + Progress)."""
    pos = ""
    if overall_index is not None and overall_total:
        pos = f"{overall_index}/{overall_total} "
    if status == "done":
        return "✓ ролик"
    if status == "oauth":
        return f"{pos}OAuth вход".strip()
    if status == "fail":
        return f"{pos}ошибка".strip()
    if stalled or status == "stall":
        if percent is not None:
            return f"{pos}ЗАВИС {percent:.0f}%".strip()
        return f"{pos}ЗАВИС".strip()
    if status == "upload":
        if percent is not None:
            return f"{pos}↑ 2ч {percent:.0f}%".strip()
        return f"{pos}↑ 2ч …".strip()
    if status == "compose":
        if percent is not None:
            return f"{pos}F+B {percent:.0f}%".strip()
        return f"{pos}F+B сборка".strip()
    if status == "import":
        if percent is not None:
            return f"{pos}10м {percent:.0f}%".strip()
        return f"{pos}copy/merge".strip() if pos else "copy/merge"
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
        return "все события"
    if row.record_type == "Parking":
        return "все parking"
    parts = row.label.split()
    if len(parts) >= 3 and parts[0] == "trip":
        return " ".join(parts[2:])
    return row.label


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
                total = len(self.rows)
                row.progress = _stage_label(
                    row.status,
                    percent=pct_f,
                    stalled=stalled,
                    overall_index=row.overall_index or None,
                    overall_total=total,
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
                total = len(self.rows)
                row.progress = _stage_label(
                    row.status,
                    overall_index=row.overall_index or None,
                    overall_total=total,
                )
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
        mtime = path.stat().st_mtime_ns
    except OSError:
        return view
    prev = getattr(_load_dashboard_view, "_mtime", None)
    if prev is not None and mtime != prev:
        view = importlib.reload(view)
        # Brief note on stderr so the TTY table is not polluted mid-frame.
        try:
            sys.stderr.write("[dashboard] view reloaded\n")
            sys.stderr.flush()
        except OSError:
            pass
    _load_dashboard_view._mtime = mtime  # type: ignore[attr-defined]
    return view


def _dashboard_watch_paths() -> list[Path]:
    here = Path(__file__).resolve()
    return [
        here,
        here.with_name("autopilot_dashboard_view.py"),
    ]


def _dashboard_supervisor(argv: list[str]) -> int:
    """Keep one long-lived parent; respawn --worker when dashboard sources change."""
    import signal
    import sys

    watch = _dashboard_watch_paths()
    mtimes: dict[Path, int] = {}
    for path in watch:
        try:
            mtimes[path] = path.stat().st_mtime_ns
        except OSError:
            mtimes[path] = -1

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

    while True:
        child = subprocess.Popen(worker_argv)  # noqa: S603 — controlled argv
        reload = False
        while child.poll() is None:
            time.sleep(0.7)
            for path in watch:
                try:
                    mtime = path.stat().st_mtime_ns
                except OSError:
                    continue
                if mtime != mtimes.get(path, -1):
                    mtimes[path] = mtime
                    reload = True
                    break
            if reload:
                sys.stderr.write(
                    f"\n[dashboard] файл изменён ({path.name}) — перезапуск…\n"
                )
                sys.stderr.flush()
                _stop_child()
                time.sleep(0.25)
                break
        else:
            code = child.returncode if child is not None else 0
            return int(code or 0)


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
