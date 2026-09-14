import json
import shutil
import subprocess
import random
from pathlib import Path

from runtime_paths import MediaRuntimeError, get_ffprobe_path

SEGMENT_W = 1080
SEGMENT_H = 1920
FPS = 30


def normalize_video_encoder(value: str | None) -> str:
    """Return a renderer codec name while keeping legacy CPU behavior."""
    value = str(value or "libx264").strip().lower()
    return "h264_nvenc" if value in {"nvenc", "h264_nvenc"} else "libx264"


def video_codec_args(encoder: str | None) -> list[str]:
    codec = normalize_video_encoder(encoder)
    if codec == "h264_nvenc":
        return ["-c:v", codec, "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    return ["-c:v", codec, "-preset", "veryfast", "-crf", "23"]


class TimelineError(RuntimeError):
    pass


COLOR_FILTER_PRESETS = {
    "weak": "eq=brightness=0.01:contrast=1.03:saturation=1.06:gamma=1.02",
    "standard": "eq=brightness=0.015:contrast=1.06:saturation=1.10:gamma=1.03",
    "strong": "eq=brightness=0.02:contrast=1.10:saturation=1.14:gamma=1.04",
}


def build_color_filter(filter_enabled, filter_strength):
    """Return a restrained FFmpeg color filter, or an empty string when disabled."""
    enabled = str(filter_enabled if filter_enabled is not None else "1").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return ""
    aliases = {
        "weak": "weak", "弱": "weak",
        "standard": "standard", "标准": "standard",
        "strong": "strong", "稍强": "strong",
    }
    strength = aliases.get(str(filter_strength or "standard").strip().lower(), "standard")
    return COLOR_FILTER_PRESETS[strength]


def timeline_item(item_type, start_time, end_time, source="", layer=0, **extra):
    return {
        "type": item_type,
        "start_time": round(float(start_time or 0), 3),
        "end_time": round(float(end_time or 0), 3),
        "source": source or "",
        "layer": int(layer or 0),
        **extra,
    }


def build_timeline_from_shots(
    shots, bgm=None, title_text=None, width=SEGMENT_W, height=SEGMENT_H, fps=FPS,
    color_filter_enabled=True, color_filter_strength="standard",
    end_hold_duration=0.8, inter_shot_safety=0.15,
):
    """
    将数据库镜头记录转换成统一时间线。

    第一版实际渲染：
    - video
    - audio
    - subtitle

    预留：
    - bgm
    - text
    """
    items = []
    transition_map = {}
    sfx_library = []
    sfx_enabled = True
    try:
        from db import connect
        shot_ids = [int(s.get("shot_id") or 0) for s in shots if s.get("shot_id")]
        if shot_ids:
            marks = ",".join("?" for _ in shot_ids)
            with connect() as conn:
                transition_rows = conn.execute(
                    f"SELECT * FROM shot_transitions WHERE from_shot_id IN ({marks}) AND to_shot_id IN ({marks})",
                    shot_ids + shot_ids,
                ).fetchall()
                transition_map = {(r["from_shot_id"], r["to_shot_id"]): dict(r) for r in transition_rows}
                try:
                    sfx_setting = conn.execute("SELECT value FROM app_settings WHERE key='sfx_enabled'").fetchone()
                except Exception:
                    # Older/test databases have no app_settings table yet;
                    # preserve the historical default of enabled.
                    sfx_setting = None
                if sfx_setting is not None:
                    sfx_enabled = str(sfx_setting["value"] or "1").strip().lower() in {"1", "true", "yes", "on"}
                if sfx_enabled:
                    sfx_library = [dict(r) for r in conn.execute("SELECT * FROM sound_effect_library ORDER BY id").fetchall()]
    except Exception:
        transition_map, sfx_library = {}, []
    cursor = 0.0
    last_voice_end = 0.0
    end_hold_duration = max(0.0, float(end_hold_duration or 0))
    inter_shot_safety = max(0.1, min(0.2, float(inter_shot_safety or 0.15)))
    for index, shot in enumerate(shots, 1):
        voice_duration = float(shot.get("voice_duration") or 0)
        if voice_duration <= 0:
            raise TimelineError(f"镜头 {index} voice_duration 无效")
        transition = None
        if index > 1:
            previous = shots[index - 2]
            transition = transition_map.get((previous.get("shot_id"), shot.get("shot_id")))
        overlap = min(float(transition.get("duration") or 0), voice_duration * 0.45) if transition and transition.get("enabled") else 0.0
        outgoing_overlap = 0.0
        if index < len(shots):
            following = shots[index]
            outgoing = transition_map.get((shot.get("shot_id"), following.get("shot_id")))
            if outgoing and outgoing.get("enabled"):
                outgoing_overlap = min(
                    float(outgoing.get("duration") or 0),
                    voice_duration * 0.45,
                    float(following.get("voice_duration") or 0) * 0.45,
                )
        tail_padding = end_hold_duration if index == len(shots) else max(inter_shot_safety, outgoing_overlap)
        # Incoming transition occupies an initial silent pad so the next voice
        # starts after the crossfade instead of being faded or clipped.
        duration = overlap + voice_duration + tail_padding
        start = round(max(0.0, cursor - overlap), 3)
        end = round(start + duration, 3)
        if index > 1:
            previous = shots[index - 2]
            transition_data = transition or {"from_shot_id": previous.get("shot_id"), "to_shot_id": shot.get("shot_id"), "effect": "fade", "duration": 0.001, "enabled": 0}
            items.append(timeline_item("transition", start, start + max(overlap, 0.001), "", layer=40,
                from_shot_id=transition_data["from_shot_id"], to_shot_id=transition_data["to_shot_id"],
                effect=transition_data.get("effect") or "fade", duration=max(overlap, 0.001), enabled=bool(transition_data.get("enabled"))))
        material_start = float(shot.get("material_start_time") or 0)
        material_end = float(shot.get("material_end_time") or (material_start + duration))

        items.append(
            timeline_item(
                "video",
                start,
                end,
                shot.get("material_path") or "",
                layer=0,
                shot_id=shot.get("shot_id"),
                shot_name=shot.get("shot_name") or f"镜头{index}",
                trim_start=round(material_start, 3),
                trim_end=round(material_end, 3),
                voice_duration=round(voice_duration, 6),
                tail_padding=round(tail_padding, 3),
                incoming_transition=round(overlap, 3),
                is_last=index == len(shots),
                volume=0,
                width=shot.get("material_width"),
                height=shot.get("material_height"),
            )
        )
        items.append(
            timeline_item(
                "audio",
                start,
                end,
                shot.get("voice_file_path") or "",
                layer=10,
                shot_id=shot.get("shot_id"),
                shot_name=shot.get("shot_name") or f"镜头{index}",
                volume=1.0,
                voice_duration=round(voice_duration, 6),
                voice_delay=round(overlap, 3),
                tail_padding=round(tail_padding, 3),
            )
        )
        last_voice_end = round(start + overlap + voice_duration, 6)
        if int(shot.get("enable_subtitle") or 0) and shot.get("subtitle_file_path"):
            items.append(
                timeline_item(
                    "subtitle",
                    start,
                    end,
                    shot.get("subtitle_file_path") or "",
                    layer=20,
                    shot_id=shot.get("shot_id"),
                    shot_name=shot.get("shot_name") or f"镜头{index}",
                    format="ass",
                )
            )
        text = str(shot.get("ai_text") or shot.get("original_text") or "")
        matches = []
        for effect in sfx_library if sfx_enabled else []:
            keys = [
                k.strip().lower()
                for k in str(effect.get("keywords") or "")
                .replace("\uFF0C", ",")
                .replace("\uFF1B", ",")
                .replace(";", ",")
                .split(",")
                if k.strip()
            ]
            if any(key in text.lower() for key in keys):
                matches.append(effect)
        if matches:
            effect = random.choice(matches)
            sfx_duration = min(float(effect.get("duration") or 0.8), voice_duration)
            items.append(timeline_item("sfx", start, start + sfx_duration, effect.get("file_path") or "", layer=15,
                shot_id=shot.get("shot_id"), volume=float(effect.get("volume_default") or 0.25),
                category=effect.get("category") or "general", name=effect.get("name") or ""))
        cursor = end

    if bgm and bgm.get("source"):
        items.append(
            timeline_item(
                "bgm",
                0,
                cursor,
                bgm.get("source"),
                layer=5,
                volume=float(bgm.get("volume", 0.25)),
                enabled=bool(bgm.get("enabled", True)),
            )
        )

    if title_text:
        title = dict(title_text) if isinstance(title_text, dict) else {"main": str(title_text)}
        main = str(title.get("main") or "").strip()
        sub = str(title.get("sub") or "").strip()
        if not main and not sub:
            title = None
        if title:
            title.pop("main", None)
            title.pop("sub", None)
            items.append(
                timeline_item(
                    "text",
                    0,
                    cursor,
                    "",
                    layer=30,
                    text=main,
                    sub_text=sub,
                    enabled=True,
                    **title,
                )
            )

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration": round(cursor, 3),
        "last_voice_end": last_voice_end,
        "end_hold_duration": round(end_hold_duration, 3),
        "inter_shot_safety": round(inter_shot_safety, 3),
        "color_filter_enabled": bool(build_color_filter(color_filter_enabled, color_filter_strength)),
        "color_filter_strength": str(color_filter_strength or "standard"),
        "items": sorted(items, key=lambda x: (x["start_time"], x["layer"], x["type"])),
    }


