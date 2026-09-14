"""Composite ASS visuals shared by final burn-in and card previews.

Only this module lays out visual layers. It never writes project content or
changes the time range of a title/subtitle. Legacy never calls this renderer.
"""
import math
import os
import re
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont


_NUMBER = re.compile(r'(?:[¥￥$€]\s*)?\d+(?:[.,]\d+)*(?:\s*(?:%|％|元|万|亿|条|倍|天|秒|分钟|小时|个))?')
_KEYWORD = re.compile(r'无算力限制|不限次数|免费[^，。！？\s]{0,4}|一键[^，。！？\s]{0,4}|更高效|零基础')


def title_visual_segments(text, strategy='last_emphasis'):
    """Return ordered source slices; concatenation is exactly the input string."""
    text = str(text or '')
    if not text:
        return []
    match = list(_NUMBER.finditer(text)) or list(_KEYWORD.finditer(text))
    if match:
        start, end = match[-1].span()
    else:
        breaks = [m.end() for m in re.finditer(r'[\s，。！？；：,!?;:]+', text) if m.end() < len(text)]
        start = breaks[-1] if breaks else len(text) // 2 if len(text) > 7 else 0
        end = len(text)
    parts = []
    def add(a, b, role):
        if b > a:
            parts.append(dict(text=text[a:b], start=a, end=b, role=role))
    if strategy == 'badge_emphasis' and start > 5:
        # A badge is an original prefix, never a generated label.
        badge_end = next((m.end() for m in re.finditer(r'[\s，：, :]', text[:start]) if 2 <= m.end() <= 6), min(4, start))
        add(0, badge_end, 'badge')
        add(badge_end, start, 'primary')
    else:
        add(0, start, 'primary')
    add(start, end, 'highlight')
    add(end, len(text), 'secondary')
    assert ''.join(p['text'] for p in parts) == text
    return parts


@lru_cache(maxsize=8)
def _metric_font(bold):
    folder = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for name in (('msyhbd.ttc', 'msyh.ttc') if bold else ('msyh.ttc', 'msyhbd.ttc')):
        if (folder / name).is_file():
            return ImageFont.truetype(str(folder / name), 1000)
    return None


def text_width(text, size, weight=700):
    font = _metric_font(weight >= 600)
    text = text.replace('\r', '').replace('\n', '')
    if font:
        ascent, descent = font.getmetrics()
        # libass normalizes the font to its ascender+descender line height.
        return font.getlength(text) * size / (ascent + descent)
    return sum(.95 if ord(c) > 255 else .8 for c in text) * size


def _wrap(text, size, width, weight):
    chunks = []
    cursor = 0
    while cursor < len(text):
        end = cursor
        while end < len(text):
            candidate = text[cursor:end + 1]
            if end > cursor and text_width(candidate, size, weight) > width:
                break
            end += 1
            if text[end - 1] == '\n':
                break
        end = max(cursor + 1, end)
        if end < len(text) and text[end - 1] != '\n':
            # Prefer a word/clause boundary near the right edge; retain delimiter.
            boundaries = [m.end() for m in re.finditer(r'[\s，。！？；：,!?;:]', text[cursor:end])]
            if boundaries and boundaries[-1] >= (end - cursor) * .55:
                end = cursor + boundaries[-1]
        chunks.append((cursor, end, text[cursor:end]))
        cursor = end
    return chunks


def _rect(x, y, width, height, color, opacity=100, radius=0, role='panel', **extra):
    return dict(type='rect', x=x, y=y, width=width, height=height, color=color,
                opacity=opacity, radius=radius, role=role, **extra)


