import subprocess
import random
import json
import subprocess
import threading
from pathlib import Path

from runtime_paths import LOG_DIR, MediaRuntimeError, get_ffmpeg_path, get_ffprobe_path
from startup_diagnostics import startup_exception, startup_log

from db import APP_DIR, connect, now
from bgm_service import scan_bgm_folder
from timeline_engine import (
    TimelineError, build_color_filter, build_timeline_from_shots, render_timeline,
)
from tts_service import audio_duration
from task_runtime import heartbeat, run_managed_command
from generation_success_service import on_generation_succeeded
from subprocess_utils import background_process_kwargs

SEGMENT_W = 1080
SEGMENT_H = 1920

_GPU_DETECTION_LOCK = threading.RLock()
_GPU_DETECTION_RESULT: dict | None = None
_GPU_DETECTION_STARTED = False


def _pending_video_acceleration_status() -> dict:
    try:
        mode = video_encoder_mode()
    except Exception:
        mode = "auto"
    return {
        "mode": mode, "current_encoder": "libx264",
        "current_encoder_label": "检测中", "gpu_model": "检测中",
        "gpu_memory": "未知", "gpu_vendor": "未知", "gpu_available": False,
        "nvenc_status": "checking", "hardware_status": {},
        "fallback_reason": "后台检测中", "available_encoders": [],
        "detection_pending": True,
    }


def _compute_video_acceleration_status() -> dict:
    startup_log("gpu_detection_start")
    try:
        startup_log("ffmpeg_detection_start")
        ffmpeg = find_ffmpeg()
        startup_log("ffmpeg_detection_end", ffmpeg_path=ffmpeg)
        details = hardware_encoder_details(ffmpeg)
        selected = details["selected_encoder"]
        probes = {item["encoder"]: item for item in details["probes"]}
        labels = {
            "h264_nvenc": "NVIDIA NVENC (h264_nvenc)",
            "h264_qsv": "Intel QSV (h264_qsv)",
            "h264_amf": "AMD AMF (h264_amf)",
            "libx264": "CPU x264 (libx264)",
        }
        result = {
            "mode": details["mode"], "current_encoder": selected,
            "current_encoder_label": labels.get(selected, selected),
            "gpu_model": details["gpu_name"], "gpu_memory": details["gpu_memory"],
            "gpu_vendor": details["gpu_vendor"], "gpu_available": selected != "libx264",
            "nvenc_status": "available" if probes.get("h264_nvenc", {}).get("usable") else "unavailable",
            "hardware_status": probes, "fallback_reason": details["fallback_reason"],
            "available_encoders": details["encoders"], "detection_pending": False,
        }
        startup_log("ffmpeg_detection_end", encoders=details["encoders"], selected_encoder=selected)
        startup_log("gpu_detection_end", available=result["gpu_available"], gpu_model=result["gpu_model"], selected_encoder=selected)
        return result
    except Exception as exc:
        # Hardware probing is optional. Never let a driver/FFmpeg error kill startup.
        startup_exception("gpu_detection_error", exc)
        startup_log("gpu_detection_end", available=False, fallback_reason=str(exc)[:240])
        return {
            **_pending_video_acceleration_status(), "current_encoder_label": "CPU x264 (libx264)",
            "fallback_reason": "硬件检测失败，使用 CPU 编码", "detection_pending": False,
        }


def _run_gpu_detection_once() -> None:
    global _GPU_DETECTION_RESULT
    result = _compute_video_acceleration_status()
    with _GPU_DETECTION_LOCK:
        _GPU_DETECTION_RESULT = result


def start_gpu_detection_once() -> None:
    """Schedule one non-blocking startup probe; repeated UI calls are no-ops."""
    global _GPU_DETECTION_STARTED
    with _GPU_DETECTION_LOCK:
        if _GPU_DETECTION_STARTED:
            return
        _GPU_DETECTION_STARTED = True
    threading.Thread(target=_run_gpu_detection_once, name="mixcut-gpu-detection", daemon=True).start()


def reset_gpu_detection_for_tests() -> None:
    global _GPU_DETECTION_RESULT, _GPU_DETECTION_STARTED
    with _GPU_DETECTION_LOCK:
        _GPU_DETECTION_RESULT = None
        _GPU_DETECTION_STARTED = False


class VideoGenerationError(RuntimeError):
    pass


def find_ffmpeg(configured: str = "") -> str:
    try:
        return get_ffmpeg_path()
    except MediaRuntimeError as exc:
        raise VideoGenerationError(str(exc)) from exc


def video_encoder_mode() -> str:
    with connect() as conn:
        item = conn.execute("SELECT value FROM app_settings WHERE key='video_encoder_mode'").fetchone()
    mode = str(item["value"] if item else "auto").strip().lower()
    if mode == "nvenc":  # compatibility with the first acceleration setting
        mode = "gpu"
    return mode if mode in {"auto", "cpu", "gpu"} else "auto"


