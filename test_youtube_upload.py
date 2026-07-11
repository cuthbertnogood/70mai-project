import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import youtube_upload
from youtube_upload_diagnostics import latest_upload_health


class UploadRecoveryTests(unittest.TestCase):
    def test_stale_saved_session_restarts_once_without_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "trip.mp4"
            video.write_bytes(b"video")
            session = root / "trip.upload.json"
            session.write_text("{}", encoding="utf-8")

            with patch.object(
                youtube_upload,
                "_upload_video_inner",
                side_effect=[
                    youtube_upload.StaleUploadSessionError("HTTP 400"),
                    "video-id",
                ],
            ) as inner:
                result = youtube_upload.upload_video(
                    video,
                    title="test",
                    session_path=session,
                    resume=True,
                    diag_log=None,
                )

            self.assertEqual(result, "video-id")
            self.assertFalse(session.exists())
            self.assertTrue(inner.call_args_list[0].kwargs["resume"])
            self.assertFalse(inner.call_args_list[1].kwargs["resume"])
            self.assertEqual(inner.call_count, 2)

    def test_non_session_error_is_not_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "trip.mp4"
            video.write_bytes(b"video")
            with patch.object(
                youtube_upload,
                "_upload_video_inner",
                side_effect=youtube_upload.YouTubeUploadError("HTTP 403"),
            ) as inner:
                with self.assertRaises(youtube_upload.YouTubeUploadError):
                    youtube_upload.upload_video(
                        video,
                        title="test",
                        diag_log=None,
                    )
            self.assertEqual(inner.call_count, 1)

    def test_network_error_during_offset_query_keeps_saved_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "trip.mp4"
            video.write_bytes(b"video")
            session = root / "trip.upload.json"
            session.write_text(
                json.dumps(
                    {
                        "video_path": str(video),
                        "video_stem": video.stem,
                        "size": video.stat().st_size,
                        "upload_url": "https://upload.invalid/session",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(youtube_upload, "_authorized_session", return_value=object()),
                patch.object(
                    youtube_upload,
                    "_query_upload_offset",
                    side_effect=youtube_upload.YouTubeUploadError("network timeout"),
                ),
            ):
                with self.assertRaises(youtube_upload.YouTubeUploadError):
                    youtube_upload._upload_video_inner(
                        video,
                        title="test",
                        description="",
                        tags=None,
                        privacy="private",
                        category_id="22",
                        credentials_path=root / "credentials.json",
                        token_path=root / "token.json",
                        session_path=session,
                        resume=True,
                        diag=None,
                        on_progress=None,
                    )
            self.assertTrue(session.exists())

    def test_chunk_put_declares_mp4_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "trip.mp4"
            video.write_bytes(b"video")
            session = root / "trip.upload.json"
            seen_headers = {}

            def request(_session, method, _url, **kwargs):
                if method == "POST":
                    return SimpleNamespace(
                        status_code=200,
                        headers={"Location": "https://upload.invalid/session"},
                        text="",
                    )
                seen_headers.update(kwargs["headers"])
                list(kwargs["data"])
                return SimpleNamespace(
                    status_code=201,
                    headers={},
                    text="",
                    json=lambda: {"id": "video-id"},
                )

            with (
                patch.object(youtube_upload, "_authorized_session", return_value=object()),
                patch.object(youtube_upload, "_request_with_retries", side_effect=request),
            ):
                result = youtube_upload._upload_video_inner(
                    video,
                    title="test",
                    description="",
                    tags=None,
                    privacy="private",
                    category_id="22",
                    credentials_path=root / "credentials.json",
                    token_path=root / "token.json",
                    session_path=session,
                    resume=False,
                    diag=None,
                    on_progress=None,
                )

            self.assertEqual(result, "video-id")
            self.assertEqual(seen_headers["Content-Type"], "video/mp4")
            self.assertEqual(seen_headers["Content-Length"], "5")

    def test_sized_stream_does_not_enable_chunked_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "trip.mp4"
            video.write_bytes(b"video")
            with video.open("rb") as handle:
                body = youtube_upload._byte_stream(handle, 0, 4, lambda _offset: None)
                prepared = requests.Request(
                    "PUT",
                    "https://upload.invalid/session",
                    data=body,
                    headers={
                        "Content-Length": "5",
                        "Content-Type": "video/mp4",
                        "Content-Range": "bytes 0-4/5",
                    },
                ).prepare()

            self.assertEqual(prepared.headers["Content-Length"], "5")
            self.assertNotIn("Transfer-Encoding", prepared.headers)


class UploadHealthTests(unittest.TestCase):
    def test_latest_http_error_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "diag.jsonl"
            records = [
                {"event": "upload_start"},
                {
                    "event": "error",
                    "status_code": 400,
                    "category": "chunk_rejected",
                },
            ]
            log.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                latest_upload_health(log),
                ("error", "HTTP 400 (chunk_rejected)"),
            )

    def test_success_replaces_old_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "diag.jsonl"
            records = [
                {"event": "error", "status_code": 400},
                {"event": "upload_success", "video_id": "abc123"},
            ]
            log.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                latest_upload_health(log),
                ("ok", "last success abc123"),
            )


class OAuthHelpTests(unittest.TestCase):
    def test_oauth_needs_reauth_detects_invalid_grant(self) -> None:
        self.assertTrue(
            youtube_upload.oauth_needs_reauth(
                "invalid_grant: Token has been expired or revoked."
            )
        )
        self.assertFalse(youtube_upload.oauth_needs_reauth("network timeout"))

    def test_oauth_help_includes_recovery_commands(self) -> None:
        lines = youtube_upload.oauth_reauth_help_lines(
            token_path=Path("/Volumes/Untitled/.70mai/auth/youtube_token.json"),
        )
        text = "\n".join(lines)
        self.assertIn("publish_all_70mai.sh --skip-import", text)
        self.assertIn("rm -f", text)

    def test_ensure_oauth_non_interactive_skips_browser(self) -> None:
        with patch.object(
            youtube_upload,
            "check_youtube_upload_ready",
            return_value=(False, "oauth_reauth: invalid_grant"),
        ):
            ok, detail = youtube_upload.ensure_youtube_oauth_for_upload(
                Path("creds.json"),
                Path("token.json"),
                interactive=False,
            )
        self.assertFalse(ok)
        self.assertIn("invalid_grant", detail)


if __name__ == "__main__":
    unittest.main()
