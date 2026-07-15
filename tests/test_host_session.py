#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from card_identity import host_session_stale
from publish_state import (
    clear_host_session,
    empty_publish_state,
    get_or_create_card_id,
    save_state_file,
    sd_state_path,
    stamp_host_session,
)


class HostSessionTests(unittest.TestCase):
    def test_stale_done_without_sd_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "card"
            source.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            card_id = get_or_create_card_id(source)
            assert card_id

            save_state_file(
                sd_state_path(source, "Parking"),
                empty_publish_state(source, "Parking", card_id=card_id),
            )
            (temp / "autopilot_status.json").write_text(
                json.dumps(
                    {
                        "phase": "done",
                        "record_type": "Parking",
                        "chunk_index": 1,
                        "trip_index": 1,
                        "youtube_url": "https://youtu.be/lUy-Y6DwCEM",
                    }
                ),
                encoding="utf-8",
            )
            stamp_host_session(temp, card_id)

            self.assertTrue(host_session_stale(source, card_id, temp))

    def test_clear_host_session_drops_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "autopilot_status.json").write_text("{}", encoding="utf-8")
            (temp / "autopilot_trip_reasons.json").write_text("{}", encoding="utf-8")
            clear_host_session(temp)
            self.assertFalse((temp / "autopilot_status.json").is_file())
            self.assertFalse((temp / "autopilot_trip_reasons.json").is_file())


if __name__ == "__main__":
    unittest.main()
