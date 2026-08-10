import subprocess
import random
import json
from pathlib import Path

from runtime_paths import MediaRuntimeError, get_ffmpeg_path, get_ffprobe_path

from db import APP_DIR, connect, now
from bgm_service import scan_bgm_folder
from timeline_engine import TimelineError, build_color_filter, build_timeline_from_shots, render_timeline
from tts_service import audio_duration
from task_runtime import heartbeat, run_managed_command
from generation_success_service import on_generation_succeeded

SEGMENT_W = 1080
SEGMENT_H = 1920


class VideoGenerationError(RuntimeError):
    pass


def find_ffmpeg(configured: str = "") -> str:
    try:
        return get_ffmpeg_path()
    except MediaRuntimeError as exc:
        raise VideoGenerationError(str(exc)) from exc


def update_task(task_id, **fields):
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ",".join([f"{k}=?" for k in fields])
    vals = list(fields.values()) + [task_id]
    with connect() as conn:
        conn.execute(f"UPDATE generation_tasks SET {sets} WHERE id=?", vals)


def load_template_shots(template_id, task_id=None):
    with connect() as conn:
        relation_join = (
            "LEFT JOIN generation_shot_materials gsm ON gsm.id=("
            "SELECT id FROM generation_shot_materials x "
            "WHERE x.shot_id=s.id AND x.task_id=? ORDER BY x.id DESC LIMIT 1)"
            if task_id is not None
            else
            "LEFT JOIN generation_shot_materials gsm ON gsm.id=("
            "SELECT id FROM generation_shot_materials x "
            "WHERE x.shot_id=s.id ORDER BY x.id DESC LIMIT 1)"
        )
        params = (task_id, template_id) if task_id is not None else (template_id,)
        rows = conn.execute(
            f"""
            SELECT
                s.id shot_id,
                s.shot_name,
                s.original_text,
                s.ai_text,
                s.sort_order,
                s.voice_file_path,
                s.voice_duration,
                s.enable_subtitle,
                s.subtitle_file_path,
                gsm.material_start_time,
                gsm.material_end_time,
                m.id material_id,
                m.file_path material_path,
                m.file_name material_name,
                m.duration material_duration,
                m.width material_width,
                m.height material_height
            FROM template_shots s
            {relation_join}
            LEFT JOIN materials m ON m.id=gsm.material_id
            WHERE s.template_id=?
            ORDER BY s.sort_order,s.id
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def load_default_bgm():
    with connect() as conn:
        enabled_setting = conn.execute("SELECT value FROM app_settings WHERE key='bgm_enabled'").fetchone()
        enabled = str(enabled_setting["value"] if enabled_setting else "0").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return None
        setting = conn.execute("SELECT value FROM app_settings WHERE key='default_bgm_id'").fetchone()
        bgm_id = str(setting["value"]).strip() if setting else ""
        row = None
        if bgm_id:
            selected = conn.execute("SELECT * FROM bgm_library WHERE id=?", (bgm_id,)).fetchone()
            row = selected if selected and Path(selected["file_path"]).is_file() else None
        if not row:
            candidates = conn.execute("SELECT * FROM bgm_library ORDER BY id").fetchall()
            candidates = [item for item in candidates if Path(item["file_path"]).is_file()]
            row = random.choice(candidates) if candidates else None
    if not row:
        with connect() as conn:
            bgm_dir = conn.execute("SELECT value FROM app_settings WHERE key='bgm_dir'").fetchone()
        folder = str(bgm_dir["value"] if bgm_dir else "").strip()
        if folder and Path(folder).is_dir():
            scan_bgm_folder(folder)
            with connect() as conn:
                candidates = conn.execute("SELECT * FROM bgm_library ORDER BY id").fetchall()
                candidates = [item for item in candidates if Path(item["file_path"]).is_file()]
                row = random.choice(candidates) if candidates else None
    if not row or not Path(row["file_path"]).is_file():
        raise VideoGenerationError("已开启背景音乐，但 BGM 目录中没有可用的 mp3、wav 或 m4a 文件")
    with connect() as conn:
        volume_setting = conn.execute("SELECT value FROM app_settings WHERE key='bgm_volume'").fetchone()
        volume = float(volume_setting["value"] or 0.15) if volume_setting else float(row["volume_default"] or 0.15)
        return {"source": row["file_path"], "volume": volume, "enabled": True, "name": row["name"]}


def load_title_config():
    with connect() as conn:
        values = {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key,value FROM app_settings WHERE key LIKE 'title_%'").fetchall()
        }
    return title_config_from_settings(values)


def load_color_filter_config():
    with connect() as conn:
        values = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key,value FROM app_settings WHERE key IN ('natural_color_filter_enabled','natural_color_filter_strength')"
            ).fetchall()
        }
    enabled = str(values.get("natural_color_filter_enabled", "1")).strip().lower() in {"1", "true", "yes", "on"}
    strength = str(values.get("natural_color_filter_strength") or "standard")
    return enabled, strength


def load_timeline_timing_config():
    with connect() as conn:
        values = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key,value FROM app_settings WHERE key IN ('end_hold_duration','inter_shot_audio_safety')"
            ).fetchall()
        }
    end_hold = max(0.0, float(values.get("end_hold_duration") or 0.8))
    inter_shot = max(0.1, min(0.2, float(values.get("inter_shot_audio_safety") or 0.15)))
    return end_hold, inter_shot


def refresh_real_voice_durations(shots, ffprobe_path=""):
    refreshed = []
    with connect() as conn:
        for source in shots:
            shot = dict(source)
            voice_path = str(shot.get("voice_file_path") or "")
            actual = audio_duration(voice_path, ffprobe_path) if voice_path else 0.0
            if actual <= 0:
                raise VideoGenerationError(f"镜头 {shot.get('shot_name') or shot.get('shot_id')} 无法读取真实语音时长")
            shot["voice_duration"] = actual
            shot["estimated_duration"] = actual
            conn.execute(
                "UPDATE template_shots SET voice_duration=?,estimated_duration=?,updated_at=? WHERE id=?",
                (actual, actual, now(), shot["shot_id"]),
            )
            conn.execute(
                "UPDATE generation_shot_materials SET voice_duration=?,updated_at=? WHERE shot_id=?",
                (actual, now(), shot["shot_id"]),
            )
            refreshed.append(shot)
    return refreshed


def probe_av_durations(ffmpeg, path):
    try:
        ffprobe = get_ffprobe_path()
    except MediaRuntimeError as exc:
        raise VideoGenerationError(str(exc)) from exc
    heartbeat(stage="ffprobe", step="验收成片音视频时长")
    try:
        proc = run_managed_command(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
            timeout=30, error_type=VideoGenerationError, failure_message="无法读取最终成片的音视频时长",
        )
    except VideoGenerationError:
        raise
    except Exception as exc:
        raise VideoGenerationError(str(exc)) from exc
    payload = json.loads(proc.stdout or "{}")
    format_duration = float((payload.get("format") or {}).get("duration") or 0)
    durations = {"video": 0.0, "audio": 0.0}
    for stream in payload.get("streams") or []:
        kind = stream.get("codec_type")
        if kind in durations:
            try:
                durations[kind] = float(stream.get("duration") or format_duration)
            except (TypeError, ValueError):
                durations[kind] = format_duration
    return durations["video"] or format_duration, durations["audio"] or format_duration


def validate_final_timing(ffmpeg, output_path, timeline):
    video_duration, audio_track_duration = probe_av_durations(ffmpeg, output_path)
    last_voice_end = float(timeline.get("last_voice_end") or 0)
    end_hold = float(timeline.get("end_hold_duration") or 0)
    required_hold = max(0.0, end_hold - 0.1)
    final_duration = min(video_duration, audio_track_duration)
    if final_duration + 0.02 < last_voice_end + required_hold:
        raise VideoGenerationError(
            f"成片时长验收失败：成片 {final_duration:.3f} 秒，最后语音结束 {last_voice_end:.3f} 秒，"
            f"结尾停留不足 {required_hold:.3f} 秒"
        )
    av_diff = abs(video_duration - audio_track_duration)
    if av_diff > 0.05:
        raise VideoGenerationError(
            f"成片音视频不同步：视频 {video_duration:.3f} 秒，音频 {audio_track_duration:.3f} 秒，差值 {av_diff:.3f} 秒"
        )
    return {
        "video_duration": round(video_duration, 6),
        "audio_duration": round(audio_track_duration, 6),
        "last_voice_end": round(last_voice_end, 6),
        "end_hold": round(final_duration - last_voice_end, 6),
        "av_diff": round(av_diff, 6),
    }


def title_config_from_settings(values):
    main = str(values.get("title_main") or "").strip()
    sub = str(values.get("title_sub") or "").strip()
    if not main and not sub:
        return None
    return {
        "main": main,
        "sub": sub,
        "font": values.get("title_font") or "Microsoft YaHei",
        "font_dir": values.get("title_font_dir") or "",
        "font_size": float(values.get("title_font_size") or 118),
        "color": values.get("title_color") or "#FFE67A",
        "auto_fit": str(values.get("title_auto_fit") or "0") == "1",
        "background_enabled": str(values.get("title_background_enabled") or "0") == "1",
        "background_color": values.get("title_background_color") or "#16B8B8",
        "background_opacity": float(values.get("title_background_opacity") or 72),
        "background_padding_y": float(values.get("title_background_padding_y") or 22),
        "outline_color": values.get("title_outline_color") or "#000000",
        "outline_width": float(values.get("title_outline_width") or 1),
        "shadow_color": values.get("title_shadow_color") or "#F15A24",
        "shadow_x": float(values.get("title_shadow_x") or 2),
        "shadow_y": float(values.get("title_shadow_y") or 2),
        "center": str(values.get("title_center") or "1") == "1",
        "main_x": float(values.get("title_main_x") or 50),
        "main_y": float(values.get("title_main_y") or 13),
        "sub_x": float(values.get("title_sub_x") or 50),
        "sub_y": float(values.get("title_sub_y") or 21),
    }


def validate_shots(shots):
    if not shots:
        raise VideoGenerationError("模板没有镜头，无法生成视频。")
    errors = []
    for s in shots:
        label = f"镜头【{s.get('shot_name') or s['shot_id']}】"
        if not s.get("material_path"):
            errors.append(f"{label} 未匹配素材")
        elif not Path(s["material_path"]).exists():
            errors.append(f"{label} 素材文件不存在：{s['material_path']}")
        if not s.get("voice_file_path"):
            errors.append(f"{label} 未生成/导入语音")
        elif not Path(s["voice_file_path"]).exists():
            errors.append(f"{label} 语音文件不存在：{s['voice_file_path']}")
        if float(s.get("voice_duration") or 0) <= 0:
            errors.append(f"{label} voice_duration 无效")
        if int(s.get("enable_subtitle") or 0) and (not s.get("subtitle_file_path") or not Path(s["subtitle_file_path"]).exists()):
            errors.append(f"{label} 已开启字幕但字幕文件不存在")
        start = float(s.get("material_start_time") or 0)
        end = float(s.get("material_end_time") or 0)
        if end <= start:
            errors.append(f"{label} 素材裁剪区间无效")
    if errors:
        raise VideoGenerationError("\n".join(errors))


def run_ffmpeg(cmd):
    heartbeat(stage="ffmpeg", step="FFmpeg 正在处理视频")
    durations = []
    for index, value in enumerate(cmd[:-1]):
        if value in {"-t", "-to"}:
            try:
                durations.append(float(cmd[index + 1]))
            except (TypeError, ValueError):
                pass
    timeout = min(3600, max(120, (max(durations) if durations else 60) * 12 + 90))
    try:
        run_managed_command(cmd, timeout=timeout, error_type=VideoGenerationError,
                            failure_message="FFmpeg 执行失败")
    except VideoGenerationError:
        raise
    except Exception as exc:
        raise VideoGenerationError(str(exc)) from exc


def ffmpeg_filter_path(path: str) -> str:
    # FFmpeg subtitles/ass filter on Windows needs escaped drive colon and forward slashes.
    p = str(Path(path).resolve()).replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p


def make_segment(ffmpeg, shot, output_path):
    start = float(shot["material_start_time"] or 0)
    voice_duration = float(shot["voice_duration"] or 0)
    target_duration = voice_duration + 0.15
    # 重点：这里按 voice_duration 截取素材片段，不做 setpts/atempo 变速，不把原视频压缩到语音长度。
    filter_enabled, filter_strength = load_color_filter_config()
    video_filters = [
        f"scale={SEGMENT_W}:{SEGMENT_H}:force_original_aspect_ratio=increase",
        f"crop={SEGMENT_W}:{SEGMENT_H}",
    ]
    color_filter = build_color_filter(filter_enabled, filter_strength)
    if color_filter:
        video_filters.append(color_filter)
    video_filters.extend([
        f"tpad=stop_mode=clone:stop_duration={target_duration:.3f}",
        f"trim=duration={target_duration:.3f}", "setpts=PTS-STARTPTS",
        "setsar=1", "fps=30", "format=yuv420p",
    ])
    vf = ",".join(video_filters)
    if int(shot.get("enable_subtitle") or 0) and shot.get("subtitle_file_path"):
        vf += f",ass='{ffmpeg_filter_path(shot['subtitle_file_path'])}'"
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{target_duration:.3f}",
        "-i",
        shot["material_path"],
        "-i",
        shot["voice_file_path"],
        "-map",
        "0:v:0",
        "-filter_complex",
        f"[1:a]asetpts=PTS-STARTPTS,apad,atrim=duration={target_duration:.3f},aresample=async=1:first_pts=0[aout]",
        "-map",
        "[aout]",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-t",
        f"{target_duration:.3f}",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    run_ffmpeg(cmd)


def concat_segments(ffmpeg, segments, output_path, work_dir):
    list_file = work_dir / "concat_list.txt"
    lines = []
    for seg in segments:
        safe = str(seg.resolve()).replace("\\", "/").replace("'", "'\\''")
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


def generate_video(task_id, template_id, ffmpeg_path="", output_dir="", work_root=""):
    ffmpeg = find_ffmpeg()
    # A task owns an immutable material selection.  Never read the template's
    # old editor-preview match here, otherwise every task renders the same clips.
    shots = load_template_shots(template_id, task_id)
    try:
        ffprobe_path = get_ffprobe_path()
    except MediaRuntimeError as exc:
        raise VideoGenerationError(str(exc)) from exc
    shots = refresh_real_voice_durations(shots, ffprobe_path)
    validate_shots(shots)

    out_root = Path(output_dir) if output_dir else APP_DIR / "outputs"
    out_root.mkdir(parents=True, exist_ok=True)
    cache_root = Path(work_root) if work_root else out_root
    cache_root.mkdir(parents=True, exist_ok=True)
    work_dir = cache_root / f"task_{task_id}_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_root / f"task_{task_id}_final.mp4"

    total = len(shots)
    update_task(task_id, status="preparing", progress=1, total_shots=total, current_shot="构建时间线")
    heartbeat(task_id, status="rendering", stage="timeline", step="构建时间线", progress=42)
    color_filter_enabled, color_filter_strength = load_color_filter_config()
    end_hold_duration, inter_shot_safety = load_timeline_timing_config()
    timeline = build_timeline_from_shots(
        shots,
        bgm=load_default_bgm(),
        title_text=load_title_config(),
        color_filter_enabled=color_filter_enabled,
        color_filter_strength=color_filter_strength,
        end_hold_duration=end_hold_duration,
        inter_shot_safety=inter_shot_safety,
    )
    with connect() as conn:
        volume_row = conn.execute("SELECT value FROM app_settings WHERE key='voice_volume'").fetchone()
    voice_volume = float(volume_row["value"] or 1.0) if volume_row else 1.0
    for item in timeline.get("items", []):
        if item.get("type") == "audio":
            item["volume"] = voice_volume
    update_task(task_id, status="rendering", current_shot="渲染时间线分镜", progress=45)
    heartbeat(task_id, status="rendering", stage="rendering", step="渲染时间线分镜", progress=45)
    try:
        render_timeline(ffmpeg, timeline, final_path, work_dir)
        timing = validate_final_timing(ffmpeg, final_path, timeline)
        (work_dir / "timing_acceptance.json").write_text(
            json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (TimelineError, VideoGenerationError) as exc:
        final_path.unlink(missing_ok=True)
        update_task(task_id, status="failed", current_shot="时长验收失败", error_message=str(exc))
        raise VideoGenerationError(str(exc)) from exc
    heartbeat(task_id, status="verifying", stage="verifying", step="合并并验收成片", progress=95)
    update_task(task_id, status="verifying", current_shot="合并时间线", progress=95)
    try:
        on_generation_succeeded(
            task_id,
            final_path,
            verified_duration=float(timing.get("video_duration") or 0.0),
            ffprobe_path=ffprobe_path,
        )
    except Exception as exc:
        update_task(
            task_id,
            status="failed",
            current_stage="publish_registration",
            current_step="登记发布记录",
            current_shot="登记发布记录",
            error_code="PUBLISH_REGISTRATION_FAILED",
            error_message=str(exc),
            failed_stage="publish_registration",
            finished_at=now(),
        )
        raise VideoGenerationError(str(exc)) from exc
    heartbeat(
        task_id, status="succeeded", stage="succeeded", step="完成", progress=100,
        log=f"成片生成完成：{final_path}",
    )
    return str(final_path)
from subprocess_utils import background_process_kwargs