def build_title_scene(preset, title, width, height):
    width, height = float(width), float(height)
    edge = min(width, height)
    unit = edge / 1080
    segments = title_visual_segments(title.get('text', ''), preset['segment_strategy'])
    segments = [{**s, 'source': 'text'} for s in segments]
    if title.get('sub_text'):
        sub = str(title['sub_text'])
        segments.append(dict(text=sub, start=0, end=len(sub), role='secondary', source='sub_text'))
    if not segments:
        return dict(width=width, height=height, nodes=[], title_visual_segments=[], bounds={})
    safe_left = width * (1 - preset['safe_width']) / 2
    safe_top = height * preset['safe_top']
    safe_end = height * preset['safe_bottom']
    padding = edge * preset['padding']
    available_width = width * preset['safe_width'] - 2 * padding
    decor_space = 26 * unit
    gap = edge * preset['line_spacing']
    max_lines = max(preset['max_lines'], sum(s['text'].count('\n') + 1 for s in segments))
    scale = 1.0
    for _ in range(200):
        rows = []
        for segment in segments:
            style = preset[segment['role'] + '_style']
            size = max(.5, style['size'] * unit * scale)
            inset = style.get('padding', 0) * unit * scale
            for start, end, text in _wrap(segment['text'], size, available_width - 2 * inset, style['weight']):
                rows.append(dict(text=text, source=segment['source'], start=segment['start'] + start,
                                 end=segment['start'] + end, role=segment['role'], style=style,
                                 size=size, inset=inset, width=text_width(text, size, style['weight']),
                                 height=size + 2 * inset))
        total = sum(r['height'] for r in rows) + gap * scale * (len(rows) - 1)
        if len(rows) <= max_lines and total + 2 * padding + decor_space <= safe_end - safe_top:
            break
        scale *= .94
    else:
        raise ValueError('标题过长，无法在安全区域完整排版')
    panel_height = total + 2 * padding + decor_space
    nodes = []
    background = preset['background_style']
    if background:
        nodes.append(_rect(safe_left, safe_top, width * preset['safe_width'], panel_height,
                           background['color'], background.get('opacity', 100),
                           background.get('radius', 0) * unit, gradient_to=background.get('gradient_to')))
    y = safe_top + padding
    if preset['decoration_style'].get('kind') == 'editorial_rule':
        y += decor_space
    for row in rows:
        row_width = row['width'] + 2 * row['inset']
        x = safe_left + padding if preset['alignment'] == 'left' else (width - row_width) / 2
        style = row['style']
        if style.get('background'):
            nodes.append(_rect(x, y, row_width, row['height'], style['background'],
                               radius=style.get('radius', 0) * unit * scale,
                               role=row['role'] + '_block', border=style.get('border'), border_width=unit))
        nodes.append(dict(type='text', x=x + row['inset'], y=y + row['inset'],
                          width=row['width'], height=row['size'], text=row['text'],
                          source=row['source'], start=row['start'], end=row['end'], role=row['role'],
                          font=preset['font'], size=row['size'], color=style['color'], weight=style['weight'],
                          stroke=style['stroke'], outline=style['outline'] * unit * scale,
                          shadow=style['shadow'] * unit * scale))
        y += row['height'] + gap * scale
    # These are small compositional primitives, selected by configuration, not ID.
    decoration = preset['decoration_style']
    if decoration:
        kind = decoration['kind']
        color = decoration['color']
        length = edge * decoration['width']
        thickness = max(.5, decoration['thickness'] * unit)
        left, right, top, bottom = safe_left + padding, width - safe_left - padding, safe_top + padding / 2, safe_top + panel_height - padding / 2
        if kind in {'short_rule', 'soft_pill'}:
            nodes.append(_rect((width - length) / 2, bottom - thickness, length, thickness, color,
                               radius=thickness / 2 if kind == 'soft_pill' else 0, role='decoration'))
        elif kind == 'double_rule':
            nodes.extend([_rect(left, top, right - left, thickness, color, role='decoration'),
                          _rect(left, bottom, right - left, thickness, color, opacity=60, role='decoration')])
        elif kind == 'editorial_rule':
            nodes.append(_rect(left, top + padding / 2, length, thickness, color, role='decoration'))
        elif kind in {'brackets', 'corner'}:
            nodes.extend([_rect(safe_left, safe_top, length, thickness, color, role='decoration'),
                          _rect(safe_left, safe_top, thickness, length, color, role='decoration')])
            if kind == 'brackets':
                nodes.extend([_rect(width - safe_left - length, safe_top + panel_height - thickness, length, thickness, color, role='decoration'),
                              _rect(width - safe_left - thickness, safe_top + panel_height - length, thickness, length, color, role='decoration')])
    return dict(width=width, height=height, nodes=nodes, title_visual_segments=segments,
                layout_type=preset['layout_type'], font_scale=scale, line_count=len(rows),
                bounds=dict(left=safe_left, top=safe_top, right=width - safe_left, bottom=safe_top + panel_height))


