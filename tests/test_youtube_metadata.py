import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import youtube_metadata as ym
import youtube_upload


class YouTubeMetadataFormatTests(unittest.TestCase):
    def test_record_type_ru(self) -> None:
        self.assertEqual(ym.record_type_label("Normal"), "простые записи")
        self.assertEqual(ym.record_type_label("Event"), "запись события")
        self.assertEqual(ym.record_type_label("Parking"), "запись парковки")

    def test_build_youtube_title_same_day(self) -> None:
        start = datetime(2025, 8, 10, 8, 50, 33)
        end = start + timedelta(minutes=30)
        title = ym.build_youtube_title("70mai 2025-08-10", "Parking", [(start, end)])
        self.assertIn("запись парковки", title)
        self.assertIn("10.08.2025 08:50", title)
        self.assertIn("09:20", title)

    def test_build_youtube_title_cross_day(self) -> None:
        start = datetime(2025, 8, 10, 8, 50)
        end = datetime(2025, 9, 3, 9, 47)
        title = ym.build_youtube_title("70mai", "Parking", [(start, end)])
        self.assertIn("03.09.2025 09:47", title)

    def test_build_youtube_title_truncates_to_100(self) -> None:
        start = datetime(2025, 1, 1, 0, 0)
        end = datetime(2026, 12, 31, 23, 59)
        title = ym.build_youtube_title("70mai very long base title prefix", "Normal", [(start, end)])
        self.assertLessEqual(len(title), ym.TITLE_MAX_LEN)

    def test_build_youtube_body_lists_clips(self) -> None:
        start = datetime(2025, 8, 10, 9, 59, 47)
        end = start + timedelta(seconds=30)
        body = ym.build_youtube_body("Parking", [(start, end)])
        self.assertIn("Тип: запись парковки", body)
        self.assertIn("Клип 1:", body)
        self.assertIn("10.08.2025 09:59:47", body)

    def test_build_youtube_body_truncates_long_list(self) -> None:
        ranges = []
        t0 = datetime(2025, 1, 1, 0, 0, 0)
        for i in range(500):
            start = t0 + timedelta(minutes=i)
            ranges.append((start, start + timedelta(seconds=30)))
        body = ym.build_youtube_body("Event", ranges, max_len=800)
        self.assertIn("… и ещё", body)
        self.assertLess(len(body), len(ranges) * 60)


class YouTubeMetadataCollectTests(unittest.TestCase):
    def test_collect_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_dir = root / "Output"
            merge_dir = video_dir / "Parking" / "Front"
            merge_dir.mkdir(parents=True)
            merge = merge_dir / "PA_20250810-095947_100017_F.mp4"
            merge.write_bytes(b"x")
            wall = datetime(2025, 8, 10, 9, 59, 47)
            manifest = {
                "version": 1,
                "record_type": "Parking",
                "camera": "Front",
                "merge": merge.name,
                "clips": [
                    {
                        "key": "20250810095947-000001",
                        "wall": wall.isoformat(),
                        "dur": 30.0,
                        "offset": 0.0,
                        "src": "PA20250810095947-000001F.MP4",
                    }
                ],
            }
            merge.with_name(merge.name + ".timeline.json").write_text(
                __import__("json").dumps(manifest),
                encoding="utf-8",
            )
            ranges = ym.collect_clip_ranges(video_dir, "Parking")
            self.assertEqual(len(ranges), 1)
            self.assertEqual(ranges[0][0], wall)
            self.assertEqual(ranges[0][1], wall + timedelta(seconds=30))


class YouTubeUpdateApiTests(unittest.TestCase):
    def test_update_video_metadata(self) -> None:
        youtube = MagicMock()
        youtube.videos().list().execute.return_value = {
            "items": [{"snippet": {"title": "old", "description": "d", "categoryId": "22"}}]
        }
        with patch.object(youtube_upload, "get_youtube_service", return_value=youtube):
            youtube_upload.update_video_metadata(
                "abc123",
                title="new title",
                description="new body",
            )
        youtube.videos().update.assert_called_once()

    def test_post_video_comment(self) -> None:
        youtube = MagicMock()
        youtube.commentThreads().insert().execute.return_value = {"id": "thread1"}
        with patch.object(youtube_upload, "get_youtube_service", return_value=youtube):
            thread_id = youtube_upload.post_video_comment("abc123", "hello")
        self.assertEqual(thread_id, "thread1")


if __name__ == "__main__":
    unittest.main()
