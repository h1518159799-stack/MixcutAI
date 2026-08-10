"""用用户真实素材库跑通 TTS、ASS 字幕、素材裁剪和最终 MP4 合成。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

from db import APP_DIR, VOICE_DIR, connect, init_db, now
from subtitle_service import generate_ass_for_shot
from video_engine import generate_video
from video_scan import make_thumbnail, probe_video, quick_hash
from runtime_paths import get_ffmpeg_path, get_ffprobe_path


MATERIAL_ROOT = Path("D:/Video/混剪素材")
FFMPEG = Path(get_ffmpeg_path())
FFPROBE = Path(get_ffprobe_path())
TEST_ROOT = APP_DIR / "data" / "user_material_tests"
DESKTOP = Path.home() / "Desktop"

SHOTS = [
    {
        "name": "标准化生产",
        "category": "工厂环境",
        "source": MATERIAL_ROOT / "店内环境" / "2026_07_17_14_18_IMG_9142.MOV",
        "text": "从原料准备到灌装包装，每一瓶洗护产品，都经过标准化生产流程。",
    },
    {
        "name": "产品定制",
        "category": "产品展示",
        "source": MATERIAL_ROOT / "产品展示" / "2026_07_17_14_19_IMG_9153.MOV",
        "text": "针对不同洗护需求，我们支持配方、香型和包装定制。",
    },
    {
        "name": "设备与质检",
        "category": "操作过程",
        "source": MATERIAL_ROOT / "操作过程" / "2026_07_17_14_23_IMG_9165.MOV",
        "text": "自动化设备配合人工质检，关键工序严格把控，让品质稳定可追溯。",
    },
    {
        "name": "一站式交付",
        "category": "服务过程",
        "source": MATERIAL_ROOT / "人物" / "2026_07_13_10_00_IMG_9074.MOV",
        "text": "从打样到量产交付，我们为品牌客户提供一站式洗护产品代工服务。",
    },
]


def log(message: str):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def run(cmd: list[str], timeout: int = 180):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "命令执行失败")[-3000:])
    return result


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return round(wf.getnframes() / float(wf.getframerate()), 3)


def make_voice(text: str, index: int, run_dir: Path) -> tuple[Path, float]:
    text_file = run_dir / f"voice_{index:02d}.txt"
    mp3_file = run_dir / f"voice_{index:02d}.mp3"
    wav_file = VOICE_DIR / f"user_material_test_{run_dir.name}_{index:02d}.wav"
    text_file.write_text(text, encoding="utf-8")
    VOICE_DIR.mkdir(parents=True, exist_ok=True)

    log(f"镜头{index}：Edge TTS 生成语音")
    run(
        [
            "py",
            "-3",
            "-m",
            "edge_tts",
            "--file",
            str(text_file),
            "--voice",
            "zh-CN-XiaoxiaoNeural",
            "--rate",
            "+6%",
            "--write-media",
            str(mp3_file),
        ],
        timeout=180,
    )
    run(
        [
            str(FFMPEG),
            "-y",
            "-i",
            str(mp3_file),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(wav_file),
            "-loglevel",
            "error",
        ]
    )
    duration = wav_duration(wav_file)
    if duration <= 0:
        raise RuntimeError(f"镜头{index}生成的语音时长无效")
    return wav_file, duration


def get_or_create_category(conn: sqlite3.Connection, name: str, order: int) -> int:
    row = conn.execute("SELECT id FROM material_categories WHERE name=?", (name,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO material_categories(name,description,sort_order,created_at,updated_at) VALUES(?,?,?,?,?)",
        (name, "用户素材测试分类", order, now(), now()),
    )
    return int(cur.lastrowid)


def upsert_material(conn: sqlite3.Connection, source: Path, category_id: int, run_dir: Path) -> tuple[int, dict]:
    info = probe_video(source, str(FFPROBE))
    file_hash = quick_hash(source)
    thumb = make_thumbnail(source, f"user_{file_hash}", str(FFMPEG))
    old = conn.execute("SELECT id FROM materials WHERE file_path=?", (str(source),)).fetchone()
    values = (
        category_id,
        source.name,
        file_hash,
        float(info.get("duration") or 0),
        int(info.get("width") or 0),
        int(info.get("height") or 0),
        float(info.get("fps") or 0),
        int(source.stat().st_size),
        thumb,
        "active",
        f"用户素材库生产测试 {run_dir.name}",
        now(),
    )
    if old:
        conn.execute(
            """UPDATE materials SET category_id=?,file_name=?,file_hash=?,duration=?,width=?,height=?,fps=?,size=?,
               thumbnail_path=?,status=?,remark=?,updated_at=? WHERE id=?""",
            values + (int(old["id"]),),
        )
        return int(old["id"]), info
    cur = conn.execute(
        """INSERT INTO materials(category_id,file_path,file_name,file_hash,duration,width,height,fps,size,thumbnail_path,
           status,remark,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            category_id,
            str(source),
            source.name,
            file_hash,
            float(info.get("duration") or 0),
            int(info.get("width") or 0),
            int(info.get("height") or 0),
            float(info.get("fps") or 0),
            int(source.stat().st_size),
            thumb,
            "active",
            f"用户素材库生产测试 {run_dir.name}",
            now(),
            now(),
        ),
    )
    return int(cur.lastrowid), info


def probe_final(path: Path) -> dict:
    result = run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(result.stdout)


