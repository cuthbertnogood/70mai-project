#!/usr/bin/env python3
"""YouTube Data API v3: OAuth2 and resumable video upload with session resume."""

from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path
from typing import Any, Callable

from youtube_upload_diagnostics import DEFAULT_DIAG_LOG, UploadDiagnostics

from project_env import cli_python
HTTP_TIMEOUT_SEC = 600
MAX_UPLOAD_RETRIES = 12
UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

DEFAULT_CREDENTIALS = Path.home() / ".config/70mai/youtube_credentials.json"
DEFAULT_TOKEN = Path.home() / ".config/70mai/youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploadError(RuntimeError):
    pass


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
    **kwargs,
):
    _, _, _, _, _, _, requests, _ = _require_google()
    last_exc = None
    for attempt in range(MAX_UPLOAD_RETRIES):
        try:
            resp = session.request(method, url, timeout=HTTP_TIMEOUT_SEC, **kwargs)
            if resp.status_code in (500, 502, 503, 504) and attempt + 1 < MAX_UPLOAD_RETRIES:
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
            if diag and attempt + 1 < MAX_UPLOAD_RETRIES:
                diag.retry(
                    attempt=attempt + 1,
                    reason=str(exc),
                    method=method,
                    url_hint=url,
                )
            if attempt + 1 >= MAX_UPLOAD_RETRIES:
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
    if stored and Path(stored).resolve() != video_path.resolve():
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
) -> str:
    """Upload video via resumable protocol; optional session file for cross-run resume."""
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
) -> str:
    session = _authorized_session(credentials_path, token_path)

    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        raise YouTubeUploadError(f"Video not found: {video_path}")
    size = video_path.stat().st_size

    if session_path is None:
        session_path = upload_session_path_for_file(video_path)

    if diag:
        diag.start(
            video_path=video_path,
            size=size,
            title=title,
            resume=resume,
            chunk_bytes=UPLOAD_CHUNK_BYTES,
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

    if upload_url is None:
        upload_url = _init_upload(session, size=size, metadata=metadata, diag=diag)
        offset = 0
        save_upload_session(
            session_path,
            {
                "video_path": str(video_path),
                "size": size,
                "upload_url": upload_url,
                "title": title,
                "offset": 0,
            },
        )
        if diag:
            diag.session_created(str(session_path))

    last_logged = -1
    with video_path.open("rb") as fh:
        fh.seek(offset)
        while offset < size:
            chunk = fh.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            resp = _request_with_retries(
                session,
                "PUT",
                upload_url,
                diag=diag,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                },
            )
            if resp.status_code in (200, 201):
                clear_upload_session(session_path)
                video_id = resp.json()["id"]
                if diag:
                    diag.success(video_id, size)
                if on_progress:
                    on_progress(100)
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
                        "size": size,
                        "upload_url": upload_url,
                        "title": title,
                        "offset": offset,
                    },
                )
                if diag:
                    diag.chunk_ok(offset, size, status_code=308)
                pct = min(99, int(offset * 100 / size))
                if on_progress and pct >= last_logged + 5:
                    on_progress(pct)
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
    args = parser.parse_args(argv)

    session_path = args.session or upload_session_path_for_file(args.video)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None

    last = [-1]

    def progress(pct: int) -> None:
        if pct >= last[0] + 5 or pct == 100:
            print(f"upload {pct}%", flush=True)
            last[0] = pct

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
    )
    print(f"Done: https://youtu.be/{video_id}")
    if not args.no_diag:
        print(f"Diagnostics: {args.diag_log}", flush=True)
        print(f"Analyze: {cli_python()} scripts/analyze_youtube_upload.py", flush=True)


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
