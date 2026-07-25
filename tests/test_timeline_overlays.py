import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timeline_engine import build_timeline_from_shots, render_title_ass
from subtitle_service import display_units, logical_wrap, prepare_ass_segments, write_ass


class TimelineOverlayTests(unittest.TestCase):
    def test_space_markers_cut_captions_and_long_caption_wraps(self):
        prepared = prepare_ass_segments([{
            "start": 0,
            "end": 5,
            "text": "如果你还在通过层层代理找工厂 那你已经慢了一步",
        }])
        self.assertEqual(len(prepared), 2)
        self.assertEqual(prepared[0]["text"], "如果你还在通过层层代理找\n工厂")
        self.assertEqual(prepared[1]["text"], "那你已经慢了一步")
        self.assertAlmostEqual(prepared[0]["start"], 0)
        self.assertAlmostEqual(prepared[-1]["end"], 5)
        for left, right in zip(prepared, prepared[1:]):
            self.assertAlmostEqual(left["end"], right["start"])

    def test_space_markers_become_sequential_timed_captions(self):
        prepared = prepare_ass_segments([{
            "start": 0,
            "end": 8,
            "text": "如果你还在通过层层代理找工厂 那你已经慢了一步 我们直接对接品牌客户。",
        }])
        self.assertEqual(len(prepared), 3)
        self.assertAlmostEqual(prepared[0]["start"], 0)
        self.assertAlmostEqual(prepared[-1]["end"], 8)
        self.assertIn("\n", prepared[0]["text"])

    def test_long_copy_is_presegmented_into_sequential_ass_captions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adaptive.ass"
            write_ass([{
                "start": 0,
                "end": 4,
                "text": "这是一条特别特别长而且需要根据画面宽度自动换行显示的字幕文案",
            }], str(path))
            content = path.read_text(encoding="utf-8-sig")
        # The current product rule inserts semantic space markers before ASS
        # rendering, so long copy becomes readable sequential captions instead
        # of one mechanically balanced two-line block.
        self.assertGreater(content.count("Dialogue:"), 1)

    def test_user_line_limit_uses_greedy_not_balanced_wrapping(self):
        text = "第一行需要尽量显示得更长接近边缘之后才换行"
        wrapped = logical_wrap(text, 14).splitlines()
        self.assertEqual(len(wrapped[0]), 14)
        self.assertLess(len(wrapped[1]), len(wrapped[0]))

    def test_bgm_and_title_are_real_enabled_timeline_items(self):
        shots = [{
            "shot_id": 1,
            "shot_name": "shot",
            "voice_duration": 5,
            "material_start_time": 2,
            "material_end_time": 7,
            "material_path": "video.mp4",
            "voice_file_path": "voice.wav",
            "enable_subtitle": 0,
        }]
        timeline = build_timeline_from_shots(
            shots,
            bgm={"source": "music.mp3", "volume": 0.15, "enabled": True},
            title_text={"main": "主标题", "sub": "副标题", "font_size": 120},
        )
        bgm = next(i for i in timeline["items"] if i["type"] == "bgm")
        title = next(i for i in timeline["items"] if i["type"] == "text")
        self.assertTrue(bgm["enabled"])
        self.assertEqual(bgm["end_time"], 5.8)
        self.assertTrue(title["enabled"])
        self.assertEqual(title["text"], "主标题")
        self.assertEqual(title["sub_text"], "副标题")
        self.assertEqual(title["end_time"], 5.8)

    def test_title_ass_keeps_each_title_on_one_line(self):
        title = {
            "text": "打造爆款就选亿康源",
            "sub_text": "免费打样OEM/ODM",
            "start_time": 0,
            "end_time": 5,
            "font_size": 120,
            "main_x": 50,
            "main_y": 13,
            "sub_x": 50,
            "sub_y": 21,
            "center": True,
        }
        video = {"start_time": 0, "end_time": 5}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "title.ass"
            render_title_ass(title, video, 5, 1080, 1920, path)
            content = path.read_text(encoding="utf-8-sig")
        self.assertIn("WrapStyle: 2", content)
        self.assertIn(r"\q2", content)
        self.assertNotIn(r"\N", content)
        self.assertIn("打造爆款就选亿康源", content)
        self.assertIn("免费打样OEM/ODM", content)


    def test_title_background_and_real_font_size_are_written_to_ass(self):
        title = {
            "text": "TITLE",
            "sub_text": "OEM/ODM",
            "start_time": 0,
            "end_time": 5,
            "font_size": 108,
            "auto_fit": False,
            "background_enabled": True,
            "background_color": "#16B8B8",
            "background_opacity": 72,
            "background_padding_y": 22,
            "main_y": 13,
            "sub_y": 21,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "title-box.ass"
            render_title_ass(title, {"start_time": 0}, 5, 1080, 1920, path)
            content = path.read_text(encoding="utf-8-sig")
        self.assertIn("Style: Title,Microsoft YaHei,108", content)
        self.assertIn("Style: Box", content)
        self.assertIn(r"\p1", content)
        self.assertIn(r"\fscx100", content)

    def test_popular_subtitle_effects_write_real_ass_animation_tags(self):
        base_style = {
            "font": "Microsoft YaHei",
            "size": 78,
            "color": "&H00FFFFFF",
            "outline_color": "&H00000000",
            "outline": 2,
            "bold": 1,
            "margin_v": 446,
            "effect_duration": 160,
        }
        expected = {
            "fade": r"\fad(",
            "pop": r"\fscx60",
            "zoom": r"\fscx78",
            "slide_up": r"\move(540,",
            "slide_left": r"\move(-200,",
        }
        with tempfile.TemporaryDirectory() as directory:
            for effect, token in expected.items():
                style = {**base_style, "effect": effect}
                path = Path(directory) / f"{effect}.ass"
                with patch("subtitle_service.current_ass_style", return_value=style):
                    write_ass([{"start": 0, "end": 2, "text": "caption"}], str(path))
                self.assertIn(token, path.read_text(encoding="utf-8-sig"), effect)


if __name__ == "__main__":
    unittest.main()
