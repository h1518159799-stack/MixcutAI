TITLE_STYLE_PRESETS = [
    {'id':'legacy','name':'原有样式','text_color':'#FFE67A','outline_color':'#000000','outline_width':'1'},
    {'id':'yellow_white','name':'黄白大字','text_color':'#FFE67A','outline_color':'#191919','outline_width':'3'},
    {'id':'black_gold','name':'黑金大字','text_color':'#F6D36A','outline_color':'#080808','outline_width':'3'},
    {'id':'red_white','name':'红白爆款','text_color':'#FF4D4D','outline_color':'#FFFFFF','outline_width':'3'},
    {'id':'white_black','name':'白字黑边','text_color':'#FFFFFF','outline_color':'#000000','outline_width':'3'},
    {'id':'blue_white','name':'蓝白科技','text_color':'#75C8FF','outline_color':'#FFFFFF','outline_width':'2'},
    {'id':'brown_gold','name':'棕金商务','text_color':'#E7C17A','outline_color':'#3B2417','outline_width':'3'},
    {'id':'black_yellow','name':'黑底黄字','text_color':'#FFE34D','outline_color':'#111111','outline_width':'2'},
    {'id':'white_black_bg','name':'白底黑字','text_color':'#111111','outline_color':'#FFFFFF','outline_width':'1'},
]
SUBTITLE_STYLE_PRESETS = [
    {'id':'legacy','name':'原有样式','text_color':'#FFFFFF','outline_color':'#000000','outline_width':'1'},
    {'id':'white_black','name':'白字黑边','text_color':'#FFFFFF','outline_color':'#000000','outline_width':'3'},
    {'id':'yellow_black','name':'黄字黑边','text_color':'#FFE67A','outline_color':'#000000','outline_width':'3'},
    {'id':'white_dark','name':'白字黑底','text_color':'#FFFFFF','outline_color':'#111111','outline_width':'1'},
    {'id':'yellow_dark','name':'黄字深底','text_color':'#FFE67A','outline_color':'#111111','outline_width':'1'},
    {'id':'red_white','name':'红字白底','text_color':'#FF4D4D','outline_color':'#FFFFFF','outline_width':'1'},
    {'id':'blue_white','name':'蓝字白底','text_color':'#4DA6FF','outline_color':'#FFFFFF','outline_width':'1'},
    {'id':'black_yellow','name':'黑字黄底','text_color':'#111111','outline_color':'#FFE34D','outline_width':'1'},
    {'id':'highlight','name':'关键词高亮','text_color':'#FFFFFF','outline_color':'#000000','outline_width':'2'},
]

# Visual presets use the short edge as their reference length. Legacy entries
# deliberately never participate in this mapping.
for _preset in TITLE_STYLE_PRESETS:
    if _preset['id'] != 'legacy':
        _preset.update(font='Microsoft YaHei', font_weight=700,
                       font_size_ratio=0.14, stroke_color=_preset['outline_color'],
                       stroke_width=float(_preset['outline_width']) / 1080,
                       shadow=0.002, background_type='none',
                       background_color='#111111', background_opacity=0,
                       padding=0.02, alignment='center', position='top',
                       max_width_ratio=0.92)
        if _preset['id'] in {'black_yellow', 'white_black_bg'}:
            _preset.update(background_type='band', background_opacity=90,
                           background_color='#111111' if _preset['id'] == 'black_yellow' else '#FFFFFF')
for _preset in SUBTITLE_STYLE_PRESETS:
    if _preset['id'] != 'legacy':
        _preset.update(font='Microsoft YaHei', font_weight=700,
                       font_size_ratio=0.072, stroke_color=_preset['outline_color'],
                       stroke_width=float(_preset['outline_width']) / 1080,
                       position='bottom', background_opacity=0)


def _preset_for(presets, style_id):
    return next((p for p in presets if p['id'] == style_id and style_id != 'legacy'), None)


def get_title_render_overrides(style_id, video_width, video_height, title=None):
    preset = _preset_for(TITLE_STYLE_PRESETS, style_id)
    if preset is None:
        return {}
    width, height = float(video_width), float(video_height)
    edge = min(width, height)
    size = edge * preset['font_size_ratio']
    # Shrink long titles, never truncate/rewrite their content. A conservative
    # glyph advance also leaves room for outline and shadow inside the safe area.
    longest = max((0.85 * len(str((title or {}).get(k) or ''))
                   for k in ('text', 'sub_text')), default=0)
    stroke = edge * preset['stroke_width']
    shadow = edge * preset['shadow']
    if longest:
        size = min(size, (width * preset['max_width_ratio'] - 2 * (stroke + shadow)) / longest)
    return dict(font=preset['font'], bold=-1 if preset['font_weight'] >= 600 else 0, font_size=max(1, int(size)),
                color=preset['text_color'], outline_color=preset['stroke_color'],
                outline_width=stroke, shadow_color='#191919', shadow_x=shadow,
                shadow_y=shadow, auto_fit=False,
                background_enabled=preset['background_type'] == 'band',
                background_color=preset['background_color'],
                background_opacity=preset['background_opacity'],
                background_padding_y=edge * preset['padding'],
                center=preset['alignment'] == 'center', main_x=50, sub_x=50,
                main_y=13, sub_y=13 + (edge * preset['font_size_ratio'] * 1.5 / height * 100))


def _ass_color(color):
    value = color.lstrip('#')
    return '&H00' + value[4:6] + value[2:4] + value[0:2]


def get_subtitle_render_overrides(style_id, video_width, video_height):
    preset = _preset_for(SUBTITLE_STYLE_PRESETS, style_id)
    if preset is None:
        return {}
    edge = min(float(video_width), float(video_height))
    return dict(Fontname=preset['font'], Fontsize=edge * preset['font_size_ratio'],
                PrimaryColour=_ass_color(preset['text_color']),
                OutlineColour=_ass_color(preset['stroke_color']),
                Outline=edge * preset['stroke_width'], Bold=-1 if preset['font_weight'] >= 600 else 0,
                BackColour='&HFF000000', BorderStyle=1, Shadow=0,
                Alignment=2, MarginV=float(video_height) * 0.09375)


def restyle_subtitle_ass(source, output_path, style_id, video_width, video_height):
    """Restyle the cached ASS at burn-in, without touching any dialogue bytes.

    Existing ASS coordinates (including animation positions) stay intact. Sizes
    are converted back to that coordinate system for the actual output aspect.
    """
    from pathlib import Path
    import re
    overrides = get_subtitle_render_overrides(style_id, video_width, video_height)
    if not overrides:
        return str(source)
    content = Path(source).read_text(encoding='utf-8-sig')
    match = re.search(r'^PlayResY:\s*(\d+)', content, re.M)
    play_height = int(match.group(1)) if match else 1920
    scale = play_height / float(video_height)
    for field in ('Fontsize', 'Outline', 'MarginV'):
        overrides[field] *= scale
    lines, fields, in_styles = [], [], False
    for line in content.splitlines(keepends=True):
        if line.startswith('['):
            in_styles = line.strip() == '[V4+ Styles]'
        if in_styles and line.startswith('Format:'):
            fields = [value.strip() for value in line.split(':', 1)[1].split(',')]
        if in_styles and line.startswith('Style:') and fields:
            ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
            values = line.split(':', 1)[1].strip().split(',')
            if len(values) == len(fields):
                values = [str(overrides.get(field, value)) for field, value in zip(fields, values)]
                line = 'Style: ' + ','.join(values) + ending
        lines.append(line)
    Path(output_path).write_text(''.join(lines), encoding='utf-8-sig')
    return str(output_path)