def ass_color(color, opacity=100):
    value = color.lstrip('#')
    alpha = round(255 * (1 - opacity / 100))
    return f'&H{alpha:02X}{value[4:6]}{value[2:4]}{value[:2]}&'


def escape_text(text):
    # Literal backslashes and braces are protected using the existing ASS syntax.
    return text.replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}').replace('\r', '').replace('\n', r'\N')


def rounded_path(width, height, radius):
    w, h = float(width), float(height)
    r = min(max(0, radius), w / 2, h / 2)
    k = .55228475 * r
    if not r:
        return f'm 0 0 l {w:.3f} 0 {w:.3f} {h:.3f} 0 {h:.3f}'
    return (f'm {r:.3f} 0 l {w-r:.3f} 0 b {w-r+k:.3f} 0 {w:.3f} {r-k:.3f} {w:.3f} {r:.3f} '
            f'l {w:.3f} {h-r:.3f} b {w:.3f} {h-r+k:.3f} {w-r+k:.3f} {h:.3f} {w-r:.3f} {h:.3f} '
            f'l {r:.3f} {h:.3f} b {r-k:.3f} {h:.3f} 0 {h-r+k:.3f} 0 {h-r:.3f} '
            f'l 0 {r:.3f} b 0 {r-k:.3f} {r-k:.3f} 0 {r:.3f} 0')


