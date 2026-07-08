#!/usr/bin/env python3
"""YouTube Data API v3: OAuth2 and resumable video upload with session resume."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable

from import_70mai import format_bar, format_duration, is_tty, log
from youtube_upload_diagnostics import DEFAULT_DIAG_LOG, UploadDiagnostics

from project_env import cli_python
HTTP_TIMEOUT_SEC = 600
MAX_UPLOAD_RETRIES = 12
# Big chunks amortize the per-chunk RTT pause (334ms RTT observed); Google
# recommends the fewest possible requests on stable connections.
UPLOAD_CHUNK_BYTES = 256 * 1024 * 1024
UPLOAD_STREAM_BLOCK = 4 * 1024 * 1024  # read block for whole-file streaming mode
UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

DEFAULT_CREDENTIALS = Path.home() / ".config/70mai/youtube_credentials.json"
DEFAULT_TOKEN = Path.home() / ".config/70mai/youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploadError(RuntimeError):
    pass


def format_file_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit in ("B", "KB"):
                return f"{size:.0f} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


class UploadProgressReporter:
    """Detailed upload progress: bar, MB, speed, ETA."""

    def __init__(self, label: str, size: int) -> None:
        self.label = label
        self.size = max(size, 1)
        self.start = time.monotonic()
        self._last_logged_pct = -1

    def update(self, pct: int, offset: int | None = None) -> None:
        if offset is None:
            offset = min(self.size, int(self.size * pct / 100))
        elapsed = time.monotonic() - self.start
        rate = offset / elapsed if elapsed > 0 else 0.0
        remaining = self.size - offset
        eta = remaining / rate if rate > 0 else 0.0
        speed_mb = rate / (1024 * 1024)
        bar = format_bar(offset / self.size)
        line = (
            f"Upload {self.label}: [{bar}] "
            f"{format_file_size(offset)}/{format_file_size(self.size)} ({pct}%) "
            f"| {speed_mb:.1f} MB/s | ETA {format_duration(eta)}"
        )
        if is_tty():
            sys.stderr.write("\r\033[K" + line)
            sys.stderr.flush()
            return
        pct_bucket = pct // 2 * 2
        if pct == 100 or pct_bucket > self._last_logged_pct or self._last_logged_pct < 0:
            log(f"  {line}")
            self._last_logged_pct = pct_bucket

    def finish(self) -> None:
        if is_tty():
            sys.stderr.write("\n")
            sys.stderr.flush()


def _call_progress(
    on_progress: Callable[..., None] | None,
    pct: int,
    offset: int,
    size: int,
) -> None:
    if on_progress is None:
        return
    try:
        on_progress(pct, offset, size)
    except TypeError:
        on_progress(pct)


def _require_google():
    try:
        import httplib2
        import requests
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_httplib2 import AuthorizedHttp
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubeUploadError(
            "Google API libraries missing. Install: pip install -r requirements.txt"
        ) from exc
    return (
        Request,
        Credentials,
        InstalledAppFlow,
        build,
        httplib2,
        AuthorizedHttp,
        requests,
        AuthorizedSession,
    )


def load_credentials(
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
):
    Request, Credentials, InstalledAppFlow, *_ = _require_google()

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.is_file():
                raise YouTubeUploadError(
                    f"OAuth credentials not found: {credentials_path}\n"
                    "Download Desktop OAuth JSON from Google Cloud Console "
                    "(YouTube Data API v3 enabled)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        if ".70mai/auth" in token_path.as_posix():
            from publish_state import AuthStore

            AuthStore.sync_token(token_path)

    return creds


def get_youtube_service(
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
):
    _, _, _, build, httplib2, AuthorizedHttp, *_ = _require_google()
    creds = load_credentials(credentials_path, token_path)
    http = httplib2.Http(timeout=HTTP_TIMEOUT_SEC)
    http = AuthorizedHttp(creds, http=http)
    return build("youtube", "v3", http=http, cache_discovery=False)


def _authorized_session(
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
):
    *_, AuthorizedSession = _require_google()
    creds = load_credentials(credentials_path, token_path)
    session = AuthorizedSession(creds)
    # System/VPN proxies often break resumable PUT (RedirectMissingLocation).
    session.trust_env = False
    return session


def _request_with_retries(
    session,
    method: str,
    url: str,
    *,
    diag: UploadDiagnostics | None = None,
    attempts: int = MAX_UPLOAD_RETRIES,
    **kwargs,
):
    _, _, _, _, _, _, requests, _ = _require_google()
    last_exc = None
    for attempt in range(attempts):
        try:
            resp = session.request(method, url, timeout=HTTP_TIMEOUT_SEC, **kwargs)
            if resp.status_code in (500, 502, 503, 504) and attempt + 1 < attempts:
                if diag:
                    diag.retry(
                        attempt=attempt + 1,
                        reason=f"HTTP {resp.status_code}",
                        method=method,
                        url_hint=url,
                    )
                time.sleep(min(2 ** (attempt + 1), 60))
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if diag and attempt + 1 < attempts:
                diag.retry(
                    attempt=attempt + 1,
                    reason=str(exc),
                    method=method,
                    url_hint=url,
                )
            if attempt + 1 >= attempts:
                break
            time.sleep(min(2 ** (attempt + 1), 60))
    raise YouTubeUploadError(f"YouTube upload request failed: {last_exc}") from last_exc


def _parse_range_end(range_header: str | None) -> int | None:
    """Parse 'bytes 0-12345' from Range response header; return next byte offset."""
    if not range_header:
        return None
    part = range_header.split("bytes", 1)[-1].strip()
    if "-" not in part:
        return None
    end_str = part.split("-", 1)[1].strip()
    if not end_str.isdigit():
        return None
    return int(end_str) + 1


def save_upload_session(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_upload_session(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_upload_session(path: Path) -> None:
    path.unlink(missing_ok=True)


def _session_matches(session: dict[str, Any], video_path: Path, size: int) -> bool:
    stored = session.get("video_path")
    stored_stem = session.get("video_stem") or (Path(stored).stem if stored else None)
    if stored_stem and stored_stem != video_path.stem:
        return False
    if stored and not stored_stem:
        try:
            if Path(stored).resolve() != video_path.resolve() and Path(stored).name != video_path.name:
                return False
        except OSError:
            if Path(stored).name != video_path.name:
                return False
    if session.get("size") not in (None, size):
        return False
    return bool(session.get("upload_url"))


def _query_upload_offset(session, upload_url: str, size: int, *, diag: UploadDiagnostics | None = None) -> int:
    resp = _request_with_retries(
        session,
        "PUT",
        upload_url,
        diag=diag,
        data=b"",
        headers={
            "Content-Length": "0",
            "Content-Range": f"bytes */{size}",
        },
    )
    if resp.status_code in (200, 201):
        return size
    if resp.status_code == 308:
        offset = _parse_range_end(resp.headers.get("Range"))
        return offset if offset is not None else 0
    if resp.status_code == 404:
        raise YouTubeUploadError("Upload session expired (404); restart without --resume-upload")
    raise YouTubeUploadError(
        f"Upload status query failed ({resp.status_code}): {resp.text[:500]}"
    )


def _init_upload(session, *, size: int, metadata: dict, diag: UploadDiagnostics | None = None) -> str:
    init = _request_with_retries(
        session,
        "POST",
        UPLOAD_INIT_URL,
        diag=diag,
        params={"uploadType": "resumable", "part": "snippet,status"},
        json=metadata,
        headers={
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
    )
    if init.status_code not in (200, 201):
        raise YouTubeUploadError(
            f"Upload init failed ({init.status_code}): {init.text[:500]}"
        )
    upload_url = init.headers.get("Location")
    if not upload_url:
        raise YouTubeUploadError("Upload init missing Location header")
    return upload_url


def upload_session_path_for_file(video_path: Path, temp_dir: Path | None = None) -> Path:
    """Default session file, e.g. trip_01.upload.json under .publish_tmp."""
    base = temp_dir or Path("video/Output/.publish_tmp")
    return base / f"{Path(video_path).stem}.upload.json"


def upload_video(
    video_path: Path,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "private",
    category_id: str = "22",
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
    session_path: Path | None = None,
    resume: bool = False,
    diag_log: Path | None = DEFAULT_DIAG_LOG,
    on_progress: Callable[[int], None] | None = None,
    chunk_bytes: int | None = None,
) -> str:
    """Upload video via resumable protocol; optional session file for cross-run resume.

    chunk_bytes: upload chunk size; 0 streams the whole file in one PUT
    (fastest on stable connections); None uses UPLOAD_CHUNK_BYTES.
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(HTTP_TIMEOUT_SEC)
    diag = UploadDiagnostics(log_path=diag_log, video_path=str(video_path)) if diag_log else None

    try:
        return _upload_video_inner(
            video_path,
            title=title,
            description=description,
            tags=tags,
            privacy=privacy,
            category_id=category_id,
            credentials_path=credentials_path,
            token_path=token_path,
            session_path=session_path,
            resume=resume,
            diag=diag,
            on_progress=on_progress,
            chunk_bytes=UPLOAD_CHUNK_BYTES if chunk_bytes is None else chunk_bytes,
        )
    except YouTubeUploadError as exc:
        if diag:
            diag.error(str(exc))
        raise
    finally:
        socket.setdefaulttimeout(old_timeout)


