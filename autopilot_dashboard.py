#!/usr/bin/env python3
"""Live TTY dashboard for autopilot trip progress (Status / Progress / Disk / YouTube)."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STATUS_FILENAME = "autopilot_status.json"
REASONS_FILENAME = "autopilot_trip_reasons.json"


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


def read_status(temp_dir: Path) -> dict | None:
    path = status_path(temp_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def _fmt_gb(n: int) -> str:
    if n <= 0:
        return "—"
    gb = n / (1024**3)
    if gb >= 1:
        return f"{gb:.1f}G"
    return f"{n / (1024**2):.0f}M"


# Column widths: computed from terminal size in _column_widths().
_COL_HEADERS = (
    "#",
    "Type",
    "Label",
    "Dur",
    "Status",
    "Progress",
    "Disk",
    "Path",
    "YouTube",
    "Reason",
)

_STATUS_LEGEND = (
    "Статусы: pending compose upload done stall fail",
    "Path: chunk_XX/trip_YY.mp4  |  YouTube: youtu.be/VIDEO_ID",
)


def _table_width(widths: tuple[int, ...]) -> int:
    """Rendered row width including border chars between columns."""
    n = len(widths)
    if n == 0:
        return 0
    return 2 + (n - 1) + sum(w + 2 for w in widths)


def _column_widths(term_cols: int) -> tuple[int, ...]:
    """Fit table into terminal width; Path/YouTube keep priority."""
    term_cols = max(72, term_cols)
    # #: Type Label Dur Status Progress Disk Path YouTube Reason
    widths = [3, 6, 12, 9, 7, 7, 8, 20, 19, 8]
    floors = [2, 4, 6, 7, 5, 5, 5, 10, 19, 4]

    def fits(w: list[int]) -> bool:
        return _table_width(tuple(w)) <= term_cols

    while not fits(widths):
        shrunk = False
        for idx in (9, 2, 6, 3, 5, 1, 4, 0, 7, 8):
            if fits(widths):
                break
            if widths[idx] > floors[idx]:
                widths[idx] -= 1
                shrunk = True
        if not shrunk:
            break

    while fits([widths[0], widths[1], widths[2], widths[3], widths[4],
                widths[5], widths[6], widths[7] + 1, widths[8], widths[9]]):
        widths[7] += 1

    return tuple(widths)


def _compact_table_width(widths: tuple[int, ...]) -> int:
    n = len(widths)
    if n == 0:
        return 0
    return 3 * (n - 1) + sum(widths)


def _compact_column_widths(term_cols: int) -> tuple[int, ...]:
    term_cols = max(72, term_cols)
    widths = [2, 4, 6, 7, 5, 5, 4, 14, 11, 0]
    floors = [2, 3, 4, 6, 4, 4, 3, 6, 6, 0]

    def fits(w: list[int]) -> bool:
        return _compact_table_width(tuple(w)) <= term_cols

    while not fits(widths):
        shrunk = False
        for idx in (9, 2, 6, 3, 5, 1, 4, 0, 7, 8):
            if fits(widths):
                break
            if widths[idx] > floors[idx]:
                widths[idx] -= 1
                shrunk = True
        if not shrunk:
            break

    while fits([*widths[:7], widths[7] + 1, *widths[8:]]):
        widths[7] += 1

    return tuple(widths)


def _use_compact_table(term_cols: int) -> bool:
    return _table_width(_column_widths(term_cols)) > term_cols


def _compact_row(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    parts = []
    for cell, w in zip(cells, widths):
        if w <= 0:
            continue
        text = cell if len(cell) <= w else _fit_text(cell, w)
        parts.append(text)
    return " | ".join(parts)


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
            cut = width
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip(" |")
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


def _progress_label(
    status: str,
    *,
    percent: float | None = None,
    stalled: bool = False,
    is_active: bool = False,
) -> str:
    if status == "done":
        return "done"
    if status == "fail":
        return "fail"
    if status == "upload":
        return "upload"
    if not is_active:
        return "—"
    if stalled or status == "stall":
        if percent is not None:
            return f"STALL {percent:.0f}%"
        return "STALL"
    if status == "compose" and percent is not None:
        return f"{percent:.0f}%"
    if status == "compose":
        return "…"
    if status == "import":
        return "import"
    return "—"


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
    trip_start: datetime | None = None
    trip_end: datetime | None = None


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
        from publish_70mai import get_trip_state, trip_uploaded
        from publish_state import youtube_watch_url

        rows: list[TripRow] = []
        for chunk in chunks:
            for trip_idx, trip in enumerate(chunk.trips, start=1):
                if chunk.record_type == "Event":
                    label = "all events"
                else:
                    label = f"trip {trip.index} {trip.start:%m-%d %H:%M}"
                key = f"{chunk.record_type}:{chunk.index}:{trip_idx}"
                uploaded = trip_uploaded(
                    state, chunk.record_type, chunk.index, trip_idx
                )
                entry = get_trip_state(
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
            self._refresh_from_publish_state()
            self._refresh_from_status()
            self.render()

    def _refresh_from_publish_state(self) -> None:
        """Reload uploaded trips + YouTube URLs from SD/host publish state (source of truth)."""
        if self.source is None:
            return
        st = read_status(self.temp_dir)
        active_key: str | None = None
        active_phase = ""
        if st:
            active_key = trip_key(
                st.get("record_type", ""),
                int(st.get("chunk_index") or 0),
                int(st.get("trip_index") or 0),
            )
            active_phase = str(st.get("phase") or "")
        try:
            from publish_all_70mai import load_merged_publish_state
            from publish_70mai import get_trip_state, trip_uploaded
            from publish_state import youtube_watch_url

            state = load_merged_publish_state(
                self.source,
                self.types,
                self.temp_dir,
                state_on_sd=self.state_on_sd,
            )
        except OSError:
            return
        for row in self.rows:
            # Live compose/upload/stall — keep overlay from autopilot_status.json
            if (
                active_key == row.key
                and active_phase in ("compose", "upload", "stall", "import")
            ):
                continue
            if not trip_uploaded(
                state, row.record_type, row.chunk_index, row.trip_index
            ):
                continue
            entry = get_trip_state(
                state, row.record_type, row.chunk_index, row.trip_index
            )
            url = None
            if entry:
                url = entry.get("youtube_url")
                if not url and entry.get("video_id"):
                    url = youtube_watch_url(entry["video_id"])
            row.status = "done"
            row.progress = "done"
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

    def _refresh_from_status(self) -> None:
        st = read_status(self.temp_dir)
        reasons = read_trip_reasons(self.temp_dir)
        active_key: str | None = None
        if st:
            active_key = trip_key(
                st.get("record_type", ""),
                int(st.get("chunk_index") or 0),
                int(st.get("trip_index") or 0),
            )
        for row in self.rows:
            composed = _compose_bytes(
                self.temp_dir, row.chunk_index, row.trip_index
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

            if active_key and row.key == active_key and st:
                phase = st.get("phase") or row.status
                row.status = phase
                stalled = bool(st.get("stalled"))
                if stalled:
                    row.status = "stall"
                pct = st.get("percent")
                if isinstance(pct, (int, float)):
                    pct_f: float | None = float(pct)
                else:
                    pct_f = None
                row.progress = _progress_label(
                    row.status,
                    percent=pct_f,
                    stalled=stalled,
                    is_active=True,
                )
                live_reason = (st.get("reason") or "").strip()
                if live_reason:
                    row.reason = live_reason
                elif stalled:
                    row.reason = "ffmpeg завис (нет прогресса)"
                elif row.status in ("compose", "upload", "import"):
                    row.reason = "—"
                else:
                    row.reason = saved_reason or "—"
                if st.get("youtube_url"):
                    row.youtube_url = st["youtube_url"]
                composed = _compose_bytes(
                    self.temp_dir, row.chunk_index, row.trip_index
                )
                if phase == "done":
                    row.disk = "pruned" if "merged" not in row.disk else row.disk
                    row.reason = "—"
                elif phase in ("compose", "upload", "stall") and composed > 0:
                    row.disk = _fmt_gb(composed)
                continue

            if row.status == "done":
                row.progress = "done"
                row.reason = "—"
            elif row.status == "fail":
                row.progress = "fail"
                row.reason = saved_reason or "ошибка"
            else:
                row.progress = "—"
                row.reason = saved_reason or "—"

    def render(self) -> None:
        if not self.enabled or self._tty is None:
            return
        from import_70mai import format_duration

        done = sum(1 for r in self.rows if r.status == "done")
        fail = sum(1 for r in self.rows if r.status == "fail")
        total = len(self.rows)
        free = free_disk_gb(self.check_disk)
        from publish_all_70mai import autopilot_disk_usage, format_gb

        usage = autopilot_disk_usage(self.video_dir, self.temp_dir)
        active = next(
            (r for r in self.rows if r.status in ("compose", "upload", "import", "stall")),
            None,
        )
        if self._youtube_ok is True:
            yt_net = "YouTube OK"
        elif self._youtube_ok is False:
            yt_net = f"YouTube OFF ({self._youtube_detail})"
        else:
            yt_net = "YouTube …"
        phase = (
            f"{active.status} {active.record_type} "
            f"c{active.chunk_index}/t{active.trip_index}"
            if active
            else "idle"
        )
        try:
            term_cols = shutil.get_terminal_size().columns
        except OSError:
            term_cols = 100
        col_widths = (
            _compact_column_widths(term_cols)
            if _use_compact_table(term_cols)
            else _column_widths(term_cols)
        )
        compact = _use_compact_table(term_cols)
        header = (
            f"Autopilot {done}/{total} done"
            + (f"  {fail} fail" if fail else "")
            + f"  |  phase: {phase}"
            + f"  |  free {free:.1f} GB"
            + f"  |  video {format_gb(usage['total'])}"
            + f"  |  {yt_net}"
        )
        lines: list[str] = []
        for hl in _wrap_line(header, term_cols):
            lines.append(hl)
        lines.append("")
        if compact:
            lines.append(_compact_row(_COL_HEADERS, col_widths))
            lines.append("-" * min(term_cols, _compact_table_width(col_widths)))
        else:
            lines.append(_table_top(col_widths))
            lines.append(_table_row(_COL_HEADERS, col_widths))
            lines.append(_table_sep(col_widths))
        for i, row in enumerate(self.rows, start=1):
            dur = format_duration(row.duration_sec)
            cells = (
                str(i),
                _fit_text(row.record_type, col_widths[1]),
                _fit_text(row.label, col_widths[2]),
                _fit_text(dur, col_widths[3]),
                _fit_text(row.status, col_widths[4]),
                _fit_text(row.progress, col_widths[5]),
                _fit_text(row.disk, col_widths[6]),
                _path_for_column(row.local_path, col_widths[7]),
                _youtube_for_column(row.youtube_url, col_widths[8]),
                _fit_text(row.reason, col_widths[9], tail=True),
            )
            if compact:
                lines.append(_compact_row(cells, col_widths))
            else:
                lines.append(_table_row(cells, col_widths))
        if not compact:
            lines.append(_table_bottom(col_widths))
        lines.append("")
        for leg in _STATUS_LEGEND:
            lines.extend(_wrap_line(leg, term_cols))
        block = "\n".join(lines)
        out = self._tty
        if self._alt_screen:
            out.write("\033[H\033[J")
        elif self._lines:
            out.write(f"\033[{self._lines}A\033[J")
        out.write(block)
        if not block.endswith("\n"):
            out.write("\n")
        out.flush()
        self._lines = len(lines)


def main() -> int:
    """Standalone dashboard: reads status/state from disk only (no autopilot process)."""
    import argparse
    import signal
    import sys

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
            "Live autopilot dashboard (standalone). Reads autopilot_status.json, "
            "publish state on SD, and compose file sizes — safe to restart anytime."
        ),
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
        help="Read publish state from host cache only",
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

    if args.wait:
        source = wait_for_sd()
    elif args.source is not None:
        source = args.source.resolve()
    else:
        source = find_sd_card()
        if source is None:
            print(
                "SD card not found — use --source /Volumes/Untitled or --wait",
                file=sys.stderr,
            )
            return 1

    ffprobe = shutil.which("ffprobe") or "ffprobe"
    state_on_sd = not args.no_state_on_sd

    _trips, chunks, _dur, _total, _pending = aggregate_plan(
        source,
        args.types,
        args.temp_dir,
        state_on_sd=state_on_sd,
        ffprobe=ffprobe,
        chunk_minutes=IMPORT_CHUNK_MINUTES,
        session_gap=args.session_gap,
    )
    if not chunks:
        print("No trips in plan (empty SD or wrong --types?)", file=sys.stderr)
        return 1

    merged_state = load_merged_publish_state(
        source, args.types, args.temp_dir, state_on_sd=state_on_sd
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
        state_on_sd=state_on_sd,
    )
    dashboard.refresh_interval = args.interval
    dashboard.start()
    if not dashboard.enabled:
        print("Cannot open /dev/tty — run in a real terminal (not piped)", file=sys.stderr)
        return 1

    dashboard._refresh_youtube_reachability()
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
