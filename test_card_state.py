#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from publish_state import (
    StateStore,
    empty_publish_state,
    get_or_create_card_id,
    save_state_file,
    sd_card_id_path,
    sd_state_path,
)


class CardStateIsolationTests(unittest.TestCase):
    def test_new_card_ignores_local_upload_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "card"
            source.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            card_id = get_or_create_card_id(source)
            assert card_id

            sd_path = sd_state_path(source, "Normal")
            save_state_file(
                sd_path,
                empty_publish_state(source, "Normal", card_id=card_id),
            )

            local_path = temp / "publish_Normal.state.json"
            save_state_file(
                local_path,
                {
                    "source": str(source),
                    "card_id": "old-card-uuid-1111",
                    "trip_parts": [
                        {
                            "record_type": "Normal",
                            "chunk_index": 1,
                            "trip_index": 1,
                            "video_id": "abc123",
                            "uploaded": True,
                        }
                    ],
                },
            )

            store = StateStore(source, temp, "Normal", state_on_sd=True)
            state = store.load(resume=True, quiet=True)
            self.assertEqual(state.get("card_id"), card_id)
            self.assertEqual(state.get("trip_parts"), [])

            cleaned = json.loads(local_path.read_text(encoding="utf-8"))
            self.assertEqual(cleaned.get("trip_parts"), [])
            self.assertEqual(cleaned.get("card_id"), card_id)

    def test_same_card_merges_local_and_sd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "card"
            source.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            card_id = get_or_create_card_id(source)

            sd_path = sd_state_path(source, "Normal")
            save_state_file(
                sd_path,
                {
                    **empty_publish_state(source, "Normal", card_id=card_id),
                    "trip_parts": [
                        {
                            "record_type": "Normal",
                            "chunk_index": 1,
                            "trip_index": 1,
                            "video_id": "on_sd",
                            "uploaded": True,
                        }
                    ],
                },
            )
            local_path = temp / "publish_Normal.state.json"
            save_state_file(
                local_path,
                {
                    "source": str(source),
                    "card_id": card_id,
                    "trip_parts": [
                        {
                            "record_type": "Normal",
                            "chunk_index": 1,
                            "trip_index": 2,
                            "video_id": "local_only",
                            "uploaded": True,
                        }
                    ],
                },
            )

            store = StateStore(source, temp, "Normal", state_on_sd=True)
            state = store.load(resume=True, quiet=True)
            self.assertEqual(len(state.get("trip_parts", [])), 2)

    def test_new_card_ignores_local_chunk_upload_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "card"
            source.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            card_id = get_or_create_card_id(source)
            assert card_id

            sd_path = sd_state_path(source, "Parking")
            save_state_file(
                sd_path,
                empty_publish_state(source, "Parking", card_id=card_id),
            )

            local_path = temp / "publish_Parking.state.json"
            save_state_file(
                local_path,
                {
                    "source": str(source),
                    "card_id": "old-card-uuid-2222",
                    "parts": [
                        {
                            "record_type": "Parking",
                            "index": 1,
                            "video_id": "lUy-Y6DwCEM",
                            "uploaded": True,
                        }
                    ],
                },
            )
            (temp / "autopilot_status.json").write_text(
                '{"phase":"done","youtube_url":"https://youtu.be/lUy-Y6DwCEM"}',
                encoding="utf-8",
            )

            store = StateStore(source, temp, "Parking", state_on_sd=True)
            state = store.load(resume=True, quiet=True)
            self.assertEqual(state.get("parts"), [])
            self.assertFalse((temp / "autopilot_status.json").is_file())

    def test_card_id_file_created_on_sd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "card"
            source.mkdir()
            card_id = get_or_create_card_id(source)
            self.assertTrue(sd_card_id_path(source).is_file())
            self.assertEqual(get_or_create_card_id(source), card_id)


if __name__ == "__main__":
    unittest.main()
