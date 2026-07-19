#!/usr/bin/env python3
"""Tests for slot-aligned 2-cam ffmpeg command construction."""

from __future__ import annotations

import unittest
from pathlib import Path

from clip_timeline import Span
from compose_2cam_70mai import build_compose_2cam_aligned_cmd, run_compose_2cam


def _cmd(front_lane, back_lane, audio="front"):
    return build_compose_2cam_aligned_cmd(
        front_lane,
        back_lane,
        Path("out.mp4"),
        merge_paths={"F.mp4": Path("F.mp4"), "B.mp4": Path("B.mp4")},
        width=1080,
        front_height=608,
        back_height=608,
        crf=20,
        preset="medium",
        fps=25,
        hw=False,
        hw_quality=50,
        hw_decode=False,
        codec="h264",
        audio_source=audio,
        total_duration=60.0,
    )


class AlignedCmdTests(unittest.TestCase):
    def test_full_coverage_two_inputs(self) -> None:
        front = [Span("video", 0.0, 60.0, merge="F.mp4", source_ss=0.0)]
        back = [Span("video", 0.0, 60.0, merge="B.mp4", source_ss=0.0)]
        cmd = _cmd(front, back)
        # Two file inputs (front + back), no black source needed.
        self.assertEqual(cmd.count("-i"), 2)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("vstack=inputs=2", fc)
        self.assertNotIn("color=c=black", fc)
        # Output clamped to target duration.
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_missing_camera_inserts_black_and_silence(self) -> None:
        # Front missing a middle slot → black video; when audio=front, silence.
        front = [
            Span("video", 0.0, 30.0, merge="F.mp4", source_ss=0.0),
            Span("black", 30.0, 30.0),
        ]
        back = [Span("video", 0.0, 60.0, merge="B.mp4", source_ss=0.0)]
        cmd = _cmd(front, back, audio="front")
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("color=c=black:s=1080x608", fc)
        self.assertIn("anullsrc", fc)
        self.assertIn("concat=n=2:v=1:a=0", fc)  # front lane: video + black

    def test_seek_offset_passed_to_input(self) -> None:
        front = [Span("video", 0.0, 60.0, merge="F.mp4", source_ss=12.5)]
        back = [Span("video", 0.0, 60.0, merge="B.mp4", source_ss=0.0)]
        cmd = _cmd(front, back)
        # -ss for the front input reflects the media offset.
        self.assertIn("12.500", cmd)

    def test_black_only_lane_uses_color_source(self) -> None:
        front = [Span("black", 0.0, 60.0)]
        back = [Span("video", 0.0, 60.0, merge="B.mp4", source_ss=0.0)]
        cmd = _cmd(front, back)
        self.assertEqual(cmd.count("-i"), 1)  # only back is a real input
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("color=c=black:s=1080x608", fc)

    def test_run_compose_requires_timeline_manifest(self) -> None:
        from datetime import datetime
        from unittest import mock

        with mock.patch(
            "compose_2cam_70mai.build_aligned_lanes", return_value=None
        ), mock.patch(
            "compose_2cam_70mai.merges_timeline_ready",
            return_value=(False, "PA_front.mp4 missing timeline manifest"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                run_compose_2cam(
                    Path("video/Output"),
                    Path("out.mp4"),
                    wall_start=datetime(2025, 8, 10, 8, 50, 33),
                    duration=60.0,
                    record_type="Parking",
                    width=320,
                    crf=28,
                    preset="veryfast",
                    fps=25,
                    hw=False,
                    hw_quality=50,
                    hw_decode=False,
                    use_vt_scale=False,
                    codec="h264",
                    dry_run=True,
                )
        self.assertIn("timeline manifest", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
