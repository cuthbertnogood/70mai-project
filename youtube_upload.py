#!/usr/bin/env python3
"""YouTube Data API v3: OAuth2 and resumable video upload."""

from __future__ import annotations

import json
import time
from pathlib import Path

UPLOAD_CHUNK_BYTES = 10 * 1024 * 1024
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


def _request_with_retries(session, method: str, url: str, **kwargs):
    _, _, _, _, _, _, requests, _ = _require_google()
    last_exc = None
    for attempt in range(MAX_UPLOAD_RETRIES):
        try:
            resp = session.request(method, url, timeout=HTTP_TIMEOUT_SEC, **kwargs)
            if resp.status_code in (500, 502, 503, 504) and attempt + 1 < MAX_UPLOAD_RETRIES:
                time.sleep(min(2 ** (attempt + 1), 60))
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt + 1 >= MAX_UPLOAD_RETRIES:
                break
            time.sleep(min(2 ** (attempt + 1), 60))
    raise YouTubeUploadError(f"YouTube upload request failed: {last_exc}") from last_exc


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
    """Upload video via resumable protocol (requests, not httplib2)."""
    *_, requests, AuthorizedSession = _require_google()
    creds = load_credentials(credentials_path, token_path)
    session = AuthorizedSession(creds)
    # System/VPN proxies often break resumable PUT (RedirectMissingLocation).
    session.trust_env = False

    video_path = Path(video_path)
    size = video_path.stat().st_size
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

    init = _request_with_retries(
        session,
        "POST",
        UPLOAD_INIT_URL,
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

    offset = 0
    last_logged = -1
    with video_path.open("rb") as fh:
        while offset < size:
            chunk = fh.read(UPLOAD_CHUNK_BYTES)
            end = offset + len(chunk) - 1
            resp = _request_with_retries(
                session,
                "PUT",
                upload_url,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                },
            )
            if resp.status_code in (200, 201):
                if on_progress:
                    on_progress(100)
                return resp.json()["id"]
            if resp.status_code == 308:
                offset = end + 1
                pct = min(99, int(offset * 100 / size))
                if on_progress and pct >= last_logged + 5:
                    on_progress(pct)
                    last_logged = pct
                continue
            raise YouTubeUploadError(
                f"Upload chunk failed ({resp.status_code}): {resp.text[:500]}"
            )

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
