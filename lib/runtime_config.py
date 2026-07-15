"""Hot-reloadable runtime tunables for import / autopilot.

Edit ``70mai_runtime.json`` (project root) while a run is in progress.
Import re-reads it before each merge job and between camera groups.
Autopilot re-reads it before starting import and between publish steps.

Optional override (takes precedence when present):
  ``video/Output/.publish_tmp/70mai_runtime.json``
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "70mai_runtime.json"
OVERRIDE_CONFIG_PATH = (
    PROJECT_ROOT / "video" / "Output" / ".publish_tmp" / "70mai_runtime.json"
)

DEFAULTS: dict[str, Any] = {
    "import": {
        "chunk_clips": 10,
        "chunk_minutes": 10.0,
        "stage_batch_clips": 10,
        "gap_seconds": 120.0,
        "merge_workers": 1,
        "prefetch": True,
        "prefetch_batches": 2,
        "probe_workers": 8,
        "merge_heartbeat_sec": 30.0,
        "merge_max_attempts": 3,
        "merge_retry_delay_sec": 3.0,
    },
    "autopilot": {
        "publish_chunk_minutes": 120.0,
        "session_gap": 120.0,
        "import_merge_retry_max": 3,
        "import_merge_retry_delay_sec": 15.0,
        "min_free_gb": 20.0,
        "profile": "balanced",
        "prune_merged": "after-compose",
        "sd_poll_sec": 15.0,
    },
}

_lock = threading.RLock()
_cache: dict[str, Any] = deepcopy(DEFAULTS)
_cache_mtime: float | None = None
_cache_path: Path | None = None
_last_logged_fingerprint: str | None = None


def config_paths() -> tuple[Path, Path]:
    """Return (primary tracked defaults, optional live override)."""
    return DEFAULT_CONFIG_PATH, OVERRIDE_CONFIG_PATH


def active_config_path() -> Path:
    if OVERRIDE_CONFIG_PATH.is_file():
        return OVERRIDE_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _fingerprint(cfg: dict[str, Any]) -> str:
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def _load_file(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be an object: {path}")
    return raw


def reload_runtime_config(*, force: bool = False) -> dict[str, Any]:
    """Load config from disk if mtime changed (or force). Returns merged dict."""
    global _cache, _cache_mtime, _cache_path
    path = active_config_path()
    with _lock:
        try:
            mtime = path.stat().st_mtime if path.is_file() else None
        except OSError:
            mtime = None
        if (
            not force
            and _cache_path == path
            and mtime is not None
            and mtime == _cache_mtime
        ):
            return deepcopy(_cache)

        merged = deepcopy(DEFAULTS)
        if path.is_file():
            try:
                merged = _deep_merge(merged, _load_file(path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                # Keep previous cache on bad edit; caller may log.
                merged = deepcopy(_cache)
                merged["_config_error"] = f"{path.name}: {exc}"
        _cache = merged
        _cache_mtime = mtime
        _cache_path = path
        return deepcopy(_cache)


def get_runtime_config(*, force: bool = False) -> dict[str, Any]:
    return reload_runtime_config(force=force)


def import_settings(*, force: bool = False) -> dict[str, Any]:
    cfg = get_runtime_config(force=force)
    section = dict(DEFAULTS["import"])
    section.update(cfg.get("import") or {})
    return section


def autopilot_settings(*, force: bool = False) -> dict[str, Any]:
    cfg = get_runtime_config(force=force)
    section = dict(DEFAULTS["autopilot"])
    section.update(cfg.get("autopilot") or {})
    return section


def log_config_if_changed(log_fn, *, force: bool = False) -> dict[str, Any]:
    """Log a one-liner when import/autopilot tunables change."""
    global _last_logged_fingerprint
    cfg = get_runtime_config(force=force)
    err = cfg.get("_config_error")
    fp = _fingerprint({k: cfg[k] for k in ("import", "autopilot") if k in cfg})
    with _lock:
        changed = fp != _last_logged_fingerprint
        if changed or force:
            _last_logged_fingerprint = fp
            path = active_config_path()
            imp = cfg.get("import") or {}
            log_fn(
                f"Runtime config ({path.name}): "
                f"chunk_clips={imp.get('chunk_clips')} "
                f"stage_batch={imp.get('stage_batch_clips')} "
                f"chunk_min={imp.get('chunk_minutes')} "
                f"workers={imp.get('merge_workers')} "
                f"prefetch={imp.get('prefetch')}"
            )
            if err:
                log_fn(f"Runtime config warning: {err}")
    return cfg


def ensure_default_config_file() -> Path:
    """Write default JSON if the project file is missing."""
    path = DEFAULT_CONFIG_PATH
    if not path.is_file():
        path.write_text(
            json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return path