def _upload_video_inner(
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str] | None,
    privacy: str,
    category_id: str,
    credentials_path: Path,
    token_path: Path,
    session_path: Path | None,
    resume: bool,
    diag: UploadDiagnostics | None,
    on_progress: Callable[[int], None] | None,
    chunk_bytes: int = UPLOAD_CHUNK_BYTES,
) -> str:
    session = _authorized_session(credentials_path, token_path)
    whole_file = chunk_bytes <= 0

    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        raise YouTubeUploadError(f"Video not found: {video_path}")
    size = video_path.stat().st_size

    if session_path is None:
        session_path = upload_session_path_for_file(video_path)

    log(
        f"Uploading: {video_path.name} ({format_file_size(size)}) — {title}"
    )

    if diag:
        diag.start(
            video_path=video_path,
            size=size,
            title=title,
            resume=resume,
            chunk_bytes=chunk_bytes,
        )

    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    upload_url: str | None = None
    offset = 0

    if resume and session_path:
        saved = load_upload_session(session_path)
        if saved and _session_matches(saved, video_path, size):
            upload_url = saved["upload_url"]
            try:
                offset = _query_upload_offset(session, upload_url, size, diag=diag)
            except YouTubeUploadError as exc:
                if diag:
                    diag.error(str(exc), status_code=404)
                clear_upload_session(session_path)
                upload_url = None
            else:
                if offset >= size:
                    clear_upload_session(session_path)
                    raise YouTubeUploadError(
                        "Session file indicates complete upload but no video ID saved"
                    )
                if diag:
                    diag.session_resumed(offset, size)
                log(
                    f"  Resume from {format_file_size(offset)} "
                    f"({int(offset * 100 / size)}%)"
                )

    if upload_url is None:
        upload_url = _init_upload(session, size=size, metadata=metadata, diag=diag)
        offset = 0
        save_upload_session(
            session_path,
            {
                "video_path": str(video_path),
                "video_stem": video_path.stem,
                "size": size,
                "upload_url": upload_url,
                "title": title,
                "offset": 0,
            },
        )
        if diag:
            diag.session_created(str(session_path))

    last_logged = -1

    def report(offset_now: int) -> None:
        nonlocal last_logged
        pct = min(99, int(offset_now * 100 / size))
        if on_progress and pct >= last_logged + 2:
            _call_progress(on_progress, pct, offset_now, size)
            last_logged = pct

    stream_failures = 0
    with video_path.open("rb") as fh:
        fh.seek(offset)
        while offset < size:
            end = size - 1 if whole_file else min(offset + chunk_bytes, size) - 1
            headers = {
                "Content-Length": str(end - offset + 1),
                "Content-Range": f"bytes {offset}-{end}/{size}",
            }
            if whole_file:

                def stream(start: int = offset, stop: int = end):
                    fh.seek(start)
                    remaining = stop - start + 1
                    sent = start
                    while remaining > 0:
                        block = fh.read(min(UPLOAD_STREAM_BLOCK, remaining))
                        if not block:
                            break
                        remaining -= len(block)
                        sent += len(block)
                        report(sent)
                        yield block

                try:
                    resp = _request_with_retries(
                        session,
                        "PUT",
                        upload_url,
                        diag=diag,
                        attempts=1,
                        data=stream(),
                        headers=headers,
                    )
                except YouTubeUploadError:
                    # Stream interrupted: ask the server where to resume from.
                    stream_failures += 1
                    if stream_failures >= MAX_UPLOAD_RETRIES:
                        raise
                    time.sleep(min(2**stream_failures, 60))
                    offset = _query_upload_offset(
                        session, upload_url, size, diag=diag
                    )
                    fh.seek(offset)
                    if diag:
                        diag.retry(
                            attempt=stream_failures,
                            reason=f"stream interrupted; resuming at {offset}",
                            method="PUT",
                            url_hint=upload_url,
                        )
                    continue
            else:
                chunk = fh.read(end - offset + 1)
                if not chunk:
                    break
                resp = _request_with_retries(
                    session,
                    "PUT",
                    upload_url,
                    diag=diag,
                    data=chunk,
                    headers=headers,
                )
            if resp.status_code in (200, 201):
                clear_upload_session(session_path)
                video_id = resp.json()["id"]
                if diag:
                    diag.success(video_id, size)
                _call_progress(on_progress, 100, size, size)
                return video_id
            if resp.status_code == 308:
                server_offset = _parse_range_end(resp.headers.get("Range"))
                if server_offset is not None and server_offset > offset:
                    offset = server_offset
                    fh.seek(offset)
                else:
                    offset = end + 1
                save_upload_session(
                    session_path,
                    {
                        "video_path": str(video_path),
                        "video_stem": video_path.stem,
                        "size": size,
                        "upload_url": upload_url,
                        "title": title,
                        "offset": offset,
                    },
                )
                if diag:
                    diag.chunk_ok(offset, size, status_code=308)
                pct = min(99, int(offset * 100 / size))
                if on_progress and pct >= last_logged + 2:
                    _call_progress(on_progress, pct, offset, size)
                    last_logged = pct
                continue
            if resp.status_code == 404:
                clear_upload_session(session_path)
                msg = "Upload session expired mid-transfer (404); rerun with fresh upload"
                if diag:
                    diag.error(msg, status_code=404, offset=offset)
                raise YouTubeUploadError(msg)
            msg = f"Upload chunk failed ({resp.status_code}): {resp.text[:500]}"
            if diag:
                diag.error(msg, status_code=resp.status_code, offset=offset)
            raise YouTubeUploadError(msg)

    raise YouTubeUploadError("Upload finished without video ID")