def save_timeline(timeline, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def run_ffmpeg(cmd):
    heartbeat(stage="ffmpeg", step="FFmpeg 正在渲染")
    durations = []
    for index, value in enumerate(cmd[:-1]):
        if value in {"-t", "-to"}:
            try:
                durations.append(float(cmd[index + 1]))
            except (TypeError, ValueError):
                pass
    timeout = min(3600, max(120, (max(durations) if durations else 60) * 12 + 90))
    try:
        run_managed_command(cmd, timeout=timeout, error_type=TimelineError,
                            failure_message="FFmpeg 渲染失败")
    except TimelineError:
        raise
    except Exception as exc:
        raise TimelineError(str(exc)) from exc


def ffmpeg_filter_path(path):
    p = str(Path(path).resolve()).replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p


def find_pair(items, item_type, video_item):
    start = video_item["start_time"]
    end = video_item["end_time"]
    candidates = [
        i
        for i in items
        if i["type"] == item_type
        and abs(float(i["start_time"]) - start) < 0.01
        and abs(float(i["end_time"]) - end) < 0.05
        and i.get("shot_id") == video_item.get("shot_id")
    ]
    return candidates[0] if candidates else None


def render_timeline(ffmpeg, timeline, output_path, work_dir, video_encoder="libx264"):
    """
    第一版 timeline 渲染策略：
    1. 按 video item 逐段生成标准化分镜 MP4
    2. 同时间段 audio item 替换原声音
    3. 同时间段 subtitle item 使用 ASS 烧录
    4. concat 分镜输出最终 MP4

    这仍然是 FFmpeg 方案，但业务层已经从“分镜硬编码”升级为“统一时间线 items”。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_timeline(timeline, work_dir / "timeline.json")

    items = timeline.get("items") or []
    video_items = sorted([i for i in items if i["type"] == "video"], key=lambda x: (x["start_time"], x["layer"]))
    if not video_items:
        raise TimelineError("timeline 没有 video item")

    segments = []
    for idx, video in enumerate(video_items, 1):
        audio = find_pair(items, "audio", video)
        subtitle = find_pair(items, "subtitle", video)
        bgm = find_bgm_for_range(items, video["start_time"], video["end_time"])
        title = find_text_for_range(items, video["start_time"], video["end_time"])
        if not audio:
            raise TimelineError(f"{video.get('shot_name') or idx} 缺少 audio item")
        segment_path = work_dir / f"timeline_shot_{idx:03d}.mp4"
        render_segment(ffmpeg, timeline, video, audio, subtitle, bgm, title, segment_path, video_encoder)
        segments.append(segment_path)

    transitions = sorted([i for i in items if i["type"] == "transition"], key=lambda x: x["start_time"])
    merged_path = output_path if not any(i["type"] == "sfx" for i in items) else work_dir / "timeline_without_sfx.mp4"
    if transitions and any(i.get("enabled") for i in transitions):
        concat_segments_with_transitions(ffmpeg, segments, transitions, merged_path, work_dir, video_encoder)
    else:
        concat_segments(ffmpeg, segments, merged_path, work_dir)
    sfx_items = [i for i in items if i["type"] == "sfx" and i.get("source")]
    if sfx_items:
        mix_sfx(ffmpeg, merged_path, sfx_items, output_path)
    return str(output_path)


def find_bgm_for_range(items, start, end):
    start = float(start or 0)
    end = float(end or 0)
    candidates = [
        i
        for i in items
        if i["type"] == "bgm"
        and i.get("enabled", True)
        and i.get("source")
        and float(i["start_time"]) <= start + 0.01
        and float(i["end_time"]) >= end - 0.01
    ]
    return candidates[0] if candidates else None


def find_text_for_range(items, start, end):
    candidates = [
        i for i in items
        if i["type"] == "text"
        and i.get("enabled", True)
        and (i.get("text") or i.get("sub_text"))
        and float(i["start_time"]) < float(end) - 0.001
        and float(i["end_time"]) > float(start) + 0.001
    ]
    return candidates[0] if candidates else None


def ass_color(value, default="#FFFFFF"):
    value = str(value or default).lstrip("#")
    if len(value) != 6:
        value = default.lstrip("#")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H00{b}{g}{r}"


def ass_color_opacity(value, opacity=100, default="#FFFFFF"):
    value = str(value or default).lstrip("#")
    if len(value) != 6:
        value = default.lstrip("#")
    r, g, b = value[0:2], value[2:4], value[4:6]
    opacity = max(0.0, min(100.0, float(opacity or 0)))
    alpha = round((100.0 - opacity) / 100.0 * 255)
    return f"&H{alpha:02X}{b}{g}{r}"


def ass_time(seconds):
    value = max(0.0, float(seconds or 0))
    hours = int(value // 3600)
    minutes = int(value % 3600 // 60)
    secs = value % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def ass_escape_text(value):
    return str(value or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def title_horizontal_scale(text, font_size, width):
    # Keep each title on one line.  Long CJK/Latin text is compressed
    # horizontally in the same way as the UI preview instead of wrapping.
    units = sum(1.0 if ord(ch) > 255 else 0.58 for ch in str(text or ""))
    estimated = max(1.0, units * float(font_size))
    return max(25, min(100, round((float(width) * 0.90) / estimated * 100)))


def render_title_ass(title, video, duration, width, height, output_path):
    from style_presets import get_title_render_overrides
    overrides = get_title_render_overrides(title.get("title_style"), width, height, title)
    if overrides:
        title = {**title, **overrides}
    font = str(title.get("font") or "Microsoft YaHei")
    size = max(1 if overrides else 12, int(float(title.get("font_size") or 118)))
    bold = title.get("bold", -1) if overrides else -1
    primary = ass_color(title.get("color"), "#FFE67A")
    outline_color = ass_color(title.get("outline_color"), "#000000")
    shadow_color = ass_color(title.get("shadow_color"), "#F15A24")
    outline = max(0.0, float(title.get("outline_width") or 1))
    shadow_x = float(title.get("shadow_x") or 0)
    shadow_y = float(title.get("shadow_y") or 0)
    auto_fit = bool(title.get("auto_fit", False))
    background_enabled = bool(title.get("background_enabled", False))
    background_color = ass_color_opacity(
        title.get("background_color"), title.get("background_opacity", 72), "#16B8B8"
    )
    background_padding_y = max(0.0, float(title.get("background_padding_y") or 22))
    alignment = 5 if bool(title.get("center", True)) else 4
    global_start = float(video.get("start_time") or 0)
    local_start = max(0.0, float(title.get("start_time") or 0) - global_start)
    local_end = min(float(duration), float(title.get("end_time") or duration) - global_start)
    if local_end <= local_start:
        return ""

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{size},{primary},{primary},{outline_color},&H00000000,{bold},0,0,0,100,100,0,0,1,{outline},0,{alignment},0,0,0,1
Style: Shadow,{font},{size},{shadow_color},{shadow_color},{shadow_color},&H00000000,{bold},0,0,0,100,100,0,0,1,0,0,{alignment},0,0,0,1
Style: Box,Arial,10,{background_color},{background_color},{background_color},{background_color},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    active_y_positions = []
    for key, y_key in (("text", "main_y"), ("sub_text", "sub_y")):
        if str(title.get(key) or "").strip():
            prefix = "main" if key == "text" else "sub"
            active_y_positions.append(
                round(height * float(title.get(y_key) or (13 if prefix == "main" else 21)) / 100)
            )
    if background_enabled and active_y_positions:
        top = max(0, round(min(active_y_positions) - size * 0.62 - background_padding_y))
        bottom = min(height, round(max(active_y_positions) + size * 0.62 + background_padding_y))
        events.append(
            f"Dialogue: 0,{ass_time(local_start)},{ass_time(local_end)},Box,,0,0,0,,"
            f"{{\\an7\\pos(0,0)\\p1}}m 0 {top} l {width} {top} {width} {bottom} 0 {bottom}"
        )
    for key, y_key in (("text", "main_y"), ("sub_text", "sub_y")):
        text = str(title.get(key) or "").strip()
        if not text:
            continue
        prefix = "main" if key == "text" else "sub"
        x = round(width * float(title.get(f"{prefix}_x") or 50) / 100)
        y = round(height * float(title.get(y_key) or (13 if prefix == "main" else 21)) / 100)
        # Font size maps directly to libass pixels. Auto-fit is optional and no
        # longer silently cancels out the user's size adjustment.
        scale_x = title_horizontal_scale(text, size, width) if auto_fit else 100
        escaped = ass_escape_text(text)
        start_s, end_s = ass_time(local_start), ass_time(local_end)
        if shadow_x or shadow_y:
            events.append(f"Dialogue: 1,{start_s},{end_s},Shadow,,0,0,0,,{{\\an{alignment}\\pos({x + shadow_x:.1f},{y + shadow_y:.1f})\\q2\\fscx{scale_x}}}{escaped}")
        events.append(f"Dialogue: 2,{start_s},{end_s},Title,,0,0,0,,{{\\an{alignment}\\pos({x},{y})\\q2\\fscx{scale_x}}}{escaped}")

    if not events:
        return ""
    path = Path(output_path)
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    return str(path)


def render_segment(ffmpeg, timeline, video, audio, subtitle, bgm, title, output_path, video_encoder="libx264"):
    width = int(timeline.get("width") or SEGMENT_W)
    height = int(timeline.get("height") or SEGMENT_H)
    fps = int(timeline.get("fps") or FPS)
    duration = round(float(video["end_time"]) - float(video["start_time"]), 3)
    trim_start = float(video.get("trim_start") or 0)

    video_filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
    ]
    color_filter = build_color_filter(
        timeline.get("color_filter_enabled", True),
        timeline.get("color_filter_strength", "standard"),
    )
    if color_filter:
        video_filters.append(color_filter)
    # Continue reading the source when it has enough frames. If it ends early,
    # clone the final frame; never speed-change footage to fill the hold.
    video_filters.extend([
        f"tpad=stop_mode=clone:stop_duration={duration:.3f}",
        f"trim=duration={duration:.3f}",
        "setpts=PTS-STARTPTS",
        "setsar=1",
        f"fps={fps}",
        "format=yuv420p",
    ])
    vf = ",".join(video_filters)
    if subtitle and subtitle.get("source"):
        from style_presets import restyle_subtitle_ass
        subtitle_source = restyle_subtitle_ass(
            subtitle["source"], Path(output_path).with_suffix(".subtitle.ass"),
            timeline.get("subtitle_style"), width, height,
        )
        vf += f",ass='{ffmpeg_filter_path(subtitle_source)}'"
    if title:
        title_ass = render_title_ass(
            title, video, duration, width, height, Path(output_path).with_suffix(".title.ass")
        )
        if title_ass:
            title_filter = f"ass='{ffmpeg_filter_path(title_ass)}'"
            font_dir = str(title.get("font_dir") or "").strip()
            if font_dir and Path(font_dir).is_dir():
                title_filter += f":fontsdir='{ffmpeg_filter_path(font_dir)}'"
            vf += "," + title_filter

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{trim_start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        video["source"],
        "-i",
        audio["source"],
    ]
    if bgm:
        cmd += [
            "-stream_loop",
            "-1",
            "-ss",
            f"{float(video['start_time']):.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            bgm["source"],
        ]
    cmd += [
        "-map",
        "0:v:0",
        "-vf",
        vf,
    ]
    voice_volume = float(audio.get("volume", 1.0))
    voice_delay_ms = max(0, round(float(audio.get("voice_delay") or 0) * 1000))
    audio_filters = [
        f"[1:a]asetpts=PTS-STARTPTS,adelay={voice_delay_ms}|{voice_delay_ms},"
        f"volume={voice_volume},apad,atrim=duration={duration:.3f},"
        "aresample=async=1:first_pts=0[voice]"
    ]
    if bgm:
        bgm_volume = float(bgm.get("volume", 0.15))
        bgm_chain = f"[2:a]asetpts=PTS-STARTPTS,volume={bgm_volume},apad,atrim=duration={duration:.3f}"
        if video.get("is_last") and duration > 0.5:
            bgm_chain += f",afade=t=out:st={duration - 0.5:.3f}:d=0.500"
        audio_filters.append(bgm_chain + "[bgm]")
        audio_filters.append(
            f"[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
            f"apad,atrim=duration={duration:.3f},aresample=async=1:first_pts=0[aout]"
        )
    else:
        audio_filters.append(
            f"[voice]apad,atrim=duration={duration:.3f},aresample=async=1:first_pts=0[aout]"
        )
    cmd += ["-filter_complex", ";".join(audio_filters), "-map", "[aout]"]
    cmd += video_codec_args(video_encoder) + [
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    run_ffmpeg(cmd)


def concat_segments(ffmpeg, segments, output_path, work_dir):
    list_file = Path(work_dir) / "timeline_concat_list.txt"
    lines = []
    for seg in segments:
        safe = str(Path(seg).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    run_ffmpeg(cmd)


TRANSITION_FILTERS = {
    "fade": "fade", "slide_left": "slideleft", "slide_right": "slideright",
    "zoom": "zoomin", "flash": "fadewhite", "blur": "smoothleft",
}


def concat_segments_with_transitions(ffmpeg, segments, transitions, output_path, work_dir, video_encoder="libx264"):
    if len(segments) < 2:
        return concat_segments(ffmpeg, segments, output_path, work_dir)
    cmd = [ffmpeg, "-y"]
    durations = []
    for segment in segments:
        cmd += ["-i", str(segment)]
        durations.append(probe_duration(ffmpeg, segment))
    # Every input must start at zero and use the same video time base before it
    # enters concat/xfade.  More importantly, a disabled transition is a hard
    # cut, not a 1 ms xfade.  FFmpeg can emit only the longest input's video
    # frames when xfade is asked to operate below one frame (the old 0.001 s
    # workaround), while the concatenated audio remains full length.
    chains = []
    for index in range(len(segments)):
        chains.append(f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[vin{index}]")
        chains.append(
            f"[{index}:a]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[ain{index}]"
        )
    video_label, audio_label = "vin0", "ain0"
    elapsed = durations[0]
    for index in range(1, len(segments)):
        transition = transitions[index - 1] if index - 1 < len(transitions) else {"effect": "fade", "duration": 0.001, "enabled": False}
        vout, aout = f"v{index}", f"a{index}"
        if transition.get("enabled"):
            td = max(
                1.0 / FPS,
                min(
                    float(transition.get("duration") or 0.35),
                    durations[index - 1] * 0.45,
                    durations[index] * 0.45,
                ),
            )
            offset = max(0.0, elapsed - td)
            effect = TRANSITION_FILTERS.get(str(transition.get("effect")), "fade")
            chains.append(
                f"[{video_label}][vin{index}]xfade=transition={effect}:"
                f"duration={td:.3f}:offset={offset:.3f}[{vout}]"
            )
            chains.append(
                f"[{audio_label}][ain{index}]acrossfade=d={td:.3f}:c1=tri:c2=tri[{aout}]"
            )
            elapsed += durations[index] - td
        else:
            chains.append(
                f"[{video_label}][vin{index}]concat=n=2:v=1:a=0[{vout}]"
            )
            chains.append(
                f"[{audio_label}][ain{index}]concat=n=2:v=0:a=1[{aout}]"
            )
            elapsed += durations[index]
        video_label, audio_label = vout, aout
    final_duration = max(0.001, elapsed)
    chains.append(f"[{video_label}]trim=duration={final_duration:.3f},setpts=PTS-STARTPTS[vfinal]")
    chains.append(
        f"[{audio_label}]apad,atrim=duration={final_duration:.3f},"
        "aresample=async=1:first_pts=0[afinal]"
    )
    cmd += ["-filter_complex", ";".join(chains), "-map", "[vfinal]", "-map", "[afinal]"]
    cmd += video_codec_args(video_encoder) + ["-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2", "-t", f"{final_duration:.3f}",
            "-movflags", "+faststart", str(output_path)]
    run_ffmpeg(cmd)


def probe_duration(ffmpeg, path):
    try:
        ffprobe = get_ffprobe_path()
    except MediaRuntimeError as exc:
        raise TimelineError(str(exc)) from exc
    heartbeat(stage="ffprobe", step="读取分镜时长")
    try:
        proc = run_managed_command(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(path)],
            timeout=30, error_type=TimelineError, failure_message="无法读取转场分镜时长",
        )
    except TimelineError:
        raise
    except Exception as exc:
        raise TimelineError(str(exc)) from exc
    return float(proc.stdout.strip())


def mix_sfx(ffmpeg, video_path, sfx_items, output_path):
    cmd = [ffmpeg, "-y", "-i", str(video_path)]
    filters = []
    target_duration = probe_duration(ffmpeg, video_path)
    filters.append(
        f"[0:a]asetpts=PTS-STARTPTS,apad,atrim=duration={target_duration:.3f},"
        "aresample=async=1:first_pts=0[base]"
    )
    labels = ["[base]"]
    for index, item in enumerate(sfx_items, 1):
        cmd += ["-i", item["source"]]
        delay = max(0, round(float(item.get("start_time") or 0) * 1000))
        duration = max(0.01, float(item.get("end_time") or 0) - float(item.get("start_time") or 0))
        label = f"sfx{index}"
        volume = min(0.35, max(0.0, float(item.get("volume") or 0.25)))
        filters.append(f"[{index}:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume={volume:.3f},adelay={delay}|{delay}[{label}]")
        labels.append(f"[{label}]")
    # amix normalizes by the number of inputs by default, which halves the
    # TTS level as soon as one SFX is added.  Keep the voice as the reference
    # track and add SFX at its configured low level without automatic gain loss.
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        + f"apad,atrim=duration={target_duration:.3f},aresample=async=1:first_pts=0[aout]"
    )
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-t", f"{target_duration:.3f}", "-movflags", "+faststart", str(output_path)]
    try:
        from generation_performance import measure
        with measure("audio_mix"):
            run_ffmpeg(cmd)
    except ImportError:
        run_ffmpeg(cmd)
from subprocess_utils import background_process_kwargs
from task_runtime import heartbeat, run_managed_command
