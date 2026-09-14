import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops
import timeline_engine as renderer
from style_presets import (TITLE_STYLE_PRESETS, SUBTITLE_STYLE_PRESETS, get_style_preset,
                           get_title_render_overrides, get_subtitle_render_overrides,
                           restyle_subtitle_ass)
from title_visual_renderer import (title_visual_segments, build_title_scene,
                                   build_subtitle_scene, ass_header, scene_events)
from style_preview import render_style_preview, PREVIEW_TITLE
from subtitle_service import write_ass
from video_engine import title_config_from_settings

BASELINE = 'ba8a0ffcd241236dc10eee72f49d302ac945dc1b'


def text_events(content):
    return [line.split(',', 9) for line in content.splitlines()
            if line.startswith('Dialogue: 2,')]


def visible_text(raw):
    return re.sub(r'\{[^}]*\}', '', raw).replace(r'\N', '\n')


class CompositeStyleTests(unittest.TestCase):
    def test_legacy_and_invalid_never_enter_visual_renderer(self):
        for style in (None, '', 'legacy', 'missing', {}, 10):
            self.assertEqual(get_title_render_overrides(style, 1080, 1920), {})
            self.assertEqual(get_subtitle_render_overrides(style, 1080, 1920), {})
            self.assertEqual(restyle_subtitle_ass('original.ass', 'unused.ass', style, 1080, 1920), 'original.ass')

    def test_old_project_and_retired_ids(self):
        self.assertEqual(title_config_from_settings({'title_main': 'Original'})['title_style'], 'legacy')
        self.assertEqual(get_style_preset('title', 'black_yellow')['id'], 'yellow_white')
        self.assertEqual(get_style_preset('title', 'white_black_bg')['id'], 'brown_gold')
        self.assertEqual(get_style_preset('subtitle', 'yellow_dark')['id'], 'white_dark')

    def test_visual_segments_are_lossless_source_ranges(self):
        titles = [PREVIEW_TITLE, '新手做短视频一天也能出100条', '只需￥99.50，提升50%效率！',
                  '  完整 保留\n每一个字符。 ', 'AI software for everyone, save 25%', '你好',
                  '含有{原字符}和\\反斜杠', '🎬全新功能上线', '']
        for text in titles:
            for strategy in ('last_emphasis', 'badge_emphasis'):
                pieces = title_visual_segments(text, strategy)
                self.assertEqual(''.join(p['text'] for p in pieces), text)
                cursor = 0
                for piece in pieces:
                    self.assertEqual(piece['start'], cursor)
                    self.assertEqual(text[piece['start']:piece['end']], piece['text'])
                    cursor = piece['end']
                self.assertEqual(cursor, len(text))
        pieces = title_visual_segments('新手做短视频一天也能出100条')
        self.assertEqual(next(p['text'] for p in pieces if p['role'] == 'highlight'), '100条')

    def test_six_compositions_not_color_swaps(self):
        self.assertEqual(TITLE_STYLE_PRESETS[0]['id'], 'legacy')
        self.assertEqual(len(TITLE_STYLE_PRESETS), 7)
        signatures = set()
        for preset in TITLE_STYLE_PRESETS[1:]:
            scene = build_title_scene(preset, {'text': PREVIEW_TITLE}, 1080, 1920)
            texts = [n for n in scene['nodes'] if n['type'] == 'text']
            features = [len({n['size'] for n in texts}) >= 2,
                        len({n['color'] for n in texts}) >= 2,
                        any(n['role'] == 'decoration' for n in scene['nodes']),
                        any(n['role'] == 'panel' for n in scene['nodes']),
                        any(n.get('shadow', 0) > 0 for n in texts),
                        any(n.get('radius', 0) > 0 for n in scene['nodes'])]
            self.assertGreaterEqual(sum(features), 3, preset['id'])
            signatures.add(json.dumps([(n['type'], n['role'], round(n['x']), round(n['y']), round(n['width'])) for n in scene['nodes']]))
        self.assertEqual(len(signatures), 6)

    def test_long_titles_fit_all_aspects_without_rewriting(self):
        for width, height in ((1080, 1920), (1920, 1080), (1080, 1080), (360, 640)):
            for preset in TITLE_STYLE_PRESETS[1:]:
                for text in (PREVIEW_TITLE, '这是一条完整保留文字并且需要合理换行的长标题' * 6, 'A long English title with spaces and 100% original words ' * 3):
                    title = {'text': text, 'sub_text': '副标题也必须完整保留'}
                    before = copy.deepcopy(title)
                    scene = build_title_scene(preset, title, width, height)
                    self.assertEqual(title, before)
                    self.assertLessEqual(scene['line_count'], preset['max_lines'])
                    for key in title:
                        pieces = [n for n in scene['nodes'] if n['type'] == 'text' and n['source'] == key]
                        self.assertEqual(''.join(n['text'] for n in pieces), title[key])
                    for node in scene['nodes']:
                        self.assertGreaterEqual(node['x'], 0)
                        self.assertGreaterEqual(node['y'], height * preset['safe_top'] - 1)
                        self.assertLessEqual(node['x'] + node['width'], width + 1)
                        self.assertLessEqual(node['y'] + node['height'], height * preset['safe_bottom'] + 1)

    def test_subtitles_have_panels_and_local_emphasis(self):
        signatures = set()
        for preset in SUBTITLE_STYLE_PRESETS[1:]:
            scene = build_subtitle_scene(preset, '一天也能出100条', 1080, 1920)
            nodes = scene['nodes']
            self.assertEqual(''.join(n['text'] for n in nodes if n['type'] == 'text'), '一天也能出100条')
            signatures.add(json.dumps([(n['type'], n['role'], n['color'], n.get('radius', 0), n.get('shadow', 0)) for n in nodes]))
        self.assertEqual(len(signatures), 6)
        scene = build_subtitle_scene(get_style_preset('subtitle', 'white_dark'), '字幕原文', 1080, 1920)
        panel = next(n for n in scene['nodes'] if n['type'] == 'rect')
        self.assertGreater(panel['radius'], 0)
        self.assertLess(panel['opacity'], 100)

    def test_subtitle_cached_source_and_timing_are_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / 'source.ass'
            style = dict(font='Microsoft YaHei', size=78, color='&H00FFFFFF', outline_color='&H00000000', outline=1, bold=1, margin_v=180, max_chars_per_line=14, effect='pop', effect_duration=160)
            segments = [{'start': .15, 'end': .8, 'text': '第一段字幕 第二段字幕100条'}]
            original = copy.deepcopy(segments)
            with patch('subtitle_service.current_ass_style', return_value=style):
                write_ass(segments, source)
            before = source.read_bytes()
            original_events = [line.split(',', 9) for line in source.read_text(encoding='utf-8-sig').splitlines() if line.startswith('Dialogue:')]
            for preset in SUBTITLE_STYLE_PRESETS[1:]:
                result = Path(restyle_subtitle_ass(source, folder / 'styled.ass', preset['id'], 720, 1280))
                styled_events = text_events(result.read_text(encoding='utf-8-sig'))
                self.assertEqual({tuple(e[1:3]) for e in original_events}, {tuple(e[1:3]) for e in styled_events})
                for event in original_events:
                    parts = [e for e in styled_events if e[1:3] == event[1:3]]
                    self.assertEqual(''.join(visible_text(e[-1]) for e in parts), visible_text(event[-1]).replace('\n', ''))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(segments, original)

    def test_title_ass_keeps_original_time_window(self):
        with tempfile.TemporaryDirectory() as folder:
            for preset in TITLE_STYLE_PRESETS[1:]:
                title = dict(text=PREVIEW_TITLE, sub_text='副标题', start_time=2.2, end_time=4.8, title_style=preset['id'])
                original = copy.deepcopy(title)
                path = Path(folder) / 'title.ass'
                renderer.render_title_ass(title, {'start_time': 2}, 3, 1080, 1920, path)
                events = text_events(path.read_text(encoding='utf-8-sig'))
                self.assertEqual(''.join(visible_text(e[-1]) for e in events), PREVIEW_TITLE + '副标题')
                self.assertTrue(all(e[1:3] == ['0:00:00.20', '0:00:02.80'] for e in events))
                self.assertEqual(title, original)

    def test_real_render_legacy_pixels_and_exact_card_frames(self):
        ffmpeg = shutil.which('ffmpeg')
        self.assertIsNotNone(ffmpeg)
        baseline = subprocess.check_output(['git', 'show', f'{BASELINE}:timeline_engine.py'], text=True, encoding='utf-8')
        namespace = {'__name__': 'baseline_timeline'}
        exec(compile(baseline, 'baseline_timeline.py', 'exec'), namespace)
        output = Path(tempfile.mkdtemp(prefix='premium-style-smoke-'))
        def run(args):
            subprocess.run([ffmpeg, '-v', 'error', '-y', *args], check=True, capture_output=True)
        run(['-f', 'lavfi', '-i', 'color=c=0x30383C:s=720x1280:r=15:d=1', str(output / 'source.mp4')])
        run(['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '1', str(output / 'voice.wav')])
        subtitle = output / 'source.ass'
        style = dict(font='Microsoft YaHei', size=78, color='&H00FFFFFF', outline_color='&H00000000', outline=1, bold=1, margin_v=180, max_chars_per_line=14, effect='none')
        with patch('subtitle_service.current_ass_style', return_value=style):
            write_ass([{'start': 0, 'end': 1, 'text': '一天也能出100条'}], subtitle)
        timeline = dict(width=720, height=1280, fps=15, color_filter_enabled=False)
        video = dict(source=str(output / 'source.mp4'), start_time=0, end_time=1)
        audio = dict(source=str(output / 'voice.wav'))
        title = dict(text=PREVIEW_TITLE, start_time=0, end_time=1, auto_fit=True)
        execute = lambda cmd: subprocess.run(cmd, check=True, capture_output=True)
        namespace['run_ffmpeg'] = execute
        namespace['render_segment'](ffmpeg, timeline, video, audio, {'source': str(subtitle)}, None, title, output / 'baseline.mp4')
        with patch.object(renderer, 'run_ffmpeg', side_effect=execute):
            for preset in TITLE_STYLE_PRESETS:
                renderer.render_segment(ffmpeg, {**timeline, 'subtitle_style': 'legacy' if preset['id'] == 'legacy' else 'blue_white'}, video, audio, {'source': str(subtitle)}, None, {**title, 'title_style': preset['id']}, output / (preset['id'] + '.mp4'))
        raw = lambda name: subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(output / (name + '.mp4')), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        self.assertEqual(raw('baseline'), raw('legacy'))
        self.assertNotEqual(raw('yellow_white'), raw('black_gold'))
        for preset in TITLE_STYLE_PRESETS:
            name = preset['id']
            run(['-i', str(output / (name + '.mp4')), '-frames:v', '1', str(output / (name + '.png'))])
            if name == 'legacy':
                continue
            # Direct final-render ASS, decoded losslessly and cropped in precisely
            # the same way as UI thumbnails. This catches preview drift pixelwise.
            ass = output / (name + '.title.ass')
            final_frame = output / (name + '-direct.png')
            run(['-f', 'lavfi', '-i', 'color=c=0x30383C:s=720x1280:d=1', '-vf', f"ass='{renderer.ffmpeg_filter_path(ass)}',crop=720:360:0:30", '-frames:v', '1', str(final_frame)])
            preview = render_style_preview('title', name, output / 'cards', ffmpeg)
            self.assertIsNone(ImageChops.difference(Image.open(preview), Image.open(final_frame)).getbbox())
        # Real long title pixels must stay inside the configured safe area.
        for preset in TITLE_STYLE_PRESETS[1:]:
            ass = output / 'long.ass'
            renderer.render_title_ass({**title, 'text': '长标题完整保留并且不能溢出安全区域' * 5, 'title_style': preset['id']}, {}, 1, 720, 1280, ass)
            run(['-f', 'lavfi', '-i', 'color=c=0x30383C:s=720x1280:d=1', '-vf', f"ass='{renderer.ffmpeg_filter_path(ass)}'", '-frames:v', '1', str(output / 'long.png')])
            frame = Image.open(output / 'long.png').convert('RGB')
            backdrop = Image.new('RGB', frame.size, frame.getpixel((0, 0)))
            bounds = ImageChops.difference(frame, backdrop).getbbox()
            self.assertIsNotNone(bounds)
            self.assertGreaterEqual(bounds[0], 720 * .04)
            self.assertGreaterEqual(bounds[1], 1280 * .05)
            self.assertLessEqual(bounds[2], 720 * .96)
            self.assertLessEqual(bounds[3], 1280 * .45)
        print('SMOKE_ARTIFACTS=' + str(output))


if __name__ == '__main__':
    unittest.main()
