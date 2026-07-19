#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from card_identity import refresh_card_identity, sd_card_meta_path
from import_state import sd_import_state_path
from publish_state import (
    StateStore,
    empty_publish_state,
    get_or_create_card_id,
    reset_portable_sd_state,
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

    def test_card_id_change_resets_sd_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "card"
            source.mkdir()
            (source / "Normal" / "Front").mkdir(parents=True)
            old_id = "old-card-uuid-aaaa"
            new_id = "new-card-uuid-bbbb"
            sd_card_id_path(source).parent.mkdir(parents=True, exist_ok=True)
            sd_card_id_path(source).write_text(old_id + "\n", encoding="utf-8")
            sd_card_meta_path(source).write_text(
                json.dumps({"card_id": old_id, "clip_signature": {}}) + "\n",
                encoding="utf-8",
            )
            save_state_file(
                sd_state_path(source, "Normal"),
                {
                    **empty_publish_state(source, "Normal", card_id=old_id),
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
            save_state_file(
                sd_import_state_path(source, "Normal"),
                {
                    "source": str(source),
                    "label": "Normal",
                    "files": {"Normal/Front/foo.MP4": {"status": "merged"}},
                },
            )

            sd_card_id_path(source).write_text(new_id + "\n", encoding="utf-8")
            refresh_card_identity(source, new_id)

            sd = json.loads(sd_state_path(source, "Normal").read_text(encoding="utf-8"))
            self.assertEqual(sd.get("trip_parts"), [])
            self.assertEqual(sd.get("card_id"), new_id)
            imp = json.loads(
                sd_import_state_path(source, "Normal").read_text(encoding="utf-8")
            )
            self.assertEqual(imp.get("files"), {})
            meta = json.loads(sd_card_meta_path(source).read_text(encoding="utf-8"))
            self.assertEqual(meta.get("card_id"), new_id)
            self.assertEqual(meta.get("uploaded_trips"), 0)

    def test_reset_portable_sd_state_keeps_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "card"
            source.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            card_id = "test-card-id"
            auth = source / ".70mai/auth/youtube_token.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"token": "keep"}', encoding="utf-8")
            save_state_file(
                sd_state_path(source, "Normal"),
                empty_publish_state(source, "Normal"),
            )
            (temp / "autopilot_plan.json").write_text('{"chunks":[]}', encoding="utf-8")
            save_state_file(
                temp / "import_Parking.state.json",
                {"source": str(source), "label": "Parking", "files": {"x": {}}},
            )
            reset_portable_sd_state(source, card_id, local_dir=temp)
            self.assertTrue(auth.is_file())
            sd = json.loads(sd_state_path(source, "Normal").read_text(encoding="utf-8"))
            self.assertEqual(sd.get("card_id"), card_id)
            self.assertFalse((temp / "autopilot_plan.json").is_file())
            imp = json.loads((temp / "import_Parking.state.json").read_text(encoding="utf-8"))
            self.assertEqual(imp.get("files"), {})


if __name__ == "__main__":
    unittest.main()
