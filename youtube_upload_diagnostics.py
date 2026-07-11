#!/usr/bin/env python3
"""Structured diagnostics for YouTube resumable uploads (JSONL log + analysis)."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DIAG_LOG = Path("video/Output/.publish_tmp/youtube_upload.diag.jsonl")

# Known failure patterns → remediation hints for humans and future script tuning.
RECOMMENDATIONS: dict[str, str] = {
    "network_timeout": (
        "Repeated network timeouts. Check Wi‑Fi/VPN; upload uses 600 s per request. "
        "Retry with --resume-upload; session URI is saved in .upload.json."
    ),
    "session_expired": (
        "YouTube session URI expired (404). Start a fresh upload or rerun without "
        "--resume-upload. Resume within a few hours of the interruption."
    ),
    "proxy_redirect": (
        "Proxy/VPN redirect error (RedirectMissingLocation). Upload ignores system "
        "proxy (trust_env=False); disable VPN or split tunnel if this persists."
    ),
    "rate_limit": (
        "HTTP 429 or quota errors. YouTube allows ~6 uploads/day on default quota. "
        "Wait 24 h or request quota increase in Google Cloud Console."
    ),
    "server_error": (
        "YouTube server errors (5xx). Transient; retries with backoff are automatic. "
        "If persistent, check Google API status and retry --resume-upload."
    ),
    "process_interrupted": (
        "Upload stopped before 100% with no terminal error. Process may have been "
        "killed. Rerun with --resume-upload to continue from saved offset."
    ),
    "slow_throughput": (
        "Average throughput below ~1 MB/s. Large 2 GB files need stable connection; "
        "consider wired network or upload overnight with --resume-upload."
    ),
    "chunk_rejected": (
        "HTTP 400 on chunk upload. A rejected saved resumable session is now "
        "discarded and retried once from 0%; repeated HTTP 400 needs raw-log review."
    ),
    "auth_error": (
        "OAuth/auth failure. Refresh token: delete ~/.config/70mai/youtube_token.json "
        "and re-run to open browser consent."
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_error(message: str, *, status_code: int | None = None) -> str:
    text = (message or "").lower()
    if status_code == 404 or "session expired" in text or "404" in text:
        return "session_expired"
    if status_code == 429 or "quota" in text or "rate" in text:
        return "rate_limit"
    if status_code == 400:
        return "chunk_rejected"
    if status_code in (401, 403) or "oauth" in text or "credential" in text:
        return "auth_error"
    if status_code in (500, 502, 503, 504):
        return "server_error"
    if "redirectmissinglocation" in text.replace("_", "").replace(" ", ""):
        return "proxy_redirect"
    if "timeout" in text or "timed out" in text:
        return "network_timeout"
    if "requestexception" in text or "connection" in text:
        return "network_timeout"
    return "unknown"


@dataclass
class UploadDiagnostics:
    """Append-only JSONL logger for one upload run."""

    log_path: Path = field(default_factory=lambda: DEFAULT_DIAG_LOG)
    upload_id: str = ""
    video_path: str = ""
    _run_start: float = field(default_factory=time.monotonic, repr=False)
    _last_offset: int = 0
    _last_progress_ts: float = field(default_factory=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        self.log_path = Path(self.log_path)
        if not self.upload_id:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            stem = Path(self.video_path).stem if self.video_path else "upload"
            self.upload_id = f"{stem}-{stamp}"

    def log(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": _utc_now(),
            "upload_id": self.upload_id,
            "event": event,
        }
        if self.video_path:
            record["video_path"] = self.video_path
        record.update({k: v for k, v in fields.items() if v is not None})
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def start(self, *, video_path: Path, size: int, title: str, resume: bool, chunk_bytes: int) -> None:
        self.video_path = str(video_path)
        self.log(
            "upload_start",
            size_bytes=size,
            title=title,
            resume=resume,
            chunk_bytes=chunk_bytes,
        )

    def session_resumed(self, offset: int, size: int) -> None:
        self._last_offset = offset
        self._last_progress_ts = time.monotonic()
        pct = round(offset * 100 / size, 1) if size else 0
        self.log("session_resumed", offset_bytes=offset, progress_pct=pct)

    def session_created(self, session_path: str | None) -> None:
        self.log("session_created", session_path=session_path)

    def session_reset(self, *, reason: str) -> None:
        self.log(
            "session_reset",
            reason=reason[:500],
            category="session_expired",
            action="restart_from_zero",
        )

    def chunk_ok(self, offset: int, size: int, *, status_code: int) -> None:
        now = time.monotonic()
        delta_bytes = max(0, offset - self._last_offset)
        delta_sec = max(0.001, now - self._last_progress_ts)
        mbps = (delta_bytes / delta_sec) / (1024 * 1024)
        self._last_offset = offset
        self._last_progress_ts = now
        pct = round(offset * 100 / size, 1) if size else 0
        self.log(
            "chunk_ok",
            offset_bytes=offset,
            progress_pct=pct,
            status_code=status_code,
            throughput_mbps=round(mbps, 2),
        )

    def retry(self, *, attempt: int, reason: str, method: str, url_hint: str = "") -> None:
        category = classify_error(reason)
        self.log(
            "retry",
            attempt=attempt,
            reason=reason[:500],
            category=category,
            method=method,
            url_hint=url_hint[:120] if url_hint else None,
        )

    def error(self, message: str, *, status_code: int | None = None, offset: int | None = None) -> None:
        category = classify_error(message, status_code=status_code)
        self.log(
            "error",
            message=message[:1000],
            status_code=status_code,
            category=category,
            offset_bytes=offset,
            elapsed_sec=round(time.monotonic() - self._run_start, 1),
        )

    def success(self, video_id: str, size: int) -> None:
        elapsed = time.monotonic() - self._run_start
        avg_mbps = (size / elapsed) / (1024 * 1024) if elapsed > 0 else 0
        self.log(
            "upload_success",
            video_id=video_id,
            elapsed_sec=round(elapsed, 1),
            avg_throughput_mbps=round(avg_mbps, 2),
        )


def latest_upload_health(
    log_path: Path = DEFAULT_DIAG_LOG,
    *,
    max_bytes: int = 256 * 1024,
) -> tuple[str, str]:
    """Return latest upload state without parsing an unbounded JSONL history."""
    if not log_path.is_file():
        return "unknown", "no uploads yet"
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return "unknown", str(exc)[:80]

    lines = raw.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        kind = event.get("event")
        if kind == "upload_success":
            video_id = event.get("video_id") or "complete"
            return "ok", f"last success {video_id}"
        if kind == "error":
            code = event.get("status_code")
            message = str(event.get("message") or "")
            if not code and "Upload chunk failed (" in message:
                candidate = message.split("Upload chunk failed (", 1)[1].split(")", 1)[0]
                if candidate.isdigit():
                    code = int(candidate)
            category = classify_error(message, status_code=code)
            if code:
                return "error", f"HTTP {code} ({category})"
            message = message or category
            return "error", message.splitlines()[0][:80]
        if kind == "session_reset":
            return "retry", "bad session reset; restarting from 0%"
        if kind == "retry":
            attempt = event.get("attempt") or "?"
            category = event.get("category") or "network"
            return "retry", f"retry {attempt} ({category})"
        if kind == "upload_start":
            return "upload", "upload started"
    return "unknown", "no upload status"


def load_events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def analyze_log(
    log_path: Path = DEFAULT_DIAG_LOG,
    *,
    upload_id: str | None = None,
    last_n_uploads: int | None = None,
) -> dict[str, Any]:
    """Analyze JSONL diagnostics; return summary dict with recommendations."""
    events = load_events(log_path)
    if not events:
        return {
            "log_path": str(log_path),
            "events": 0,
            "recommendations": ["No diagnostic events yet. Run an upload to populate the log."],
        }

    if upload_id:
        events = [e for e in events if e.get("upload_id") == upload_id]
    elif last_n_uploads:
        ids: list[str] = []
        for e in reversed(events):
            uid = e.get("upload_id")
            if uid and uid not in ids:
                ids.append(uid)
            if len(ids) >= last_n_uploads:
                break
        keep = set(ids)
        events = [e for e in events if e.get("upload_id") in keep]

    by_upload: dict[str, list[dict]] = {}
    for e in events:
        uid = e.get("upload_id", "unknown")
        by_upload.setdefault(uid, []).append(e)

    summaries = []
    all_categories: Counter[str] = Counter()
    recommendations: list[str] = []

    for uid, upload_events in by_upload.items():
        starts = [e for e in upload_events if e.get("event") == "upload_start"]
        errors = [e for e in upload_events if e.get("event") == "error"]
        retries = [e for e in upload_events if e.get("event") == "retry"]
        successes = [e for e in upload_events if e.get("event") == "upload_success"]
        chunks = [e for e in upload_events if e.get("event") == "chunk_ok"]

        max_pct = 0.0
        for e in upload_events:
            pct = e.get("progress_pct")
            if isinstance(pct, (int, float)):
                max_pct = max(max_pct, float(pct))

        categories = Counter(
            e.get("category") for e in errors + retries if e.get("category")
        )
        all_categories.update(categories)

        throughputs = [
            float(e["throughput_mbps"])
            for e in chunks
            if isinstance(e.get("throughput_mbps"), (int, float))
        ]
        avg_tp = sum(throughputs) / len(throughputs) if throughputs else None

        status = "success" if successes else ("failed" if errors else "incomplete")
        if status == "incomplete" and max_pct > 0 and max_pct < 100:
            all_categories["process_interrupted"] += 1

        entry = {
            "upload_id": uid,
            "video_path": starts[-1].get("video_path") if starts else None,
            "status": status,
            "max_progress_pct": max_pct,
            "retry_count": len(retries),
            "error_count": len(errors),
            "avg_chunk_mbps": round(avg_tp, 2) if avg_tp is not None else None,
            "video_id": successes[-1].get("video_id") if successes else None,
            "categories": dict(categories),
        }
        summaries.append(entry)

        if avg_tp is not None and avg_tp < 1.0 and status != "success":
            recommendations.append(RECOMMENDATIONS["slow_throughput"])

    for cat, count in all_categories.most_common():
        if cat in RECOMMENDATIONS and count > 0:
            recommendations.append(f"[{cat} ×{count}] {RECOMMENDATIONS[cat]}")

    if not recommendations:
        if all(s["status"] == "success" for s in summaries):
            recommendations.append("Recent uploads completed without classified issues.")
        else:
            recommendations.append(
                "Review raw log for unclassified errors: "
                f"{log_path}"
            )

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_recs: list[str] = []
    for r in recommendations:
        if r not in seen:
            seen.add(r)
            unique_recs.append(r)

    return {
        "log_path": str(log_path),
        "events": len(events),
        "uploads": summaries,
        "category_totals": dict(all_categories),
        "recommendations": unique_recs,
    }


def format_report(analysis: dict[str, Any]) -> str:
    lines = [
        f"# YouTube upload diagnostics",
        "",
        f"Log: `{analysis.get('log_path', '?')}`",
        f"Events: {analysis.get('events', 0)}",
        "",
    ]
    uploads = analysis.get("uploads") or []
    if uploads:
        lines.append("## Upload runs")
        lines.append("")
        for u in uploads:
            lines.append(f"### {u.get('upload_id', '?')}")
            lines.append(f"- Status: **{u.get('status')}**")
            if u.get("video_path"):
                lines.append(f"- File: `{u['video_path']}`")
            lines.append(f"- Max progress: {u.get('max_progress_pct', 0)}%")
            lines.append(f"- Retries: {u.get('retry_count', 0)}")
            if u.get("avg_chunk_mbps") is not None:
                lines.append(f"- Avg chunk throughput: {u['avg_chunk_mbps']} MB/s")
            if u.get("video_id"):
                lines.append(f"- Video: https://youtu.be/{u['video_id']}")
            cats = u.get("categories") or {}
            if cats:
                lines.append(f"- Error categories: {cats}")
            lines.append("")

    recs = analysis.get("recommendations") or []
    if recs:
        lines.append("## Recommendations")
        lines.append("")
        for r in recs:
            lines.append(f"- {r}")
        lines.append("")

    return "\n".join(lines)
