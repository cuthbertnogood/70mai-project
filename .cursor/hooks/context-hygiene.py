#!/usr/bin/env python3
"""Enforce context-hygiene limits on Write / StrReplace tool calls.

preToolUse: deny oversized full-file writes and huge patches.
postToolUse: track per-conversation edit counts; warn on hot-file churn.
sessionStart: inject short hygiene reminders into new chats.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ~500 tokens ≈ 2000 chars; hard deny above these sizes.
STRREPLACE_SOFT_CHARS = 2000
STRREPLACE_HARD_CHARS = 4000
WRITE_EXISTING_HARD_CHARS = 8000
HOT_FILE_EDIT_WARN = 8

STATE_DIRNAME = ".cursor/hooks/state"
STATE_FILENAME = "edit-counts.json"


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def tool_input_dict(data: dict[str, Any]) -> dict[str, Any]:
    tip = data.get("tool_input")
    if isinstance(tip, dict):
        return tip
    if isinstance(tip, str) and tip.strip():
        try:
            parsed = json.loads(tip)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def resolve_repo_root(data: dict[str, Any]) -> Path:
    roots = data.get("workspace_roots")
    if isinstance(roots, list) and roots:
        return Path(str(roots[0]))
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path.cwd()


def state_path(repo: Path) -> Path:
    return repo / STATE_DIRNAME / STATE_FILENAME


def load_state(repo: Path) -> dict[str, Any]:
    path = state_path(repo)
    if not path.is_file():
        return {"conversations": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"conversations": {}}
    if not isinstance(data, dict):
        return {"conversations": {}}
    conv = data.get("conversations")
    if not isinstance(conv, dict):
        data["conversations"] = {}
    return data


def save_state(repo: Path, state: dict[str, Any]) -> None:
    path = state_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def rel_path(repo: Path, path_str: str) -> str:
    try:
        p = Path(path_str)
        if p.is_absolute():
            return str(p.relative_to(repo))
        return path_str
    except ValueError:
        return path_str


def file_exists(repo: Path, path_str: str) -> bool:
    p = Path(path_str)
    if not p.is_absolute():
        p = repo / p
    return p.is_file()


def handle_session_start(_data: dict[str, Any]) -> None:
    emit(
        {
            "additional_context": (
                "Context hygiene (auto): prefer small StrReplace patches; "
                "do not Write full bodies of existing files; batch README/GOALS "
                "once at end of the task; for a new workstream ask the user to "
                "start a fresh chat. Oversized Write/StrReplace may be blocked by hooks."
            )
        }
    )


def handle_pre_tool_use(data: dict[str, Any]) -> None:
    tool = str(data.get("tool_name") or "")
    tip = tool_input_dict(data)
    repo = resolve_repo_root(data)
    path = str(tip.get("path") or "")

    if tool == "Write":
        contents = tip.get("contents")
        if not isinstance(contents, str):
            emit({"permission": "allow"})
            return
        size = len(contents)
        exists = bool(path) and file_exists(repo, path)
        if exists and size >= WRITE_EXISTING_HARD_CHARS:
            emit(
                {
                    "permission": "deny",
                    "user_message": (
                        f"Blocked full-file Write to existing file "
                        f"({size:,} chars). Use StrReplace instead."
                    ),
                    "agent_message": (
                        f"Context hygiene: refused Write of existing file "
                        f"`{rel_path(repo, path)}` ({size:,} chars). "
                        f"Apply a targeted StrReplace (keep new_string under "
                        f"~{STRREPLACE_HARD_CHARS} chars). Only Write when creating "
                        f"a new file or the user explicitly asks for a full rewrite."
                    ),
                }
            )
            return
        emit({"permission": "allow"})
        return

    if tool == "StrReplace":
        new_string = tip.get("new_string")
        if not isinstance(new_string, str):
            emit({"permission": "allow"})
            return
        size = len(new_string)
        if size >= STRREPLACE_HARD_CHARS:
            emit(
                {
                    "permission": "deny",
                    "user_message": (
                        f"Blocked oversized StrReplace ({size:,} chars). "
                        f"Split into smaller patches."
                    ),
                    "agent_message": (
                        f"Context hygiene: refused StrReplace on "
                        f"`{rel_path(repo, path) if path else '(unknown)'}` "
                        f"because new_string is {size:,} chars "
                        f"(limit {STRREPLACE_HARD_CHARS}). Split into several "
                        f"smaller StrReplace calls (prefer under "
                        f"{STRREPLACE_SOFT_CHARS} chars each)."
                    ),
                }
            )
            return
        # Soft limit: allow, but agent_message is only documented for deny.
        # Soft guidance is injected via postToolUse additional_context instead.
        emit({"permission": "allow"})
        return

    emit({"permission": "allow"})


def handle_post_tool_use(data: dict[str, Any]) -> None:
    tool = str(data.get("tool_name") or "")
    if tool not in {"Write", "StrReplace"}:
        emit({})
        return

    tip = tool_input_dict(data)
    path = str(tip.get("path") or "")
    if not path:
        emit({})
        return

    repo = resolve_repo_root(data)
    rel = rel_path(repo, path)
    conversation_id = str(data.get("conversation_id") or data.get("session_id") or "default")

    state = load_state(repo)
    conversations: dict[str, Any] = state.setdefault("conversations", {})
    conv: dict[str, Any] = conversations.setdefault(conversation_id, {"files": {}})
    files: dict[str, Any] = conv.setdefault("files", {})
    entry = files.setdefault(rel, {"edits": 0, "chars": 0})
    entry["edits"] = int(entry.get("edits") or 0) + 1

    chunk = tip.get("new_string") if tool == "StrReplace" else tip.get("contents")
    if isinstance(chunk, str):
        entry["chars"] = int(entry.get("chars") or 0) + len(chunk)
        soft_note = ""
        if tool == "StrReplace" and len(chunk) >= STRREPLACE_SOFT_CHARS:
            soft_note = (
                f" Last StrReplace new_string was {len(chunk):,} chars "
                f"(soft target <{STRREPLACE_SOFT_CHARS}). Prefer smaller patches."
            )
    else:
        soft_note = ""

    save_state(repo, state)
    edits = int(entry["edits"])

    notes: list[str] = []
    if soft_note:
        notes.append(soft_note.strip())
    if edits >= HOT_FILE_EDIT_WARN:
        notes.append(
            f"`{rel}` has been edited {edits} times in this chat. "
            f"Finish remaining changes with minimal patches, or ask the user "
            f"to continue in a new chat to free context."
        )

    if notes:
        emit({"additional_context": "Context hygiene: " + " ".join(notes)})
    else:
        emit({})


def main() -> None:
    data = read_input()
    event = str(data.get("hook_event_name") or os.environ.get("CURSOR_HOOK_EVENT") or "")

    if event == "sessionStart":
        handle_session_start(data)
    elif event == "preToolUse":
        handle_pre_tool_use(data)
    elif event == "postToolUse":
        handle_post_tool_use(data)
    else:
        # Unknown / empty: fail open
        if "tool_name" in data:
            emit({"permission": "allow"})
        else:
            emit({})


if __name__ == "__main__":
    main()
