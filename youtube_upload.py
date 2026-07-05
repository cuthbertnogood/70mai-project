#!/usr/bin/env python3
"""YouTube Data API v3: OAuth2 and resumable video upload."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

UPLOAD_CHUNK_BYTES = 10 * 1024 * 1024
HTTP_TIMEOUT_SEC = 600
MAX_UPLOAD_RETRIES = 12

DEFAULT_CREDENTIALS = Path.home() / ".config/70mai/youtube_credentials.json"
DEFAULT_TOKEN = Path.home() / ".config/70mai/youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploadError(RuntimeError):
    pass


def _require_google():
    try:
        import httplib2
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_httplib2 import AuthorizedHttp
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise YouTubeUploadError(
            "Google API libraries missing. Install: pip install -r requirements.txt"
        ) from exc
    return Request, Credentials, InstalledAppFlow, build, HttpError, MediaFileUpload, httplib2, AuthorizedHttp


def get_youtube_service(
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
):
    Request, Credentials, InstalledAppFlow, build, HttpError, _Media, httplib2, AuthorizedHttp = (
        _require_google()
    )

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

    http = httplib2.Http(timeout=HTTP_TIMEOUT_SEC)
    http = AuthorizedHttp(creds, http=http)
    return build("youtube", "v3", http=http, cache_discovery=False)


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
    on_progress=None,
) -> str:
    """Upload video; returns YouTube video ID."""
    _, _, _, _, HttpError, MediaFileUpload, httplib2, _ = _require_google()
    youtube = get_youtube_service(credentials_path, token_path)

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(HTTP_TIMEOUT_SEC)

    body = {
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

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=UPLOAD_CHUNK_BYTES,
    )

    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retry = 0
    try:
        while response is None:
            try:
                status, response = request.next_chunk()
                retry = 0
                if status and on_progress:
                    on_progress(int(status.progress() * 100))
            except HttpError as exc:
                if exc.resp.status in (500, 502, 503, 504) and retry < MAX_UPLOAD_RETRIES:
                    retry += 1
                    time.sleep(min(2**retry, 60))
                    continue
                raise YouTubeUploadError(f"YouTube upload failed: {exc}") from exc
            except (TimeoutError, socket.timeout, OSError, httplib2.error.HttpLib2Error) as exc:
                if retry < MAX_UPLOAD_RETRIES:
                    retry += 1
                    time.sleep(min(2**retry, 60))
                    continue
                raise YouTubeUploadError(f"YouTube upload timed out: {exc}") from exc
    finally:
        socket.setdefaulttimeout(old_timeout)

    video_id = response["id"]
    return video_id


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
