import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timeline_engine import build_timeline_from_shots, render_segment


class TimelineAudioTailTests(unittest.TestCase):
    def _shot(self, duration=5.0):
        return {
            "shot_id": 1,
            "shot_name": "尾音测试",
            "voice_duration": duration,
            "material_start_time": 0,
            "material_end_time": duration,
            "material_path": "video.mp4",
            "voice_file_path": "voice.wav",
            "enable_subtitle": 0,
        }

    def test_last_shot_adds_configured_hold(self):
        timeline = build_timeline_from_shots([self._shot()], end_hold_duration=0.8)
        self.assertAlmostEqual(timeline["last_voice_end"], 5.0)
        self.assertAlmostEqual(timeline["duration"], 5.8)
        video = next(item for item in timeline["items"] if item["type"] == "video")
        self.assertAlmostEqual(video["tail_padding"], 0.8)
        self.assertTrue(video["is_last"])

    def test_render_uses_clone_pad_audio_pad_and_no_shortest(self):
        timeline = build_timeline_from_shots([self._shot()], end_hold_duration=0.8)
        video = next(item for item in timeline["items"] if item["type"] == "video")
        audio = next(item for item in timeline["items"] if item["type"] == "audio")
        with tempfile.TemporaryDirectory() as directory, patch("timeline_engine.run_ffmpeg") as run:
            render_segment("ffmpeg", timeline, video, audio, None, None, None, Path(directory) / "out.mp4")
        command = run.call_args.args[0]
        self.assertNotIn("-shortest", command)
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("tpad=stop_mode=clone", video_filter)
        audio_filter = command[command.index("-filter_complex") + 1]
        self.assertIn("apad", audio_filter)
        self.assertIn("atrim=duration=5.800", audio_filter)
        self.assertNotIn("duration=first", audio_filter)

    def test_bgm_mix_uses_longest_and_final_fade(self):
        timeline = build_timeline_from_shots([self._shot()], end_hold_duration=0.8)
        video = next(item for item in timeline["items"] if item["type"] == "video")
        audio = next(item for item in timeline["items"] if item["type"] == "audio")
        bgm = {"source": "bgm.mp3", "volume": 0.15}
        with tempfile.TemporaryDirectory() as directory, patch("timeline_engine.run_ffmpeg") as run:
            render_segment("ffmpeg", timeline, video, audio, None, bgm, None, Path(directory) / "out.mp4")
        command = run.call_args.args[0]
        audio_filter = command[command.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2:duration=longest", audio_filter)
        self.assertIn("afade=t=out", audio_filter)


if __name__ == "__main__":
    unittest.main()
