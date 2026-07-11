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
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
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


def _compose_bytes(temp_dir: Path, chunk_index: int, trip_index: int) -> int:
    path = temp_dir / f"chunk_{chunk_index:02d}" / f"trip_{trip_index:02d}.mp4"
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


# Column widths for the trip table (cell content, excluding padding).
_COL_WIDTHS = (3, 6, 22, 10, 8, 10, 10, 26)
_COL_HEADERS = ("#", "Type", "Label", "Dur", "Status", "Progress", "Disk", "YouTube")

_STATUS_LEGEND = (
    "Статусы:  pending — в очереди  |  compose — кодирование (ffmpeg)  |  "
    "upload — заливка на YouTube",
    "          done — уже на YouTube  |  stall — encode завис  |  fail — ошибка",
    "Диск:     merged N — исходники с SD  |  N G/M — готовый MP4  |  "
    "pruned — загружено, исходники удалены  |  clean — пусто",
    "Progress: N% — идёт encode  |  STALL N% — нет прогресса 5+ мин  |  "
    "upload — заливается  |  done — готово  |  — — не активно",
)


def _table_top(widths: tuple[int, ...]) -> str:
    return "┏" + "┳".join("━" * (w + 2) for w in widths) + "┓"


def _table_sep(widths: tuple[int, ...]) -> str:
    return "┣" + "┳".join("━" * (w + 2) for w in widths) + "┫"


def _table_bottom(widths: tuple[int, ...]) -> str:
    return "┗" + "┻".join("━" * (w + 2) for w in widths) + "┛"


def _table_row(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    padded = []
    for cell, w in zip(cells, widths):
        text = cell if len(cell) <= w else cell[: max(0, w - 1)] + "…"
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
    detail: str = ""


@dataclass
class Dashboard:
    rows: list[TripRow] = field(default_factory=list)
    temp_dir: Path = Path("video/Output/.publish_tmp")
    video_dir: Path = Path("video/Output")
    check_disk: Path = Path(".")
    min_free_gb: float = 20.0
    enabled: bool = True
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _tty = None
    _lines: int = 0
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
    ) -> Dashboard:
        from import_70mai import format_duration
        from publish_70mai import get_trip_state, trip_uploaded

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
                        url = f"https://youtu.be/{entry['video_id']}"
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
                        detail=format_duration(trip.duration_sec),
                    )
                )
        return cls(
            rows=rows,
            temp_dir=temp_dir,
            video_dir=video_dir,
            check_disk=check_disk,
            min_free_gb=min_free_gb,
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
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._tty is not None:
            try:
                self._tty.write("\n")
                self._tty.flush()
                self._tty.close()
            except OSError:
                pass
            self._tty = None

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            self._refresh_youtube_reachability()
            self._refresh_from_status()
            self.render()

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
        active_key: str | None = None
        if st:
            active_key = (
                f"{st.get('record_type')}:{st.get('chunk_index')}:{st.get('trip_index')}"
            )
        for row in self.rows:
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
                if st.get("youtube_url"):
                    row.youtube_url = st["youtube_url"]
                composed = _compose_bytes(
                    self.temp_dir, row.chunk_index, row.trip_index
                )
                if phase == "done":
                    row.disk = "pruned" if "merged" not in row.disk else row.disk
                elif phase in ("compose", "upload", "stall") and composed > 0:
                    row.disk = _fmt_gb(composed)
                continue

            if row.status == "done":
                row.progress = "done"
            elif row.status == "fail":
                row.progress = "fail"
            else:
                row.progress = "—"

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
        lines = [
            f"Autopilot  {done}/{total} done"
            + (f"  {fail} fail" if fail else "")
            + f"  |  free {free:.1f} GB (reserve {self.min_free_gb:g})"
            + f"  |  video {format_gb(usage['total'])} "
            f"(merged {format_gb(usage['merged'])}, tmp {format_gb(usage['composed'])})"
            + f"  |  {yt_net}"
            + f"  |  phase: {phase}",
            "",
            _table_top(_COL_WIDTHS),
            _table_row(_COL_HEADERS, _COL_WIDTHS),
            _table_sep(_COL_WIDTHS),
        ]
        for i, row in enumerate(self.rows, start=1):
            yt = (row.youtube_url or "—").replace("https://", "")
            dur = format_duration(row.duration_sec)
            lines.append(
                _table_row(
                    (
                        str(i),
                        row.record_type,
                        row.label,
                        dur,
                        row.status,
                        row.progress,
                        row.disk,
                        yt,
                    ),
                    _COL_WIDTHS,
                )
            )
        lines.append(_table_bottom(_COL_WIDTHS))
        lines.append("")
        lines.extend(_STATUS_LEGEND)
        block = "\n".join(lines)
        # Clear previous block then rewrite
        out = self._tty
        if self._lines:
            out.write(f"\033[{self._lines}A\033[J")
        out.write(block + "\n")
        out.flush()
        self._lines = len(lines)
