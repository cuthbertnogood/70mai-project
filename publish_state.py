#!/usr/bin/env python3
"""Publish state on SD card — portable upload progress across hosts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from import_70mai import log

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_CREDENTIALS_CANDIDATES = (
    PROJECT_ROOT / "youtube_credentials.json",
    PROJECT_ROOT / "config" / "youtube_credentials.json",
)

SD_ROOT_DIR = ".70mai"
SD_PUBLISH_DIR = ".70mai/publish"
SD_AUTH_DIR = ".70mai/auth"
SD_SESSIONS_SUBDIR = "sessions"
CREDENTIALS_FILENAME = "youtube_credentials.json"
TOKEN_FILENAME = "youtube_token.json"
LOCAL_CONFIG_DIR = Path.home() / ".config/70mai"

SD_README = """70mai portable data (auto-generated, refreshed on every run)
=============================================================

auth/youtube_credentials.json      — OAuth Desktop client from Google Cloud (~1 KB)
auth/youtube_token.json            — YouTube refresh token after browser login (~1 KB)

publish/publish_Normal.state.json  — uploaded trips + YouTube video_id / URL
publish/publish_Event.state.json   — same for the merged Event video
publish/sessions/*.upload.json     — resume interrupted uploads (~few KB each)

import/card_inventory.json         — trips, date range, per-clip YouTube links
import/import_*.state.json         — per-file merge status (host video/Output/)
import/CARD_SUMMARY.txt            — human-readable card overview + YouTube URLs
import/CARD_STORAGE.txt            — video/non-video sizes on card + disk free
import/card_storage.json           — same (machine-readable)

Insert this SD card on any Mac with the project + run:
  ./scripts/publish_all_70mai.sh --wait

What autopilot does:
  Normal — merge clips into trips, compose 2-cam (Front over Back), one
           YouTube video per trip; Event — ALL events on the card become
           ONE merged 2-cam YouTube video.
  Already-uploaded trips are skipped (state above); interrupted uploads
  resume mid-file. YouTube API quota is ~6 uploads/day — extra trips are
  picked up automatically on the next day's run.

Autopilot creates this folder on first use (OAuth from ~/.config/70mai/ or project).
Raw clips stay on the card and are never modified; merged/composed MP4s are
temporary on the host and deleted after upload.

Every YouTube link for every clip: see import/CARD_SUMMARY.txt or the
clip_youtube map in import/card_inventory.json.

SECURITY: auth/youtube_token.json grants upload access to your YouTube account.
Keep the card private; revoke access at https://myaccount.google.com/permissions if lost.
Use --no-auth-on-sd to keep OAuth only on the host.
"""


def state_filename(label: str) -> str:
    safe = label.replace(" ", "_").replace("/", "-")
    return f"publish_{safe}.state.json"


def sd_publish_dir(source: Path) -> Path:
    return source.resolve() / SD_PUBLISH_DIR


def sd_state_path(source: Path, label: str) -> Path:
    return sd_publish_dir(source) / state_filename(label)


def local_state_path(temp_dir: Path, label: str) -> Path:
    return temp_dir / state_filename(label)


def sd_session_dir(source: Path) -> Path:
    return sd_publish_dir(source) / SD_SESSIONS_SUBDIR


def sd_auth_dir(source: Path) -> Path:
    return source.resolve() / SD_AUTH_DIR


def sd_credentials_path(source: Path) -> Path:
    return sd_auth_dir(source) / CREDENTIALS_FILENAME


def sd_token_path(source: Path) -> Path:
    return sd_auth_dir(source) / TOKEN_FILENAME


def local_credentials_path() -> Path:
    return LOCAL_CONFIG_DIR / CREDENTIALS_FILENAME


def local_token_path() -> Path:
    return LOCAL_CONFIG_DIR / TOKEN_FILENAME


def sd_root_dir(source: Path) -> Path:
    return source.resolve() / SD_ROOT_DIR


def _trip_key(part: dict) -> tuple:
    return (
        part.get("record_type"),
        part.get("chunk_index"),
        part.get("trip_index"),
    )


def _chunk_key(part: dict) -> tuple:
    return (part.get("record_type"), part.get("index"))


def _merge_parts(
    sd_parts: list[dict],
    local_parts: list[dict],
    key_fn,
) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    for part in local_parts + sd_parts:
        key = key_fn(part)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = part
            continue
        if part.get("uploaded") and not existing.get("uploaded"):
            by_key[key] = part
        elif part.get("uploaded") and existing.get("uploaded"):
            if part.get("video_id") and not existing.get("video_id"):
                by_key[key] = part
            elif part.get("youtube_url") and not existing.get("youtube_url"):
                by_key[key] = part
    return list(by_key.values())


def merge_publish_state(sd: dict, local: dict) -> dict:
    """Merge SD + local state; SD wins ties on uploaded trips."""
    merged = dict(local)
    for key in ("source", "types", "chunk_minutes", "chunk_mode", "updated_at"):
        if sd.get(key) is not None:
            merged[key] = sd[key]
    merged["trip_parts"] = _merge_parts(
        sd.get("trip_parts", []),
        local.get("trip_parts", []),
        _trip_key,
    )
    merged["parts"] = _merge_parts(
        sd.get("parts", []),
        local.get("parts", []),
        _chunk_key,
    )
    if sd.get("playlist_id"):
        merged["playlist_id"] = sd["playlist_id"]
    elif local.get("playlist_id"):
        merged["playlist_id"] = local["playlist_id"]
    return merged


def load_state_file(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_sd_readme(source: Path) -> None:
    """Write /.70mai/README.txt; refresh it when the project text changes."""
    readme = sd_root_dir(source) / "README.txt"
    try:
        readme.parent.mkdir(parents=True, exist_ok=True)
        current = readme.read_text(encoding="utf-8") if readme.is_file() else None
        if current != SD_README:
            readme.write_text(SD_README, encoding="utf-8")
            if current is not None:
                log(f"Refreshed SD README: {readme}")
    except OSError:
        pass


def sd_is_new_card(source: Path) -> bool:
    """True when .70mai/ has never been created on this card."""
    return not sd_root_dir(source).is_dir()


def _credentials_search_paths() -> list[Path]:
    paths = [local_credentials_path(), *PROJECT_CREDENTIALS_CANDIDATES]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.resolve()) if path.is_file() else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _copy_credentials_to_sd(source: Path) -> Path | None:
    """Copy OAuth client JSON onto SD from host or project. Returns SD path if ready."""
    sd_creds = sd_credentials_path(source)
    if sd_creds.is_file():
        return sd_creds
    for candidate in _credentials_search_paths():
        if not candidate.is_file():
            continue
        try:
            sd_auth_dir(source).mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, sd_creds)
            log(f"Migrating credentials → SD: {sd_creds} (from {candidate})")
            return sd_creds
        except OSError as exc:
            raise RuntimeError(f"Cannot copy credentials to SD: {sd_creds} ({exc})") from exc
    return None


def _credentials_missing_message(sd_creds: Path) -> str:
    lines = [
        "OAuth credentials not found on SD or host.",
        f"  SD: {sd_creds}",
        "  Searched:",
    ]
    for path in _credentials_search_paths():
        lines.append(f"    {path}")
    lines.extend(
        [
            "",
            "One-time setup:",
            "  1. Google Cloud Console → enable YouTube Data API v3",
            "  2. OAuth consent screen → add your Google account as test user",
            "  3. Credentials → OAuth client ID → Desktop app → download JSON",
            f"  4. Save as {local_credentials_path()}",
            "     (autopilot will copy it to the SD card on next run)",
        ]
    )
    return "\n".join(lines)


class AuthStore:
    """Resolve YouTube OAuth paths on SD card (portable) or local host."""

    @staticmethod
    def resolve(source: Path, *, auth_on_sd: bool) -> tuple[Path, Path]:
        local_creds = local_credentials_path()
        local_token = local_token_path()
        if not auth_on_sd:
            log(f"OAuth (local): {LOCAL_CONFIG_DIR}")
            if not local_creds.is_file():
                raise FileNotFoundError(_credentials_missing_message(local_creds))
            return local_creds, local_token

        sd_creds = sd_credentials_path(source)
        sd_token = sd_token_path(source)
        try:
            sd_auth_dir(source).mkdir(parents=True, exist_ok=True)
            ensure_sd_readme(source)
        except OSError as exc:
            raise RuntimeError(f"Cannot create SD auth dir: {sd_auth_dir(source)} ({exc})") from exc

        if not sd_creds.is_file():
            copied = _copy_credentials_to_sd(source)
            if copied is None:
                raise FileNotFoundError(_credentials_missing_message(sd_creds))

        if not sd_token.is_file() and local_token.is_file():
            shutil.copy2(local_token, sd_token)
            log(f"Migrating token → SD: {sd_token}")

        log(f"OAuth (SD): {sd_auth_dir(source)}")
        return sd_creds, sd_token

    @staticmethod
    def ensure_ready(
        source: Path,
        label: str,
        *,
        auth_on_sd: bool,
        state_on_sd: bool,
        types: list[str],
        dry_run: bool = False,
    ) -> tuple[Path, Path]:
        """Bootstrap a fresh SD card: .70mai layout, OAuth, empty publish state."""
        new_card = sd_is_new_card(source)
        creds, token = AuthStore.resolve(source, auth_on_sd=auth_on_sd)

        if state_on_sd:
            try:
                sd_publish_dir(source).mkdir(parents=True, exist_ok=True)
                sd_session_dir(source).mkdir(parents=True, exist_ok=True)
                ensure_sd_readme(source)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot create SD publish dir: {sd_publish_dir(source)} ({exc})"
                ) from exc

            for record_type in types:
                sd_path = sd_state_path(source, record_type)
                if dry_run:
                    if not sd_path.is_file():
                        log(
                            f"Dry-run: would initialize publish state on SD: {sd_path}"
                        )
                    continue
                if not sd_path.is_file():
                    save_state_file(
                        sd_path,
                        {
                            "source": str(source.resolve()),
                            "types": [record_type],
                            "trip_parts": [],
                            "parts": [],
                        },
                    )
                    log(f"Initialized publish state on SD: {sd_path}")

        needs_oauth = not dry_run and not token.is_file()
        if new_card or needs_oauth:
            log("")
            if new_card:
                log("=== New SD card — first-time setup ===")
                log(f"  Card: {source}")
                log("  Creating .70mai/ (OAuth + publish state on card)")
            if needs_oauth:
                log("YouTube OAuth: browser login required (one-time per card/account)")
            elif new_card:
                log("YouTube OAuth: token copied from host")

        if needs_oauth:
            from youtube_upload import load_credentials

            load_credentials(creds, token)
            log(f"OAuth ready: {token}")
        elif new_card:
            log("SD card ready for autopilot")
            log("")

        return creds, token

    @staticmethod
    def sync_token(primary: Path) -> None:
        """Mirror token from SD (primary) to local cache."""
        mirror = local_token_path()
        if not primary.is_file():
            return
        try:
            if primary.resolve() == mirror.resolve():
                return
            mirror.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(primary, mirror)
        except OSError:
            pass


class StateStore:
    """Read/write publish state on host + SD card (SD is portable source of truth)."""

    def __init__(
        self,
        source: Path,
        temp_dir: Path,
        label: str,
        *,
        state_on_sd: bool,
    ) -> None:
        self.source = source.resolve()
        self.temp_dir = temp_dir
        self.label = label
        self.state_on_sd = state_on_sd
        self.local_path = local_state_path(temp_dir, label)
        self.sd_path = sd_state_path(source, label) if state_on_sd else None

    @property
    def primary_path(self) -> Path:
        if self.state_on_sd and self.sd_path is not None:
            return self.sd_path
        return self.local_path

    @property
    def session_dir(self) -> Path:
        if self.state_on_sd:
            return sd_session_dir(self.source)
        return self.temp_dir

    def load(self, *, resume: bool, quiet: bool = False) -> dict:
        if not resume:
            return {}
        local = load_state_file(self.local_path)
        if not self.state_on_sd or self.sd_path is None:
            if local and not quiet:
                log(f"State (local): {self.local_path}")
            return local

        sd = load_state_file(self.sd_path)
        if sd and local:
            merged = merge_publish_state(sd, local)
            if not quiet:
                log(
                    f"State merged: SD {self.sd_path} + local "
                    f"({len(merged.get('trip_parts', []))} trip record(s))"
                )
        elif sd:
            merged = sd
            if not quiet:
                log(
                    f"State (SD): {self.sd_path} "
                    f"({len(sd.get('trip_parts', []))} trip record(s))"
                )
        elif local:
            merged = local
            if not quiet:
                log(f"Migrating local state → SD: {self.sd_path}")
        else:
            return {}

        return merged

    def save(self, data: dict) -> None:
        from datetime import datetime, timezone

        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state_file(self.local_path, data)
        if not self.state_on_sd or self.sd_path is None:
            return
        try:
            ensure_sd_readme(self.source)
            save_state_file(self.sd_path, data)
        except OSError as exc:
            log(f"Warning: cannot write state to SD ({exc})")

    def uploaded_count(self) -> int:
        data = merge_publish_state(
            load_state_file(self.sd_path) if self.sd_path else {},
            load_state_file(self.local_path),
        )
        return sum(1 for p in data.get("trip_parts", []) if p.get("uploaded"))


def youtube_watch_url(video_id: str | None) -> str | None:
    if not video_id:
        return None
    return f"https://youtu.be/{video_id}"


def build_global_trip_upload_map(
    publish_state: dict,
    chunks: list,
) -> dict[tuple[str, int], dict]:
    """Map (record_type, global_trip_index) -> upload metadata from publish state."""
    result: dict[tuple[str, int], dict] = {}
    for chunk in chunks:
        record_type = chunk.record_type
        for trip_idx, trip in enumerate(chunk.trips, start=1):
            entry = None
            for part in publish_state.get("trip_parts", []):
                if (
                    part.get("record_type") == record_type
                    and part.get("chunk_index") == chunk.index
                    and part.get("trip_index") == trip_idx
                ):
                    entry = part
                    break
            if not entry or not entry.get("uploaded"):
                continue
            video_id = entry.get("video_id")
            result[(record_type, trip.index)] = {
                "video_id": video_id,
                "youtube_url": entry.get("youtube_url")
                or youtube_watch_url(video_id),
                "chunk_index": chunk.index,
                "trip_index_in_chunk": trip_idx,
            }
    return result


def build_clip_youtube_catalog(
    source: Path,
    record_types: list[str],
    *,
    session_gap: float,
    publish_state: dict,
    chunks: list,
) -> dict[str, dict[str, dict[str, dict]]]:
    """Per SD clip filename -> YouTube link (same URL for all clips in one trip)."""
    from import_70mai import scan_clips, split_sessions

    trip_map = build_global_trip_upload_map(publish_state, chunks)
    catalog: dict[str, dict[str, dict[str, dict]]] = {}

    def trip_index_for_clip(clip, front_sessions: list[list]) -> int | None:
        ts = clip.timestamp
        for trip_idx, session in enumerate(front_sessions, start=1):
            if session[0].timestamp <= ts <= session[-1].timestamp:
                return trip_idx
        return None

    for record_type in record_types:
        catalog[record_type] = {}
        front_clips = scan_clips(source, [record_type], ["Front"], warn=False)
        if not front_clips:
            continue

        if record_type in ("Event", "Parking"):
            def trip_index_fn(_clip):
                return 1

        else:
            front_sessions = split_sessions(front_clips, session_gap)

            def trip_index_fn(clip, _sessions=front_sessions):
                return trip_index_for_clip(clip, _sessions)

        for camera in ("Front", "Back"):
            clips = (
                front_clips
                if camera == "Front"
                else scan_clips(source, [record_type], ["Back"], warn=False)
            )
            if not clips:
                continue
            by_name: dict[str, dict] = {}
            for clip in clips:
                trip_idx = trip_index_fn(clip)
                upload = trip_map.get((record_type, trip_idx), {}) if trip_idx else {}
                by_name[clip.path.name] = {
                    "trip_index": trip_idx,
                    "camera": camera,
                    "timestamp": clip.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "video_id": upload.get("video_id"),
                    "youtube_url": upload.get("youtube_url"),
                    "chunk_index": upload.get("chunk_index"),
                }
            catalog[record_type][camera] = by_name
    return catalog
