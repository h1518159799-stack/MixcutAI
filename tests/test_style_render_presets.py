import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import timeline_engine as renderer
from style_presets import (get_title_render_overrides, get_subtitle_render_overrides,
                           restyle_subtitle_ass)
from subtitle_service import write_ass
from video_engine import title_config_from_settings


class StyleRenderTests(unittest.TestCase):
    def test_legacy_and_unknown_have_no_overrides(self):
        for style in (None, '', 'legacy', 'missing'):
            self.assertEqual(get_title_render_overrides(style, 1080, 1920), {})
            self.assertEqual(get_subtitle_render_overrides(style, 1080, 1920), {})
            self.assertEqual(restyle_subtitle_ass('unchanged.ass', 'unused.ass', style, 1080, 1920), 'unchanged.ass')

    def test_old_project_falls_back(self):
        config = title_config_from_settings({'title_main': 'Original'})
        self.assertEqual(config['title_style'], 'legacy')
        self.assertEqual(config['font_size'], 118)
        self.assertEqual(config['color'], '#FFE67A')

    def test_title_mapping_and_long_title_bounds(self):
        title = {'text': '这是一个需要自动缩小但必须完整保留的超长视频标题' * 3}
        original = copy.deepcopy(title)
        for width, height in ((1080, 1920), (1920, 1080), (1080, 1080), (360, 640)):
            values = get_title_render_overrides('yellow_white', width, height, title)
            self.assertEqual(values['color'], '#FFE67A')
            self.assertEqual(values['outline_color'], '#191919')
            self.assertGreater(values['outline_width'], 0)
            self.assertEqual(values['main_y'], 13)
            self.assertTrue(values['center'])
            self.assertLessEqual(len(title['text']) * .85 * values['font_size'] + 2 * (values['outline_width'] + values['shadow_x']), width * .92)
            self.assertGreater(height * .13 - values['font_size'], height * .04)
        self.assertEqual(title, original)

    def test_subtitle_mapping(self):
        values = get_subtitle_render_overrides('white_black', 1080, 1920)
        self.assertEqual(values['PrimaryColour'], '&H00FFFFFF')
        self.assertEqual(values['OutlineColour'], '&H00000000')
        self.assertEqual(values['BackColour'], '&HFF000000')
        self.assertEqual(values['BorderStyle'], 1)
        self.assertEqual(values['Outline'], 3)

    def test_title_and_subtitle_content_times_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / 'source.ass'
            style = dict(font='Microsoft YaHei', size=78, color='&H00FFFFFF', outline_color='&H00000000', outline=1, bold=1, margin_v=180, max_chars_per_line=14, effect='pop', effect_duration=160)
            segments = [{'start': .15, 'end': .8, 'text': '第一段字幕 第二段字幕'}]
            original = copy.deepcopy(segments)
            with patch('subtitle_service.current_ass_style', return_value=style):
                write_ass(segments, source)
            before = source.read_bytes()
            result = Path(restyle_subtitle_ass(source, folder / 'styled.ass', 'white_black', 360, 640))
            events = lambda data: data.split('[Events]', 1)[1]
            self.assertEqual(events(source.read_text(encoding='utf-8-sig')), events(result.read_text(encoding='utf-8-sig')))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(segments, original)
            title = dict(text='原始标题', sub_text='副标题', start_time=.1, end_time=.8, title_style='yellow_white')
            original = copy.deepcopy(title)
            renderer.render_title_ass(title, {'start_time': 0}, 1, 1080, 1920, folder / 'title.ass')
            self.assertEqual(title, original)
            content = (folder / 'title.ass').read_text(encoding='utf-8-sig')
            self.assertIn('0:00:00.10,0:00:00.80', content)
            self.assertIn('原始标题', content)

    def test_real_render_and_legacy_pixel_identity(self):
        ffmpeg = shutil.which('ffmpeg')
        self.assertIsNotNone(ffmpeg, 'Real FFmpeg is required for this acceptance test')
        # Compare the pre-feature renderer from the checked-in revision, including
        # its original subtitle burn-in path, against the current legacy path.
        baseline = subprocess.check_output(['git', 'show', 'ba8a0ffcd241236dc10eee72f49d302ac945dc1b:timeline_engine.py'], text=True, encoding='utf-8')
        namespace = {'__name__': 'baseline_timeline'}
        exec(compile(baseline, 'baseline_timeline.py', 'exec'), namespace)
        output = Path(tempfile.mkdtemp(prefix='style-render-smoke-'))
        def run(args):
            subprocess.run([ffmpeg, '-loglevel', 'error', '-y', *args], check=True, capture_output=True)
        run(['-f', 'lavfi', '-i', 'color=c=0x334455:s=360x640:r=10:d=1', str(output / 'source.mp4')])
        run(['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '1', str(output / 'voice.wav')])
        style = dict(font='Microsoft YaHei', size=78, color='&H00FFFFFF', outline_color='&H00000000', outline=1, bold=1, margin_v=180, max_chars_per_line=14, effect='none')
        with patch('subtitle_service.current_ass_style', return_value=style):
            write_ass([{'start': 0, 'end': 1, 'text': '白字黑边字幕'}], output / 'source.ass')
        timeline = dict(width=360, height=640, fps=10, color_filter_enabled=False)
        video = dict(source=str(output / 'source.mp4'), start_time=0, end_time=1)
        audio = dict(source=str(output / 'voice.wav'))
        subtitle = dict(source=str(output / 'source.ass'))
        title = dict(text='黄白大字标题测试', font_size=36, start_time=0, end_time=1)
        # Execute the exact production FFmpeg command without task heartbeats.
        execute = lambda cmd: subprocess.run(cmd, check=True, capture_output=True)
        namespace['run_ffmpeg'] = execute
        namespace['render_segment'](ffmpeg, timeline, video, audio, subtitle, None, title, output / 'baseline.mp4')
        with patch.object(renderer, 'run_ffmpeg', side_effect=execute):
            renderer.render_segment(ffmpeg, {**timeline, 'subtitle_style': 'legacy'}, video, audio, subtitle, None, {**title, 'title_style': 'legacy'}, output / 'legacy.mp4')
            renderer.render_segment(ffmpeg, {**timeline, 'subtitle_style': 'white_black'}, video, audio, subtitle, None, {**title, 'title_style': 'yellow_white'}, output / 'styled.mp4')
        frames = {}
        for name in ('baseline', 'legacy', 'styled'):
            frames[name] = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(output / (name + '.mp4')), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
            run(['-i', str(output / (name + '.mp4')), '-frames:v', '1', str(output / (name + '.png'))])
        self.assertEqual(frames['baseline'], frames['legacy'])
        self.assertNotEqual(frames['legacy'], frames['styled'])
        # Both title and subtitle regions must change in the actual video.
        frame_size = 360 * 640 * 3
        a, b = frames['legacy'][:frame_size], frames['styled'][:frame_size]
        self.assertNotEqual(a[:360 * 220 * 3], b[:360 * 220 * 3])
        self.assertNotEqual(a[360 * 500 * 3:], b[360 * 500 * 3:])
        from PIL import Image
        frame = Image.open(output / 'styled.png').convert('RGB')
        yellow = [(x, y) for y in range(220) for x in range(360)
                  if (lambda c: c[0] > 180 and c[1] > 150 and c[2] < 160)(frame.getpixel((x, y)))]
        self.assertTrue(yellow)
        xs, ys = zip(*yellow)
        self.assertGreater(max(xs) - min(xs), 360 * .75)
        self.assertGreaterEqual(min(xs), 360 * .04)
        self.assertLessEqual(max(xs), 360 * .96)
        self.assertGreaterEqual(min(ys), 640 * .04)
        print('SMOKE_ARTIFACTS=' + str(output))


if __name__ == '__main__':
    unittest.main()
