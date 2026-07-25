import hashlib
import json
import math
import re
from pathlib import Path

from db import DATA_DIR, connect
from script_segmentation import smart_segment_text
from language_service import contains_cjk
from tts_service import audio_duration

SUBTITLE_DIR = DATA_DIR / "subtitles"
SUBTITLE_LAYOUT_VERSION = "semantic-space-cuts-adaptive-wrap-v3"

ASS_STYLE = {
    "font": "Microsoft YaHei",
    "size": 72,
    "margin_v": 180,
    "outline": 4,
    "bold": 1,
}


def ass_color(value: str, fallback: str = "#FFFFFF") -> str:
    raw = str(value or fallback).strip().lstrip("#")
    if len(raw) != 6:
        raw = fallback.lstrip("#")
    try:
        red, green, blue = raw[0:2], raw[2:4], raw[4:6]
        int(raw, 16)
    except ValueError:
        red, green, blue = "FF", "FF", "FF"
    return f"&H00{blue}{green}{red}"


def current_ass_style() -> dict:
    style = dict(ASS_STYLE)
    try:
        with connect() as conn:
            cfg = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM app_settings")}
        style.update({
            "font": cfg.get("subtitle_font") or style["font"],
            "size": max(20, min(180, int(float(cfg.get("subtitle_font_size") or style["size"])))),
            "margin_v": max(0, min(1200, int(float(cfg.get("subtitle_margin_bottom") or style["margin_v"])))),
            "outline": max(0, min(12, float(cfg.get("subtitle_outline_width") or style["outline"]))),
            "color": ass_color(cfg.get("subtitle_color"), "#FFFFFF"),
            "outline_color": ass_color(cfg.get("subtitle_outline_color"), "#000000"),
            "effect": cfg.get("subtitle_effect") if cfg.get("subtitle_effect") in {
                "none", "fade", "pop", "zoom", "slide_up", "slide_left"
            } else "none",
            "effect_duration": max(60, min(500, int(float(cfg.get("subtitle_effect_duration") or 160)))),
            "max_chars_per_line": max(6, min(30, int(float(cfg.get("subtitle_max_chars_per_line") or 14)))),
        })
    except Exception:
        style.update({
            "color": "&H00FFFFFF",
            "outline_color": "&H00000000",
            "effect": "none",
            "effect_duration": 160,
            "max_chars_per_line": 14,
        })
    return style


