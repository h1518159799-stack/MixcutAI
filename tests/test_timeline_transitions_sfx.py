import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from timeline_engine import (
    TRANSITION_FILTERS,
    build_timeline_from_shots,
    concat_segments_with_transitions,
)


class TimelineTransitionSfxTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = Path(handle.name)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE shot_transitions(
                    id INTEGER PRIMARY KEY,
                    from_shot_id INTEGER,
                    to_shot_id INTEGER,
                    effect TEXT,
                    duration REAL,
                    enabled INTEGER
                );
                CREATE TABLE sound_effect_library(
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    category TEXT,
                    keywords TEXT,
                    file_path TEXT,
                    duration REAL,
                    volume_default REAL
                );
                INSERT INTO shot_transitions VALUES(1, 11, 12, 'slide_left', 0.4, 1);
                INSERT INTO sound_effect_library VALUES(
                    1, 'shine', 'shine', 'show,new;upgrade', 'shine.wav', 0.8, 0.25
                );
                """
            )

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    def test_builds_transition_and_keyword_sfx_tracks(self):
        shots = [
            {
                "shot_id": 11,
                "shot_name": "opening",
                "voice_duration": 3.0,
                "material_path": "a.mp4",
                "voice_file_path": "a.wav",
                "original_text": "new product show",
            },
            {
                "shot_id": 12,
                "shot_name": "product",
                "voice_duration": 4.0,
                "material_path": "b.mp4",
                "voice_file_path": "b.wav",
                "original_text": "production",
            },
        ]
        with patch("db.connect", self.connect):
            timeline = build_timeline_from_shots(shots)
        items = timeline["items"]
        transition = next(item for item in items if item["type"] == "transition")
        sfx = next(item for item in items if item["type"] == "sfx")
        videos = [item for item in items if item["type"] == "video"]

        self.assertTrue(transition["enabled"])
        self.assertEqual(transition["effect"], "slide_left")
        self.assertAlmostEqual(transition["duration"], 0.4)
        # The transition now consumes padding rather than the final word of
        # either voice. The second voice starts after the transition completes.
        self.assertAlmostEqual(videos[1]["start_time"], 3.0)
        audio = next(item for item in items if item["type"] == "audio" and item["shot_id"] == 12)
        self.assertAlmostEqual(audio["voice_delay"], 0.4)
        self.assertAlmostEqual(timeline["last_voice_end"], 7.4)
        self.assertAlmostEqual(timeline["duration"], 8.2)
        self.assertEqual(sfx["source"], "shine.wav")
        self.assertAlmostEqual(sfx["volume"], 0.25)

    def test_all_requested_transition_effects_are_supported(self):
        self.assertTrue(
            {"fade", "slide_left", "slide_right", "zoom", "flash", "blur"}
            <= set(TRANSITION_FILTERS)
        )

    def test_disabled_transition_is_a_hard_cut_not_sub_frame_xfade(self):
        captured = []
        transitions = [
            {"effect": "fade", "duration": 0.4, "enabled": True},
            {"effect": "fade", "duration": 0.001, "enabled": False},
        ]
        with (
            patch("timeline_engine.probe_duration", side_effect=[2.0, 3.0, 4.0]),
            patch("timeline_engine.run_ffmpeg", side_effect=lambda cmd: captured.append(cmd)),
        ):
            concat_segments_with_transitions(
                "ffmpeg",
                ["one.mp4", "two.mp4", "three.mp4"],
                transitions,
                "output.mp4",
                ".",
            )

        command = captured[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade:duration=0.400", graph)
        self.assertIn("[v1][vin2]concat=n=2:v=1:a=0[v2]", graph)
        self.assertIn("[a1][ain2]concat=n=2:v=0:a=1[a2]", graph)
        self.assertNotIn("xfade=transition=fade:duration=0.001", graph)
        self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", graph)
        self.assertEqual(command[command.index("-t") + 1], "8.600")


if __name__ == "__main__":
    unittest.main()