def detect_video_encoders(ffmpeg: str) -> list[str]:
    try:
        command = [ffmpeg, "-hide_banner", "-encoders"]
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            **background_process_kwargs(command, "gpu_encoder_list"),
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return [name for name in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf") if name in output]
    except (OSError, subprocess.SubprocessError):
        return []


def probe_video_encoder(ffmpeg: str, encoder: str) -> bool:
    """Check that the advertised encoder can actually open on this machine."""
    try:
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                   "-i", "color=c=black:s=128x128:r=1", "-frames:v", "1",
                   "-c:v", encoder, "-f", "null", "-"]
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            **background_process_kwargs(command, "gpu_encoder_probe"),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_gpu_model() -> str:
    return detect_gpu_info()["name"]


def detect_gpu_info() -> dict:
    """Best-effort GPU name/vendor/memory detection without requiring a driver SDK."""
    try:
        command = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            **background_process_kwargs(command, "gpu_detection"),
        )
        line = next((item.strip() for item in (result.stdout or "").splitlines() if item.strip()), "")
        if line:
            name, _, memory = [item.strip() for item in line.partition(",")]
            return {"name": name, "vendor": gpu_vendor(name), "memory": f"{memory} MB" if memory else "未知"}
    except (OSError, subprocess.SubprocessError):
        pass
    for command in (
        ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | ForEach-Object { \"$($_.Name)|$($_.AdapterRAM)\" }"],
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                                    **background_process_kwargs(command, "gpu_cim_detection"))
            values = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            if values:
                name, _, raw_memory = values[0].partition("|")
                try:
                    memory = f"{int(raw_memory) / 1024 / 1024 / 1024:.1f} GB" if raw_memory else "未知"
                except ValueError:
                    memory = "未知"
                return {"name": name.strip(), "vendor": gpu_vendor(name), "memory": memory}
        except (OSError, subprocess.SubprocessError):
            continue
    return {"name": "未检测到", "vendor": "未知", "memory": "未知"}


def gpu_vendor(gpu_name: str) -> str:
    name = str(gpu_name or "").lower()
    if "nvidia" in name or "geforce" in name or "quadro" in name:
        return "NVIDIA"
    if "intel" in name:
        return "Intel"
    if "amd" in name or "radeon" in name:
        return "AMD"
    return "未知"


def encoder_vendor(encoder: str) -> str:
    if encoder.endswith("_nvenc"):
        return "NVIDIA"
    if encoder.endswith("_qsv"):
        return "Intel"
    if encoder.endswith("_amf"):
        return "AMD"
    return "CPU"


def probe_video_encoder_result(ffmpeg: str, encoder: str) -> tuple[bool, str]:
    if encoder not in {"h264_nvenc", "h264_qsv", "h264_amf"}:
        return True, ""
    try:
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                   "-i", "color=c=black:s=128x128:r=1", "-frames:v", "1",
                   "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"]
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            **background_process_kwargs(command, "gpu_encoder_probe"),
        )
        if result.returncode == 0:
            return True, ""
        lines = [line.strip() for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
        detail = next(
            (line for line in reversed(lines)
             if "nothing was written" not in line.lower()
             and any(word in line.lower() for word in ("error", "failed", "function", "driver", "cuda", "capable", "invalid"))),
            lines[-1] if lines else "硬件编码器初始化失败",
        )
        return False, detail[:240]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:240]


def hardware_encoder_details(ffmpeg: str, mode: str | None = None) -> dict:
    selected_mode = str(mode or video_encoder_mode()).lower()
    if selected_mode == "nvenc":
        selected_mode = "gpu"
    gpu = detect_gpu_info()
    gpu_name = gpu["name"]
    vendor = gpu["vendor"]
    listed = detect_video_encoders(ffmpeg)
    preferred = {
        "NVIDIA": ["h264_nvenc"],
        "Intel": ["h264_qsv"],
        "AMD": ["h264_amf"],
        "未知": ["h264_nvenc", "h264_qsv", "h264_amf"],
    }.get(vendor, ["h264_nvenc", "h264_qsv", "h264_amf"])
    candidates = preferred if selected_mode in {"auto", "gpu"} else []
    probes = []
    for encoder in dict.fromkeys(candidates):
        if encoder not in listed:
            probes.append({"encoder": encoder, "listed": False, "usable": False, "reason": "FFmpeg未提供该编码器"})
            continue
        usable, reason = probe_video_encoder_result(ffmpeg, encoder)
        probes.append({"encoder": encoder, "listed": True, "usable": usable, "reason": reason})
        if usable:
            break
    selected = next((item["encoder"] for item in probes if item["usable"]), "libx264")
    failed = next((item for item in probes if item["listed"] and not item["usable"]), None)
    return {
        "mode": selected_mode,
        "gpu_name": gpu_name,
        "gpu_memory": gpu["memory"],
        "gpu_vendor": vendor,
        "encoders": listed,
        "probes": probes,
        "selected_encoder": selected,
        "requested_encoder": (candidates[0] if candidates else "libx264"),
        "fallback_reason": (failed or {}).get("reason", "") if selected == "libx264" and candidates else "",
    }