class SubtitleError(RuntimeError):
    pass


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - math.floor(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass(text: str) -> str:
    text = (text or "").replace("{", "").replace("}", "")
    text = text.replace("\r", "").replace("\n", "\\N")
    return text


def display_units(text: str) -> float:
    return sum(1.0 if ord(ch) > 255 else 0.55 for ch in str(text or ""))


BREAK_KEYWORDS = ("那你", "那么", "但是", "所以", "因为", "同时", "我们", "现在", "如果", "只要", "而且", "以及", "就是", "直接")
BREAK_PUNCTUATION = "，。！？；：、,.!?;:"


def best_break_index(text: str, target_units: float, max_units: float) -> int:
    """Choose a balanced boundary, preferring clauses, spaces and conjunctions."""
    text = str(text or "")
    if len(text) < 2:
        return len(text)
    prefix = [0.0]
    for ch in text:
        prefix.append(prefix[-1] + (1.0 if ord(ch) > 255 else 0.55))
    candidates = []
    for index in range(1, len(text)):
        left, right = prefix[index], prefix[-1] - prefix[index]
        if left > max_units + 0.01:
            break
        hint = 0
        if text[index - 1] in BREAK_PUNCTUATION:
            hint = 5
        elif text[index - 1].isspace() or text[index].isspace():
            hint = 4
        elif any(text.startswith(word, index) for word in BREAK_KEYWORDS):
            hint = 3
        score = hint * 6 - abs(left - target_units) * 5 - abs(left - right) * 0.2
        candidates.append((score, index))
    return max(candidates)[1] if candidates else max(1, len(text) // 2)


def split_text(text: str, max_len: int = 12):
    # Product rule: whitespace in the shot script is an explicit subtitle cut
    # marker.  It is not rendered and it never creates a second visual line.
    normalized = smart_segment_text(text)
    if contains_cjk(normalized):
        return [part for part in normalized.split(" ") if part]

    # Latin-language spaces separate words, not subtitle events. Group complete
    # words into readable phrases instead of showing one word per subtitle.
    words = [part for part in normalized.split(" ") if part]
    if not words:
        return []
    target = max(24, int(max_len * 2.4))
    chunks, current = [], []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > target:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def greedy_break_index(text: str, max_line_units: float) -> int:
    """Fill a line first, then prefer a nearby clause boundary before overflow."""
    units = 0.0
    hard_index = len(text)
    for index, ch in enumerate(text, 1):
        next_units = units + (1.0 if ord(ch) > 255 else 0.55)
        if next_units > max_line_units:
            hard_index = max(1, index - 1)
            break
        units = next_units
    if hard_index >= len(text):
        return len(text)
    search_start = max(1, int(math.ceil(hard_index * 0.88)))
    for index in range(hard_index, search_start - 1, -1):
        if text[index - 1] in BREAK_PUNCTUATION:
            return index
        if any(text.startswith(word, index) for word in BREAK_KEYWORDS):
            return index
    return hard_index


def logical_wrap(text: str, max_line_units: float = 14) -> str:
    """Greedy wrap: keep the first line long and wrap only near the width limit."""
    remaining = re.sub(r"\s+", "", str(text or "")).strip()
    if not remaining or display_units(remaining) <= max_line_units:
        return remaining
    lines = []
    while remaining and display_units(remaining) > max_line_units:
        index = greedy_break_index(remaining, max_line_units)
        index = max(1, min(len(remaining) - 1, index))
        lines.append(remaining[:index].strip())
        remaining = remaining[index:].strip()
    if remaining:
        lines.append(remaining)
    return "\n".join(line for line in lines if line)


def subtitle_line_capacity(style: dict, play_width: int = 1080) -> float:
    """Use the user-controlled per-line limit; mixed Latin text uses half units."""
    return max(6.0, min(30.0, float(style.get("max_chars_per_line") or 14)))


def prepare_ass_segments(segments, max_line_units: float = 12):
    """Split long Whisper/TTS segments and preserve their real timestamp range."""
    prepared = []
    for segment in segments:
        blocks = split_text(segment.get("text") or "", 12)
        if not blocks:
            continue
        start = float(segment.get("start") or 0)
        end = max(start + 0.01, float(segment.get("end") or start))
        weights = [max(0.5, display_units(block)) for block in blocks]
        total = sum(weights)
        cursor = start
        for index, (block, weight) in enumerate(zip(blocks, weights)):
            block_end = end if index == len(blocks) - 1 else cursor + (end - start) * weight / total
            prepared.append({
                "start": cursor,
                "end": block_end,
                "text": logical_wrap(block, max_line_units),
            })
            cursor = block_end
    return prepared


def subtitle_signature(text: str) -> str:
    payload = {
        "version": SUBTITLE_LAYOUT_VERSION,
        "text": str(text or "").strip(),
        "style": current_ass_style(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def try_whisper(audio_path: str):
    """
    Whisper 接口预留：
    - 如果本机安装 openai-whisper，则使用 whisper.load_model("base")。
    - 如果本机安装 faster-whisper，则使用 WhisperModel("base")。
    - 未安装时返回 None，由 TTS 文案回退生成时间轴字幕，保证本地版本可运行。
    """
    try:
        import whisper  # type: ignore

        model = whisper.load_model("base")
        result = model.transcribe(audio_path, language="zh", fp16=False)
        segments = []
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if text:
                segments.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": text})
        return segments
    except Exception:
        pass

    try:
        from faster_whisper import WhisperModel  # type: ignore

        model = WhisperModel("base", device="cpu", compute_type="int8")
        out, _info = model.transcribe(audio_path, language="zh")
        segments = []
        for seg in out:
            text = (seg.text or "").strip()
            if text:
                segments.append({"start": float(seg.start), "end": float(seg.end), "text": text})
        return segments
    except Exception:
        return None


def fallback_segments(text: str, duration: float):
    chunks = split_text(text)
    duration = float(duration or 0)
    if duration <= 0:
        return []
    if not chunks:
        return []
    total_chars = sum(max(1, len(c)) for c in chunks)
    cur = 0.0
    segments = []
    for idx, chunk in enumerate(chunks):
        if idx == len(chunks) - 1:
            end = duration
        else:
            span = duration * (max(1, len(chunk)) / total_chars)
            end = min(duration, cur + max(0.8, span))
        segments.append({"start": cur, "end": max(cur + 0.2, end), "text": chunk})
        cur = end
    return segments


def write_ass(segments, output_path: str):
    style = current_ass_style()
    segments = prepare_ass_segments(segments, subtitle_line_capacity(style))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"""[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font']},{style['size']},{style['color']},&H00FFFFFF,{style['outline_color']},&H64000000,{style['bold']},0,0,0,100,100,0,0,1,{style['outline']},0,2,60,60,{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for seg in segments:
        start = ass_time(seg["start"])
        end = ass_time(seg["end"])
        text = escape_ass(seg["text"])
        override = ""
        effect = style.get("effect") or "none"
        segment_ms = max(1, round((float(seg["end"]) - float(seg["start"])) * 1000))
        effect_ms = min(int(style.get("effect_duration") or 160), max(60, segment_ms // 3))
        fade_out = min(100, max(40, segment_ms // 5))
        if effect == "fade":
            override = f"{{\\fad({effect_ms},{effect_ms})}}"
        elif effect == "pop":
            overshoot_ms = max(40, round(effect_ms * 0.62))
            settle_ms = max(overshoot_ms + 20, effect_ms)
            override = (
                f"{{\\fscx60\\fscy60"
                f"\\t(0,{overshoot_ms},\\fscx112\\fscy112)"
                f"\\t({overshoot_ms},{settle_ms},\\fscx100\\fscy100)"
                f"\\fad(40,{fade_out})}}"
            )
        elif effect == "zoom":
            override = (
                f"{{\\fscx78\\fscy78"
                f"\\t(0,{effect_ms},\\fscx100\\fscy100)"
                f"\\fad(40,{fade_out})}}"
            )
        elif effect == "slide_up":
            target_y = 1920 - int(style.get("margin_v") or 180)
            override = (
                f"{{\\an2\\move(540,{target_y + 70},540,{target_y},0,{effect_ms})"
                f"\\fad(50,{fade_out})}}"
            )
        elif effect == "slide_left":
            target_y = 1920 - int(style.get("margin_v") or 180)
            override = (
                f"{{\\an2\\move(-200,{target_y},540,{target_y},0,{effect_ms})"
                f"\\fad(50,{fade_out})}}"
            )
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{override}{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    return str(path)


def generate_ass_for_shot(shot_id: int, audio_path: str, fallback_text: str = "", duration: float = 0) -> tuple[str, str]:
    SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)
    real_duration = float(duration or 0) or audio_duration(audio_path)
    if real_duration <= 0:
        raise SubtitleError("无法检测语音时长，不能生成字幕")
    has_space_markers = len(split_text(fallback_text)) > 1
    if has_space_markers:
        segments = fallback_segments(fallback_text, real_duration)
        source = "script_space_markers"
    else:
        segments = try_whisper(audio_path)
        source = "whisper"
    if not segments:
        segments = fallback_segments(fallback_text, real_duration)
        source = "fallback_text"
    if not segments:
        raise SubtitleError("Whisper 未返回文本，且没有可回退的文案")
    out = SUBTITLE_DIR / f"shot_{shot_id}.ass"
    return write_ass(segments, str(out)), source


def generate_ass_from_segments(shot_id: int, segments) -> str:
    """Write subtitles from exact per-part TTS durations."""
    SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)
    out = SUBTITLE_DIR / f"shot_{shot_id}.ass"
    return write_ass(segments, str(out))
