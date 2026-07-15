#!/usr/bin/env python3
"""Persistent ffprobe duration cache shared by plan/import/publish.

Keyed by absolute path + mtime + size, so SD clips are probed once per run
chain instead of three times (plan in autopilot, import, publish estimate).
"""

from __future__ import annotations

import atexit
import json
import os
import tempfile
import threading
from pathlib import Path

DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parent.parent / "video" / "Output" / ".probe_cache.json"
)
_SAVE_EVERY = 50  # flush to disk every N new entries


class ProbeCache:
    """Thread-safe persistent {path: duration} cache with mtime/size validation."""

    def __init__(self, path: Path = DEFAULT_CACHE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._dirty = 0
        self._data: dict[str, dict] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, ValueError):
            pass

    @staticmethod
    def _stat_key(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return int(st.st_mtime), st.st_size

    def get(self, path: Path) -> float | None:
        key = str(path.resolve())
        entry = self._data.get(key)
        if not entry:
            return None
        stat = self._stat_key(path)
        if stat is None or entry.get("mtime") != stat[0] or entry.get("size") != stat[1]:
            return None
        duration = entry.get("duration")
        return float(duration) if duration is not None else None

    def put(self, path: Path, duration: float) -> None:
        stat = self._stat_key(path)
        if stat is None:
            return
        key = str(path.resolve())
        with self._lock:
            self._data[key] = {
                "mtime": stat[0],
                "size": stat[1],
                "duration": duration,
            }
            self._dirty += 1
            if self._dirty >= _SAVE_EVERY:
                self._save_locked()

    def save(self) -> None:
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".probe_cache_", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle)
            os.replace(tmp_name, self.path)
            self._dirty = 0
        except OSError:
            pass


_shared: ProbeCache | None = None
_shared_lock = threading.Lock()


def shared_cache() -> ProbeCache:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ProbeCache()
            atexit.register(_shared.save)
        return _shared


def cached_probe_duration(path: Path, prober, cache: ProbeCache | None = None) -> float:
    """Return cached duration or call prober(path) and store the result."""
    cache = cache or shared_cache()
    hit = cache.get(path)
    if hit is not None:
        return hit
    duration = prober(path)
    cache.put(path, duration)
    return duration
