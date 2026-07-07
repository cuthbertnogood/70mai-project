#!/usr/bin/env python3
"""Watch directory for new MP4 files and auto-upload to YouTube.

Monitors a directory for completed MP4 files, auto-uploads them via YouTube
Data API v3, and tracks state across restarts.

Usage:
  # One-shot
  watch_upload_70mai.py --watch video/Output --once

  # Daemon (default)
  watch_upload_70mai.py --watch video/Output

  # Generate default config
  watch_upload_70mai.py --init-config --watch video/Output
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

from youtube_upload import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    YouTubeUploadError,
    upload_session_path_for_file,
    upload_video,
)
from youtube_upload_diagnostics import DEFAULT_DIAG_LOG

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config/70mai"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "watch_upload.json"
DEFAULT_STATE_PATH = CONFIG_DIR / "watch_upload_state.json"

WATCH_EVERY_SEC = 300
STABILITY_SEC = 30
RETRY_MAX = 3
RETRY_DELAY_BASE = 300
DAILY_QUOTA = 6
TITLE_TEMPLATE = "70mai {date} — {stem}"


@dataclass
class Config:
    watch_dir: str = "video/Output"
    interval_sec: int = WATCH_EVERY_SEC
    stability_sec: int = STABILITY_SEC
    title_template: str = TITLE_TEMPLATE
    description: str = ""
    tags: list[str] = field(default_factory=lambda: ["70mai", "dashcam"])
    privacy: str = "private"
    category_id: str = "22"
    retry_max: int = RETRY_MAX
    retry_delay_base: int = RETRY_DELAY_BASE
    quota_daily: int = DAILY_QUOTA
    credentials: str = str(DEFAULT_CREDENTIALS)
    token: str = str(DEFAULT_TOKEN)
    diag_log: str = str(DEFAULT_DIAG_LOG)
    telegram_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or DEFAULT_CONFIG_PATH
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            keys = cls.__dataclass_fields__
            return cls(**{k: v for k, v in raw.items() if k in keys})
        return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or DEFAULT_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(self, k) for k in self.__dataclass_fields__}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class StateManager:
    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"files": {}, "daily_uploads": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, path: str) -> dict | None:
        return self.data["files"].get(path)

    def set(self, path: str, info: dict) -> None:
        self.data["files"][path] = info
        self.save()

    def remove(self, path: str) -> None:
        self.data["files"].pop(path, None)
        self.save()

    def list_status(self, *statuses: str) -> list[tuple[str, dict]]:
        return [(p, i) for p, i in self.data["files"].items()
                if i.get("status") in statuses]

    def daily_count(self, day: str | None = None) -> int:
        return self.data.get("daily_uploads", {}).get(day or date.today().isoformat(), 0)

    def increment_daily(self) -> None:
        day = date.today().isoformat()
        self.data.setdefault("daily_uploads", {})
        self.data["daily_uploads"][day] = self.data["daily_uploads"].get(day, 0) + 1
        self.save()

    def known_paths(self) -> set[str]:
        return set(self.data["files"].keys())


def _notify(config: Config, message: str) -> None:
    if config.telegram_token and config.telegram_chat_id:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{config.telegram_token}/sendMessage",
                json={"chat_id": config.telegram_chat_id, "text": message, "parse_mode": "HTML"},
                timeout=15,
            )
        except Exception as exc:
            log.warning("Telegram notify failed: %s", exc)
    if config.webhook_url:
        try:
            import requests
            requests.post(config.webhook_url, json={"text": message}, timeout=15)
        except Exception as exc:
            log.warning("Webhook notify failed: %s", exc)


def _notify_success(config: Config, path: str, title: str, video_id: str) -> None:
    _notify(config, (
        f"\u2705 Uploaded: {title}\n"
        f"\U0001f4c1 {path}\n"
        f"\U0001f517 https://youtu.be/{video_id}"
    ))


def _notify_failed(config: Config, path: str, title: str, error: str, attempts: int) -> None:
    _notify(config, (
        f"\u274c Upload failed: {title}\n"
        f"\U0001f4c1 {path}\n"
        f"\u26a0 {error}\n"
        f"\U0001f504 Attempts: {attempts}"
    ))


def _notify_quota(config: Config, day: str, count: int) -> None:
    _notify(config, (
        f"\u23f8 Daily limit reached: {count} videos on {day}\n"
        "Pausing until midnight."
    ))


class WatchDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.state = StateManager()
        self.watch_dir = Path(config.watch_dir).resolve()
        self.running = True

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum: int, frame: object) -> None:
        log.info("Signal %d received, shutting down...", signum)
        self.running = False

    def run(self, *, once: bool = False) -> None:
        log.info("Watching %s (every %ds)", self.watch_dir, self.config.interval_sec)
        log.info("Daily quota: %d, retry: max %d, base %ds",
                 self.config.quota_daily, self.config.retry_max, self.config.retry_delay_base)
        if self.config.telegram_token:
            log.info("Telegram notifications enabled")
        if self.config.webhook_url:
            log.info("Webhook notifications enabled")

        while self.running:
            self._process_cycle()
            if once or not self.running:
                break
            for _ in range(self.config.interval_sec):
                if not self.running:
                    break
                time.sleep(1)

    def _process_cycle(self) -> None:
        self._scan_new()
        self._upload_pending()

    def _scan_new(self) -> None:
        if not self.watch_dir.is_dir():
            return

        current = set()
        for mp4 in sorted(self.watch_dir.glob("*.mp4")):
            sp = str(mp4.resolve())
            current.add(sp)

            if sp in self.state.known_paths():
                continue

            log.info("New: %s (%s)", mp4.name, _format_size(mp4.stat().st_size))
            self.state.set(sp, {
                "status": "pending",
                "size": mp4.stat().st_size,
                "size_checked_at": time.monotonic(),
                "mtime": mp4.stat().st_mtime,
                "attempts": 0,
                "last_error": None,
                "video_id": None,
                "uploaded_at": None,
            })

        for sp in list(self.state.data["files"].keys()):
            if sp not in current and self.state.data["files"][sp].get("status") == "uploaded":
                self.state.remove(sp)

    def _upload_pending(self) -> None:
        pending = self.state.list_status("pending", "failed")
        quota_left = max(0, self.config.quota_daily - self.state.daily_count())

        for sp, info in pending:
            if not self.running:
                break
            if quota_left <= 0:
                _notify_quota(self.config, date.today().isoformat(), self.config.quota_daily)
                break

            path = Path(sp)
            if not path.is_file():
                continue
            if not self._is_stable(path, info):
                continue

            self._upload_file(path, info)
            quota_left = max(0, self.config.quota_daily - self.state.daily_count())

        self._cleanup_old()

    def _is_stable(self, path: Path, info: dict) -> bool:
        now = time.monotonic()
        try:
            size = path.stat().st_size
        except OSError:
            return False

        if info.get("size") == size:
            elapsed = now - info.get("size_checked_at", now)
            return elapsed >= self.config.stability_sec

        info["size"] = size
        info["size_checked_at"] = now
        self.state.set(str(path), info)
        return False

    def _upload_file(self, path: Path, info: dict) -> None:
        sp = str(path)
        stem = path.stem
        title = self.config.title_template.format(
            date=datetime.now().strftime("%Y-%m-%d"),
            stem=stem,
            filename=path.name,
        )
        attempts = info.get("attempts", 0) + 1
        info["attempts"] = attempts
        info["status"] = "uploading"
        self.state.set(sp, info)

        log.info("Upload [%d/%d]: %s", attempts, self.config.retry_max, title)
        session_path = upload_session_path_for_file(path)

        try:
            video_id = upload_video(
                path,
                title=title,
                description=self.config.description,
                tags=self.config.tags or None,
                privacy=self.config.privacy,
                category_id=self.config.category_id,
                credentials_path=Path(self.config.credentials),
                token_path=Path(self.config.token),
                session_path=session_path,
                resume=True,
                diag_log=Path(self.config.diag_log) if self.config.diag_log else None,
                on_progress=_progress_logger(),
            )

            info["status"] = "uploaded"
            info["video_id"] = video_id
            info["uploaded_at"] = datetime.now(timezone.utc).isoformat()
            info["last_error"] = None
            self.state.set(sp, info)
            self.state.increment_daily()

            log.info("Done: https://youtu.be/%s", video_id)
            _notify_success(self.config, sp, title, video_id)

            if session_path.is_file():
                session_path.unlink(missing_ok=True)

        except YouTubeUploadError as exc:
            error_msg = str(exc)
            info["last_error"] = error_msg

            if attempts >= self.config.retry_max:
                info["status"] = "failed"
                log.error("Failed after %d attempts: %s", attempts, error_msg)
                _notify_failed(self.config, sp, title, error_msg, attempts)
            else:
                info["status"] = "pending"
                delay = self.config.retry_delay_base * (2 ** (attempts - 1))
                log.warning("Attempt %d/%d failed, retry in %ds: %s",
                            attempts, self.config.retry_max, delay, error_msg)
                info["size_checked_at"] = time.monotonic() + delay - self.config.stability_sec

            self.state.set(sp, info)

    def _cleanup_old(self, max_days: int = 7) -> None:
        now = datetime.now(timezone.utc)
        for sp, info in list(self.state.data["files"].items()):
            if info.get("status") != "uploaded":
                continue
            ts = info.get("uploaded_at")
            if not ts:
                continue
            try:
                if (now - datetime.fromisoformat(ts)).days > max_days:
                    self.state.remove(sp)
            except (ValueError, TypeError):
                pass


def _progress_logger():
    last = [-1]
    def prog(pct: int) -> None:
        if pct >= last[0] + 5 or pct == 100:
            log.info("  upload %d%%", pct)
            last[0] = pct
    return prog


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch directory for MP4 files and auto-upload to YouTube",
    )
    parser.add_argument("--watch", "-w", help="Directory to monitor")
    parser.add_argument("--every", type=int, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Scan and upload once, then exit")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Config path")
    parser.add_argument("--title-template", help="Title template: {date} {stem} {filename}")
    parser.add_argument("--description", help="Video description")
    parser.add_argument("--tags", help="Comma-separated tags")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"))
    parser.add_argument("--init-config", action="store_true", help="Generate default config and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.init_config:
        cfg = Config()
        if args.watch:
            cfg.watch_dir = args.watch
        cfg.save(args.config)
        print(f"Config saved: {args.config}")
        print(f"Edit it, then run: python3 {sys.argv[0]} --watch {cfg.watch_dir}")
        return

    config = Config.load(args.config)
    if args.watch:
        config.watch_dir = args.watch
    if args.every is not None:
        config.interval_sec = args.every
    if args.title_template is not None:
        config.title_template = args.title_template
    if args.description is not None:
        config.description = args.description
    if args.tags is not None:
        config.tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.privacy is not None:
        config.privacy = args.privacy

    watch_path = Path(config.watch_dir)
    if not watch_path.is_dir():
        parser.error(f"Watch directory not found: {watch_path}")

    log.info("Mode: %s | watch: %s | interval: %ds",
             "one-shot" if args.once else "daemon", config.watch_dir, config.interval_sec)

    WatchDaemon(config).run(once=args.once)


if __name__ == "__main__":
    from project_env import ensure_venv_python
    ensure_venv_python()
    main()
