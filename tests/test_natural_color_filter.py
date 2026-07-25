import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timeline_engine import build_color_filter, build_timeline_from_shots, render_segment


class NaturalColorFilterTests(unittest.TestCase):
    def test_build_color_filter_presets(self):
        self.assertEqual("", build_color_filter(False, "standard"))
        self.assertEqual("", build_color_filter("0", "strong"))
        self.assertEqual(
            "eq=brightness=0.01:contrast=1.03:saturation=1.06:gamma=1.02",
            build_color_filter(True, "weak"),
        )
        self.assertEqual(
            "eq=brightness=0.015:contrast=1.06:saturation=1.10:gamma=1.03",
            build_color_filter("1", "standard"),
        )
        self.assertEqual(
            "eq=brightness=0.02:contrast=1.10:saturation=1.14:gamma=1.04",
            build_color_filter(True, "稍强"),
        )

    def test_unknown_strength_uses_standard(self):
        self.assertEqual(build_color_filter(True, "standard"), build_color_filter(True, "unknown"))

    def test_timeline_records_filter_selection(self):
        shots = [{
            "shot_id": 1,
            "shot_name": "测试镜头",
            "voice_duration": 2.0,
            "material_start_time": 0,
            "material_end_time": 2,
            "material_path": "video.mp4",
            "voice_file_path": "voice.wav",
        }]
        timeline = build_timeline_from_shots(
            shots, color_filter_enabled=True, color_filter_strength="strong"
        )
        self.assertTrue(timeline["color_filter_enabled"])
        self.assertEqual("strong", timeline["color_filter_strength"])

    def test_filter_is_after_crop_and_before_output_format(self):
        timeline = {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "color_filter_enabled": True,
            "color_filter_strength": "standard",
        }
        video = {"source": "video.mp4", "start_time": 0, "end_time": 2, "trim_start": 0}
        audio = {"source": "voice.wav", "start_time": 0, "end_time": 2, "volume": 1}
        with tempfile.TemporaryDirectory() as directory, patch("timeline_engine.run_ffmpeg") as run:
            render_segment("ffmpeg", timeline, video, audio, None, None, None, Path(directory) / "out.mp4")
        command = run.call_args.args[0]
        vf = command[command.index("-vf") + 1]
        self.assertLess(vf.index("crop=1080:1920"), vf.index("eq=brightness="))
        self.assertLess(vf.index("eq=brightness="), vf.index("format=yuv420p"))


if __name__ == "__main__":
    unittest.main()
