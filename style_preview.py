"""Cached real-libass thumbnails for the title/subtitle picker."""
import hashlib
import json
import subprocess
import tempfile
import threading
from pathlib import Path

from subprocess_utils import background_process_kwargs
from title_visual_renderer import ass_header, build_subtitle_scene, scene_events

_PREVIEW_LOCK = threading.Lock()
PREVIEW_TITLE = '有短视频需求就可以用 无算力限制'
PREVIEW_SUBTITLE = '新手一天也能出100条'


def render_style_preview(kind, style_id, directory, ffmpeg):
    from style_presets import get_style_preset
    from timeline_engine import ffmpeg_filter_path, render_title_ass
    if kind not in {'title', 'subtitle'}:
        raise ValueError('未知预览类型')
    preset = get_style_preset(kind, style_id)
    canonical = preset['id'] if preset else 'legacy'
    version = Path(__file__).read_bytes() + Path(__file__).with_name('title_visual_renderer.py').read_bytes()
    digest = hashlib.sha256(version + json.dumps([kind, canonical, preset], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f'style-card-{digest}.png'
    with _PREVIEW_LOCK:
        if output.is_file():
            return output
        with tempfile.TemporaryDirectory(prefix='style-card-', dir=directory) as folder:
            ass = Path(folder) / 'preview.ass'
            frame = Path(folder) / 'preview.png'
            if kind == 'title':
                render_title_ass(dict(text=PREVIEW_TITLE if preset else '原有标题样式', title_style=canonical, start_time=0, end_time=1), {}, 1, 720, 1280, ass)
                crop = 'crop=720:360:0:30'
            else:
                if preset:
                    scene = build_subtitle_scene(preset, PREVIEW_SUBTITLE, 720, 1280)
                    ass.write_text(ass_header(720, 1280) + '\n'.join(scene_events(scene, '0:00:00.00', '0:00:01.00')) + '\n', encoding='utf-8-sig')
                else:
                    # Match the legacy defaults without reading/changing user settings.
                    ass.write_text(ass_header(720, 1280) + 'Dialogue: 2,0:00:00.00,0:00:01.00,Visual,,0,0,0,,{\\an2\\pos(360,1158)\\fs48\\bord1}' + PREVIEW_SUBTITLE + '\n', encoding='utf-8-sig')
                crop = 'crop=720:200:0:1040'
            cmd = [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=0x30383C:s=720x1280:d=1',
                   '-vf', f"ass='{ffmpeg_filter_path(ass)}',{crop}", '-frames:v', '1', str(frame)]
            result = subprocess.run(cmd, capture_output=True, timeout=30, **background_process_kwargs(cmd, 'style_card_preview'))
            if result.returncode or not frame.is_file():
                raise ValueError('样式预览渲染失败')
            frame.replace(output)
    return output