def select_video_encoder(ffmpeg: str, mode: str | None = None) -> tuple[str, list[str]]:
    details = hardware_encoder_details(ffmpeg, mode)
    return details["selected_encoder"], details["encoders"]


def video_acceleration_status() -> dict:
    start_gpu_detection_once()
    with _GPU_DETECTION_LOCK:
        return dict(_GPU_DETECTION_RESULT or _pending_video_acceleration_status())


def render_timeline_with_fallback(ffmpeg, timeline, final_path, work_dir, encoder, task_id=0):
    """Render once with hardware and retry the same timeline with libx264."""
    try:
        render_timeline(ffmpeg, timeline, final_path, work_dir, encoder)
        return encoder, ""
    except (TimelineError, VideoGenerationError) as gpu_error:
        if encoder == "libx264":
            raise
        reason = str(gpu_error)[:240] or "GPU编码初始化失败"
        heartbeat(task_id, stage="rendering", step="GPU加速不可用，已自动切换CPU编码，继续生成", progress=45)
        final_path.unlink(missing_ok=True)
        render_timeline(ffmpeg, timeline, final_path, work_dir, "libx264")
        return "libx264", reason


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
        "title_style": values.get("title_style") or "legacy",
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
    try:
        from generation_performance import event
        import time as _perf_time
        _accel_started = _perf_time.perf_counter()
        event("external_call_start", 0.0, call="hardware_encoder_details")
    except ImportError:
        _accel_started = None
    acceleration = hardware_encoder_details(ffmpeg)
    if _accel_started is not None:
        event("external_call_end", _perf_time.perf_counter() - _accel_started, call="hardware_encoder_details")
        event("timeline_build_end", 0.0)
    encoder = acceleration["selected_encoder"]
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key='subtitle_style'").fetchone()
    subtitle_style = row["value"] if row else "legacy"
    try:
        from generation_performance import set_encoding, set_hardware
        set_encoding(encoder)
        set_hardware(
            gpu_name=acceleration["gpu_name"],
            gpu_memory=acceleration["gpu_memory"],
            gpu_available=encoder != "libx264",
            requested_encoder=acceleration["requested_encoder"],
            actual_encoder=encoder,
            fallback=encoder == "libx264" and bool(acceleration["fallback_reason"]),
            fallback_reason=acceleration["fallback_reason"],
        )
    except ImportError:
        pass
    # A task owns an immutable material selection.  Never read the template's
    # old editor-preview match here, otherwise every task renders the same clips.
    _check_started = _perf_time.perf_counter() if _accel_started is not None else None
    if _accel_started is not None: event("file_ready_check_start")
    shots = load_template_shots(template_id, task_id)
    try:
        ffprobe_path = get_ffprobe_path()
    except MediaRuntimeError as exc:
        raise VideoGenerationError(str(exc)) from exc
    shots = refresh_real_voice_durations(shots, ffprobe_path)
    if _check_started is not None: event("file_ready_check_end", _perf_time.perf_counter() - _check_started)
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
    timeline["subtitle_style"] = subtitle_style
    with connect() as conn:
        volume_row = conn.execute("SELECT value FROM app_settings WHERE key='voice_volume'").fetchone()
    voice_volume = float(volume_row["value"] or 1.0) if volume_row else 1.0
    for item in timeline.get("items", []):
        if item.get("type") == "audio":
            item["volume"] = voice_volume
    update_task(task_id, status="rendering", current_shot="渲染时间线分镜", progress=45)
    heartbeat(task_id, status="rendering", stage="rendering", step="渲染时间线分镜", progress=45)
    try:
        from generation_performance import measure
        with measure("ffmpeg_render"):
            encoder, render_fallback_reason = render_timeline_with_fallback(
                ffmpeg, timeline, final_path, work_dir, encoder, task_id
            )
        if render_fallback_reason:
            try:
                from generation_performance import set_encoding, set_hardware
                set_encoding(encoder)
                set_hardware(
                    gpu_name=acceleration["gpu_name"], gpu_available=False,
                    gpu_memory=acceleration["gpu_memory"],
                    requested_encoder=acceleration["requested_encoder"],
                    actual_encoder=encoder, fallback=True,
                    fallback_reason=render_fallback_reason,
                )
            except ImportError:
                pass
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
