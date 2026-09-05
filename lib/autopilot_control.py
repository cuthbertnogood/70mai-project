#!/usr/bin/env python3
"""Control channel between Autopilot web UI and publish_all."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

CONTROL_FILENAME = "autopilot_control.json"
RUN_STATE_FILENAME = "autopilot_run_state.json"
DEFERRED_FILENAME = "autopilot_deferred_chunks.json"

EXIT_USER_STOP = 3
EXIT_SKIP_CHUNK = 4
EXIT_REPAIR_RETRY = 5

VALID_COMMANDS = frozenset({"stop", "skip", "repair", "quit", "profile"})


def control_path(temp_dir: Path) -> Path:
    return temp_dir / CONTROL_FILENAME


def run_state_path(temp_dir: Path) -> Path:
    return temp_dir / RUN_STATE_FILENAME


def deferred_path(temp_dir: Path) -> Path:
    return temp_dir / DEFERRED_FILENAME


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_run_state(
    temp_dir: Path,
    *,
    phase: str,
    message: str = "",
    child_pid: int | None = None,
    stop_requested: bool = False,
    pipeline_exit: int | None = None,
    sd_path: str | None = None,
    dashboard_url: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "phase": phase,
        "message": message,
        "updated_at": time.time(),
        "stop_requested": stop_requested,
    }
    if child_pid is not None:
        payload["child_pid"] = child_pid
    if pipeline_exit is not None:
        payload["pipeline_exit"] = pipeline_exit
    if sd_path is not None:
        payload["sd_path"] = sd_path
    if dashboard_url is not None:
        payload["dashboard_url"] = dashboard_url
    _atomic_write(run_state_path(temp_dir), payload)


def read_run_state(temp_dir: Path) -> dict[str, Any]:
    return _read_json(run_state_path(temp_dir))


def write_command(
    temp_dir: Path,
    command: str,
    *,
    record_type: str = "",
    chunk_index: int | None = None,
) -> None:
    if command not in VALID_COMMANDS:
        raise ValueError(f"unknown command: {command}")
    payload: dict[str, Any] = {
        "command": command,
        "ts": time.time(),
    }
    if record_type:
        payload["record_type"] = record_type
    if chunk_index is not None:
        payload["chunk_index"] = chunk_index
    _atomic_write(control_path(temp_dir), payload)


def peek_control(temp_dir: Path) -> dict[str, Any]:
    return _read_json(control_path(temp_dir))


def consume_control(temp_dir: Path) -> dict[str, Any]:
    path = control_path(temp_dir)
    data = _read_json(path)
    if data:
        try:
            path.unlink()
        except OSError:
            pass
    return data


def clear_control(temp_dir: Path) -> None:
    try:
        control_path(temp_dir).unlink()
    except OSError:
        pass


def load_deferred(temp_dir: Path) -> list[dict[str, Any]]:
    raw = _read_json(deferred_path(temp_dir))
    items = raw.get("chunks")
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def defer_chunk(temp_dir: Path, *, record_type: str, chunk_index: int) -> None:
    items = load_deferred(temp_dir)
    key = {"record_type": record_type, "chunk_index": chunk_index}
    if key not in items:
        items.append(key)
    _atomic_write(deferred_path(temp_dir), {"chunks": items})


def is_chunk_deferred(temp_dir: Path, *, record_type: str, chunk_index: int) -> bool:
    for item in load_deferred(temp_dir):
        if (
            item.get("record_type") == record_type
            and int(item.get("chunk_index", -1)) == chunk_index
        ):
            return True
    return False


def clear_deferred_for_run(temp_dir: Path) -> None:
    try:
        deferred_path(temp_dir).unlink()
    except OSError:
        pass


def chunk_key(record_type: str, chunk_index: int) -> str:
    return f"{record_type}:{chunk_index}"


def control_targets_chunk(
    ctrl: dict[str, Any],
    *,
    record_type: str,
    chunk_index: int,
) -> bool:
    rt = str(ctrl.get("record_type") or "")
    ci = ctrl.get("chunk_index")
    if not rt and ci is None:
        return True
    if rt and rt != record_type:
        return False
    if ci is not None and int(ci) != chunk_index:
        return False
    return True


def handle_chunk_control(
    temp_dir: Path,
    *,
    record_type: str,
    chunk_index: int,
    log: Callable[[str], None] | None = None,
) -> tuple[int | None, bool]:
    """Return (exit_code, repair_now). exit_code set → abort run_once."""
    ctrl = peek_control(temp_dir)
    if not ctrl:
        return None, False
    cmd_name = str(ctrl.get("command") or "")
    if cmd_name == "stop":
        consume_control(temp_dir)
        return EXIT_USER_STOP, False
    if cmd_name == "skip":
        if control_targets_chunk(
            ctrl,
            record_type=record_type,
            chunk_index=chunk_index,
        ):
            consume_control(temp_dir)
            defer_chunk(
                temp_dir,
                record_type=record_type,
                chunk_index=chunk_index,
            )
            if log:
                log(
                    f"  Skip chunk {chunk_index} ({record_type}) — "
                    "deferred to next Autopilot run"
                )
            return EXIT_SKIP_CHUNK, False
    if cmd_name == "repair":
        consume_control(temp_dir)
        return None, True
    return None, False