def ensure_playlist(
    title: str,
    *,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
) -> str:
    youtube = get_youtube_service(credentials_path, token_path)
    body = {
        "snippet": {"title": title, "description": ""},
        "status": {"privacyStatus": "private"},
    }
    response = youtube.playlists().insert(part="snippet,status", body=body).execute()
    return response["id"]


def add_to_playlist(
    playlist_id: str,
    video_id: str,
    *,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
) -> None:
    youtube = get_youtube_service(credentials_path, token_path)
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
    }
    youtube.playlistItems().insert(part="snippet", body=body).execute()


def load_state_playlist(state_path: Path) -> str | None:
    if not state_path.is_file():
        return None
    data = json.loads(state_path.read_text(encoding="utf-8"))
    return data.get("playlist_id")


def save_state_playlist(state_path: Path, playlist_id: str) -> None:
    data = {}
    if state_path.is_file():
        data = json.loads(state_path.read_text(encoding="utf-8"))
    data["playlist_id"] = playlist_id
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")



def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Upload a video to YouTube (resumable)")
    parser.add_argument("video", type=Path, help="MP4 file to upload")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--privacy", default="private", choices=("private", "unlisted", "public"))
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument(
        "--session",
        type=Path,
        help="Session state JSON (default: next to video as .upload.session.json)",
    )
    parser.add_argument(
        "--resume-upload",
        action="store_true",
        help="Resume from saved session URI if present",
    )
    parser.add_argument(
        "--diag-log",
        type=Path,
        default=DEFAULT_DIAG_LOG,
        help="Append structured diagnostics to this JSONL file",
    )
    parser.add_argument(
        "--no-diag",
        action="store_true",
        help="Disable diagnostic logging",
    )
    parser.add_argument(
        "--upload-chunk-mb",
        type=int,
        default=None,
        metavar="MB",
        help=(
            f"Upload chunk size in MB (default: {UPLOAD_CHUNK_BYTES // (1024 * 1024)}); "
            "0 = whole file in one streaming PUT (fastest on stable networks)"
        ),
    )
    args = parser.parse_args(argv)

    session_path = args.session or upload_session_path_for_file(args.video)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None
    file_size = args.video.stat().st_size if args.video.is_file() else 0
    reporter = UploadProgressReporter(args.video.name, file_size)

    def progress(pct: int, offset: int = 0, size: int = 0) -> None:
        reporter.update(pct, offset or None)

    video_id = upload_video(
        args.video,
        title=args.title,
        description=args.description,
        tags=tags,
        privacy=args.privacy,
        credentials_path=args.credentials,
        token_path=args.token,
        session_path=session_path,
        resume=args.resume_upload,
        diag_log=None if args.no_diag else args.diag_log,
        on_progress=progress,
        chunk_bytes=(
            None if args.upload_chunk_mb is None else args.upload_chunk_mb * 1024 * 1024
        ),
    )
    reporter.finish()
    print(f"Done: https://youtu.be/{video_id}")
    if not args.no_diag:
        print(f"Diagnostics: {args.diag_log}", flush=True)
        print(f"Analyze: {cli_python()} scripts/analyze_youtube_upload.py", flush=True)


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