def ass_header(width, height):
    return f'''[Script Info]
ScriptType: v4.00+
PlayResX: {int(width)}
PlayResY: {int(height)}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Visual,Microsoft YaHei,60,&H00FFFFFF,&H00FFFFFF,&H00181920,&H00181920,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''


def scene_events(scene, start, end, effect=''):
    events = []
    def emit(layer, tags, text, role):
        events.append(f'Dialogue: {layer},{start},{end},Visual,{role},0,0,0,,{{{effect}{tags}}}{text}')
    for node in scene['nodes']:
        x, y = node['x'], node['y']
        position = f'\\an7\\pos({x:.3f},{y:.3f})\\q2'
        if node['type'] == 'rect':
            path = rounded_path(node['width'], node['height'], node.get('radius', 0))
            border = node.get('border') or node['color']
            tags = position + f"\\p1\\bord{node.get('border_width', 0):.3f}\\shad0\\1c{ass_color(node['color'])}\\1a&H{round(255*(1-node['opacity']/100)):02X}&\\3c{ass_color(border)}\\3a&H{round(255*(1-node['opacity']/100)):02X}&"
            # A restrained opacity gradient uses clipped copies of one rounded shape.
            gradient = node.get('gradient_to')
            if gradient is None:
                emit(0, tags, path, node['role'])
            else:
                strips = 64
                for i in range(strips):
                    opacity = node['opacity'] + (gradient - node['opacity']) * i / (strips - 1)
                    top = y + node['height'] * i / strips
                    bottom = y + node['height'] * (i + 1) / strips
                    emit(0, tags + f"\\1a&H{round(255*(1-opacity/100)):02X}&\\clip({math.floor(x)},{round(top)},{math.ceil(x+node['width'])},{round(bottom)})", path, node['role'])
        else:
            tags = position + (f"\\fn{node['font']}\\fs{node['size']:.3f}\\b{node['weight']}"
                               f"\\1c{ass_color(node['color'])}\\3c{ass_color(node['stroke'])}"
                               f"\\bord{node['outline']:.3f}\\shad{node['shadow']:.3f}\\4c{ass_color('#17191C')}\\4a&H59&")
            emit(2, tags, escape_text(node['text']), node['role'])
    return events


def render_title_scene(scene, title, video, duration, output_path):
    from timeline_engine import ass_time
    start = max(0, float(title.get('start_time') or 0) - float(video.get('start_time') or 0))
    end = min(float(duration), float(title.get('end_time') or duration) - float(video.get('start_time') or 0))
    if end <= start or not scene['nodes']:
        return ''
    events = scene_events(scene, ass_time(start), ass_time(end))
    Path(output_path).write_text(ass_header(scene['width'], scene['height']) + '\n'.join(events) + '\n', encoding='utf-8-sig')
    return str(output_path)


def build_subtitle_scene(preset, text, width, height):
    """Keep existing hard line breaks and caption boundaries; emphasize within them."""
    edge = min(width, height)
    unit = edge / 1080
    size = edge * preset['font_size_ratio']
    lines = text.split('\n')
    panel = preset['background_style']
    inset = max(panel.get('padding', 0), preset['highlight_style'].get('padding', 0)) * unit
    max_width = width * preset['safe_width'] - 2 * inset
    widest = max((text_width(line, size) for line in lines), default=1)
    size *= min(1, max_width / max(1, widest))
    row_height = size * 1.1 + 2 * inset
    top = height * (1 - preset['margin_bottom']) - len(lines) * row_height
    nodes = []
    for index, line in enumerate(lines):
        line_width = text_width(line, size)
        left, y = (width - line_width) / 2, top + index * row_height + inset
        if panel:
            nodes.append(_rect(left - inset, y - inset, line_width + inset * 2, row_height,
                               panel['color'], panel['opacity'], panel['radius'] * unit))
        segments = title_visual_segments(line) if preset.get('highlight_color') else [dict(text=line, role='primary')]
        x = left
        for segment in segments:
            highlight = segment['role'] == 'highlight'
            color = preset['highlight_color'] if highlight else preset['text_color']
            block = preset['highlight_style'] if highlight else {}
            run_width = text_width(segment['text'], size)
            if block:
                pad = block['padding'] * unit
                nodes.append(_rect(x - pad, y - pad, run_width + 2 * pad, size + 2 * pad,
                                   block['background'], radius=block['radius'] * unit, role='highlight_block'))
            nodes.append(dict(type='text', x=x, y=y, width=run_width, height=size, text=segment['text'],
                              role=segment['role'], font=preset['font'], size=size, color=color,
                              weight=700, stroke=preset['stroke_color'], outline=edge * preset['stroke_width'],
                              shadow=preset['shadow'] * unit))
            x += run_width
    return dict(width=width, height=height, nodes=nodes)


def render_subtitle_template(source, output_path, visual_preset, width, height):
    content = Path(source).read_text(encoding='utf-8-sig')
    events = []
    in_events = False
    fields = []
    for line in content.splitlines():
        if line.startswith('['):
            in_events = line.strip() == '[Events]'
        if in_events and line.startswith('Format:'):
            fields = [f.strip().lower() for f in line.split(':', 1)[1].split(',')]
        if not in_events or not line.startswith('Dialogue:'):
            continue
        values = line.split(':', 1)[1].lstrip().split(',', len(fields) - 1)
        if len(values) != len(fields) or not {'start', 'end', 'text'}.issubset(fields):
            raise ValueError('字幕 ASS 事件格式无法读取')
        event = dict(zip(fields, values))
        raw = event['text']
        # Source content and event times are immutable; only visual ASS tags change.
        text = re.sub(r'\{[^}]*\}', '', raw).replace(r'\N', '\n').replace(r'\n', '\n').replace(r'\h', '\u00a0')
        # Keep fades/scale animations, but layout and positions belong to the preset.
        tags = ''.join(re.findall(r'\{([^}]*)\}', raw))
        effect = ''.join(re.findall(r'\\(?:fad\([^)]*\)|fsc[xy][\d.]+|t\([^)]*\))', tags))
        scene = build_subtitle_scene(visual_preset, text, width, height)
        events.extend(scene_events(scene, event['start'], event['end'], effect))
    if not events:
        # Empty subtitle tracks remain empty; do not synthesize sample captions.
        Path(output_path).write_text(ass_header(width, height), encoding='utf-8-sig')
    else:
        Path(output_path).write_text(ass_header(width, height) + '\n'.join(events) + '\n', encoding='utf-8-sig')
    return str(output_path)