def main():
    started = time.perf_counter()
    init_db()
    DESKTOP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = TEST_ROOT / f"run_{stamp}"
    output_dir = run_dir / "outputs"
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_lines: list[str] = []

    log("检查 FFmpeg、FFprobe 和用户素材")
    if not FFMPEG.exists() or not FFPROBE.exists():
        raise RuntimeError("FFmpeg/FFprobe 不存在")
    for shot in SHOTS:
        if not shot["source"].exists():
            raise RuntimeError(f"素材不存在：{shot['source']}")

    with connect() as conn:
        profile_name = f"用户素材测试账号_{stamp}"
        conn.execute(
            """INSERT INTO account_profiles(company_name,industry,product_info,brand_intro,persona_info,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                profile_name,
                "洗护产品生产与代工",
                "洗发、护发及头皮护理产品的配方、包装与量产服务",
                "标准化生产、品质稳定、支持品牌定制",
                "专业可靠的洗护产品工厂账号",
                now(),
                now(),
            ),
        )
        template_id = conn.execute(
            "INSERT INTO edit_templates(name,description,output_width,output_height,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (f"用户真实素材成片测试_{stamp}", "使用 D:/Video/混剪素材 自动生成", 1080, 1920, now(), now()),
        ).lastrowid

        prepared = []
        for index, shot in enumerate(SHOTS, 1):
            step_started = time.perf_counter()
            category_id = get_or_create_category(conn, shot["category"], index)
            material_id, info = upsert_material(conn, shot["source"], category_id, run_dir)
            shot_id = conn.execute(
                """INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,material_category_id,duration,
                   estimated_duration,mute_original_audio,enable_subtitle,enable_tts,voice_file_path,voice_duration,
                   subtitle_file_path,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    template_id,
                    shot["name"],
                    shot["text"],
                    "",
                    category_id,
                    0,
                    0,
                    1,
                    1,
                    1,
                    "",
                    0,
                    "",
                    index,
                    now(),
                    now(),
                ),
            ).lastrowid
            voice_path, voice_duration = make_voice(shot["text"], index, run_dir)
            material_duration = float(info.get("duration") or 0)
            if material_duration + 0.02 < voice_duration:
                raise RuntimeError(
                    f"镜头{index}素材不足：素材 {material_duration:.2f}s，语音 {voice_duration:.2f}s"
                )
            start_time = round(max(0.0, (material_duration - voice_duration) / 2.0), 3)
            end_time = round(start_time + voice_duration, 3)
            subtitle_path, subtitle_mode = generate_ass_for_shot(
                int(shot_id), str(voice_path), fallback_text=shot["text"], duration=voice_duration
            )
            conn.execute(
                """UPDATE template_shots SET duration=?,estimated_duration=?,voice_file_path=?,voice_duration=?,
                   subtitle_file_path=?,updated_at=? WHERE id=?""",
                (voice_duration, voice_duration, str(voice_path), voice_duration, subtitle_path, now(), shot_id),
            )
            conn.execute(
                """INSERT INTO generation_shot_materials(shot_id,material_id,voice_duration,material_start_time,
                   material_end_time,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                (shot_id, material_id, voice_duration, start_time, end_time, now(), now()),
            )
            elapsed = time.perf_counter() - step_started
            prepared.append((shot_id, voice_duration, info, subtitle_mode, start_time, end_time))
            log(
                f"镜头{index}完成：语音 {voice_duration:.2f}s，素材 {material_duration:.2f}s，"
                f"裁剪 {start_time:.2f}-{end_time:.2f}s，字幕 {subtitle_mode}"
            )
            report_lines.append(
                f"镜头{index} {shot['name']} | {shot['source']} | voice={voice_duration:.3f}s | "
                f"cut={start_time:.3f}-{end_time:.3f}s | subtitle={subtitle_mode} | elapsed={elapsed:.2f}s"
            )

        task_id = conn.execute(
            """INSERT INTO generation_tasks(template_id,task_name,status,progress,current_shot,total_shots,output_path,
               error_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (template_id, f"用户真实素材生产测试_{stamp}", "pending", 0, "", len(SHOTS), "", "", now(), now()),
        ).lastrowid

    log("调用 Timeline / FFmpeg 生成最终视频")
    engine_started = time.perf_counter()
    generated = Path(generate_video(int(task_id), int(template_id), str(FFMPEG), str(output_dir)))
    engine_elapsed = time.perf_counter() - engine_started

    desktop_video = DESKTOP / f"AI混剪_真实素材测试_{stamp}.mp4"
    shutil.copy2(generated, desktop_video)
    media = probe_final(desktop_video)
    streams = media.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float((media.get("format") or {}).get("duration") or 0)
    total_elapsed = time.perf_counter() - started

    report = [
        "用户真实素材生产测试报告",
        f"时间：{datetime.now().isoformat(timespec='seconds')}",
        f"素材目录：{MATERIAL_ROOT}",
        f"模板ID：{template_id}",
        f"任务ID：{task_id}",
        "",
        *report_lines,
        "",
        f"FFmpeg合成耗时：{engine_elapsed:.2f}s",
        f"总耗时：{total_elapsed:.2f}s",
        f"输出：{desktop_video}",
        f"时长：{duration:.3f}s",
        f"视频：{video_stream.get('codec_name')} {video_stream.get('width')}x{video_stream.get('height')}",
        f"音频：{audio_stream.get('codec_name')}",
        f"文件大小：{desktop_video.stat().st_size} bytes",
        "验证：PASS" if video_stream.get("codec_name") == "h264" and audio_stream.get("codec_name") == "aac" else "验证：CHECK",
    ]
    report_path = DESKTOP / f"AI混剪_真实素材测试_{stamp}_报告.txt"
    report_path.write_text("\n".join(report), encoding="utf-8")
    (run_dir / "report.txt").write_text("\n".join(report), encoding="utf-8")

    log(f"完成：{desktop_video}")
    print(json.dumps({"video": str(desktop_video), "report": str(report_path), "probe": media}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
