#!/usr/bin/env python3
import unittest

from card_identity import clip_signature_delta, describe_card_status


class CardIdentityTests(unittest.TestCase):
    def test_describe_new_card(self) -> None:
        sig = {
            "fingerprint": "Normal:10+10@a/b",
            "total_clips": 20,
            "types": {"Normal": {"total_files": 20}},
        }
        lines = describe_card_status(
            card_id="uuid-new-card",
            label=None,
            previous=None,
            signature=sig,
        )
        self.assertTrue(any("NEW" in line for line in lines))

    def test_describe_same_card_new_clips(self) -> None:
        old = {
            "card_id": "same-id",
            "clip_signature": {
                "fingerprint": "old",
                "types": {
                    "Normal": {"total_files": 100},
                    "Event": {"total_files": 50},
                    "Parking": {"total_files": 0},
                },
            },
        }
        new = {
            "fingerprint": "new",
            "total_clips": 162,
            "types": {
                "Normal": {"total_files": 112},
                "Event": {"total_files": 50},
                "Parking": {"total_files": 0},
            },
        }
        lines = describe_card_status(
            card_id="same-id",
            label="Dashcam",
            previous=old,
            signature=new,
        )
        text = "\n".join(lines)
        self.assertIn("known", text)
        self.assertIn("Normal +12 clips", text)

    def test_clip_signature_delta(self) -> None:
        old = {"types": {"Normal": {"total_files": 10}, "Event": {"total_files": 5}}}
        new = {"types": {"Normal": {"total_files": 15}, "Event": {"total_files": 5}}}
        delta = clip_signature_delta(old, new)
        self.assertEqual(delta["Normal"]["delta"], 5)
        self.assertNotIn("Event", delta)


if __name__ == "__main__":
    unittest.main()
