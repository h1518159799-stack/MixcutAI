"""Visual-only template definitions. IDs remain compatible with saved settings."""
from copy import deepcopy


def _title(style_id, name, layout, strategy, primary, accent, *, align='center',
           background=None, decoration=None, highlight=None, badge=None,
           stroke='#201C18', outline=1.5, shadow=2, size=92, emphasis=136,
           primary_weight=700, shadow_opacity=65, shadow_blur=0,
           padding_y=None, line_spacing=.013, decoration_space=26):
    return dict(
        id=style_id, name=name, layout_type=layout, segment_strategy=strategy,
        font='Microsoft YaHei', safe_width=.88, safe_top=.065, safe_bottom=.44,
        auto_font_scale=True, max_lines=5, line_break_strategy='punctuation_words_then_chars',
        alignment=align, line_spacing=line_spacing, padding=.028,
        padding_y=.028 if padding_y is None else padding_y, decoration_space=decoration_space,
        primary_style=dict(color=primary, size=size, weight=primary_weight, stroke=stroke, outline=outline, shadow=shadow, shadow_opacity=shadow_opacity, shadow_blur=shadow_blur),
        secondary_style=dict(color=primary, size=66, weight=400, stroke=stroke, outline=outline, shadow=shadow),
        highlight_style=dict(color=accent, size=emphasis, weight=700, stroke=stroke, outline=outline, shadow=shadow, shadow_opacity=shadow_opacity, shadow_blur=shadow_blur, **(highlight or {})),
        badge_style=dict(color=accent, size=46, weight=400, stroke=stroke, outline=0, shadow=0, **(badge or {})),
        background_style=background or {}, decoration_style=decoration or {},
    )


TITLE_STYLE_PRESETS = [
    dict(id='legacy', name='原有样式', text_color='#FFE67A', outline_color='#000000', outline_width='1'),
    _title('yellow_white', '高端黄白冲击', 'double_center', 'last_emphasis', '#EEE5D5', '#DDC594',
           stroke='#39332E', outline=1.65, shadow=1.2, shadow_opacity=58, shadow_blur=3.2,
           size=76, primary_weight=400, emphasis=144, line_spacing=.017,
           decoration=dict(kind='short_rule', color='#AB9B7C', thickness=1.4, width=.12, emphasis_width_ratio=.30, opacity=65)),
    _title('black_gold', '黑金质感', 'badge_plus_title', 'badge_emphasis', '#EEE7DA', '#DDC597',
           align='left', size=82, emphasis=122, outline=.65, shadow=1, padding_y=.014, decoration_space=20,
           background=dict(color='#202121', opacity=64, radius=12, gradient_to=55),
           badge=dict(background='#28241D', border='#A58B5E', radius=3, padding=12),
           decoration=dict(kind='double_rule', color='#AD9C7B', thickness=.9, width=.92, opacity=42)),
    _title('red_white', '红白爆款', 'highlight_block', 'last_emphasis', '#FFF8F1', '#FFF8F1',
           align='center', size=86, emphasis=112, outline=.6, shadow=2,
           highlight=dict(background='#B63932', radius=16, padding=18),
           decoration=dict(kind='corner', color='#B63932', thickness=6, width=.075)),
    _title('white_black', '极简高级白', 'editorial_left', 'last_emphasis', '#F8F7F2', '#CED9DF',
           align='left', size=92, emphasis=112, outline=.6, shadow=1,
           decoration=dict(kind='editorial_rule', color='#CED9DF', thickness=2, width=.16)),
    _title('blue_white', '蓝白科技', 'tech_frame', 'badge_emphasis', '#F1F7FC', '#75CBE5',
           align='center', size=84, emphasis=122, outline=.8, shadow=1,
           background=dict(color='#102738', opacity=78, radius=8),
           badge=dict(background='#1C3D50', border='#51849A', radius=3, padding=10),
           decoration=dict(kind='brackets', color='#75CBE5', thickness=3, width=.065)),
    _title('brown_gold', '奶油棕金', 'soft_card', 'last_emphasis', '#756052', '#60483B',
           align='center', size=84, emphasis=120, outline=0, shadow=0,
           background=dict(color='#F2E8D8', opacity=96, radius=34),
           decoration=dict(kind='soft_pill', color='#BDA47C', thickness=7, width=.14)),
]

# Retired color-only IDs resolve to a designed template, never a missing item.
TITLE_STYLE_ALIASES = {'black_yellow': 'yellow_white', 'white_black_bg': 'brown_gold'}
SUBTITLE_STYLE_ALIASES = {'yellow_dark': 'white_dark', 'black_yellow': 'highlight'}


def _subtitle(style_id, name, *, accent=None, panel=None, block=None, outline=1.5, shadow=1, color='#F8F8F5'):
    return dict(id=style_id, name=name, font='Microsoft YaHei', font_size_ratio=.062,
                text_color=color, stroke_color='#16191D', stroke_width=outline / 1080,
                shadow=shadow, highlight_color=accent, background_style=panel or {},
                highlight_style=block or {}, safe_width=.86, margin_bottom=.095)


SUBTITLE_STYLE_PRESETS = [
    dict(id='legacy', name='原有样式', text_color='#FFFFFF', outline_color='#000000', outline_width='1'),
    _subtitle('white_black', '电影白字', outline=.8, shadow=3),
    _subtitle('yellow_black', '高级暖色', color='#F0E8DA', accent='#DDC594',
              panel=dict(color='#28231F', opacity=64, radius=7, padding=8), outline=.65),
    _subtitle('white_dark', '黑金字幕', color='#EFEADF', accent='#DDC597',
              panel=dict(color='#212222', opacity=62, radius=6, padding=8), outline=.5),
    _subtitle('highlight', '标签字幕', accent='#222B2C', block=dict(background='#D7E6CA', radius=7, padding=6), outline=.5),
    _subtitle('red_white', '高级红白', accent='#E18C81', block=dict(background='#542723', radius=4, padding=6), outline=1),
    _subtitle('blue_white', '科技蓝白', accent='#7BD5ED', panel=dict(color='#102E40', opacity=64, radius=7, padding=9), outline=.6),
]


def get_style_preset(kind, style_id):
    aliases = TITLE_STYLE_ALIASES if kind == 'title' else SUBTITLE_STYLE_ALIASES
    if not isinstance(style_id, str):
        return None
    style_id = aliases.get(style_id, style_id)
    presets = TITLE_STYLE_PRESETS if kind == 'title' else SUBTITLE_STYLE_PRESETS
    return next((deepcopy(p) for p in presets if p['id'] == style_id and style_id != 'legacy'), None)


def get_title_render_overrides(style_id, video_width, video_height, title=None):
    """A single conversion boundary; an empty result always means original path."""
    preset = get_style_preset('title', style_id)
    if preset is None:
        return {}
    from title_visual_renderer import build_title_scene
    return {'visual_scene': build_title_scene(preset, title or {}, video_width, video_height)}


def get_subtitle_render_overrides(style_id, video_width, video_height):
    preset = get_style_preset('subtitle', style_id)
    if preset is None:
        return {}
    return {'visual_preset': preset, 'width': video_width, 'height': video_height}


def restyle_subtitle_ass(source, output_path, style_id, video_width, video_height):
    overrides = get_subtitle_render_overrides(style_id, video_width, video_height)
    if not overrides:
        return str(source)
    from title_visual_renderer import render_subtitle_template
    return render_subtitle_template(source, output_path, **overrides)
