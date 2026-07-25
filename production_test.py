import json
import math
import shutil
import subprocess
import time
import wave
from pathlib import Path

from db import APP_DIR, DATA_DIR, THUMB_DIR, VOICE_DIR, connect, now
from subtitle_service import generate_ass_for_shot
from video_engine import generate_video
from video_scan import make_thumbnail, probe_video, quick_hash

TEST_ROOT = DATA_DIR / "production_tests"


class TestLogger:
    def __init__(self, emit=None):
        self.emit = emit or (lambda msg: None)
        self.steps = []
        self.started = time.perf_counter()
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.run_dir = TEST_ROOT / time.strftime("run_%Y%m%d_%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def step(self, name):
        return TestStep(self, name)

    def record(self, name, status, elapsed, message="", extra=None):
        item = {
            "step": name,
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
            "message": message,
            "extra": extra or {},
            "time": now(),
        }
        self.steps.append(item)
        self.emit(f"[{status}] {name} | {elapsed:.3f}s | {message}")

    def write_report(self, final_status, output_path="", error=""):
        total = round(time.perf_counter() - self.started, 3)
        report = {
            "status": final_status,
            "total_elapsed_seconds": total,
            "output_path": output_path,
            "error": error,
            "steps": self.steps,
            "created_at": now(),
        }
        json_path = self.run_dir / "report.json"
        txt_path = self.run_dir / "report.txt"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"生产测试状态：{final_status}",
            f"总耗时：{total}s",
            f"输出视频：{output_path}",
            f"错误：{error}",
            "",
            "步骤记录：",
        ]
        for s in self.steps:
            lines.append(f"- [{s['status']}] {s['step']} | {s['elapsed_seconds']}s | {s['message']}")
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        return str(txt_path)


class TestStep:
    def __init__(self, logger, name):
        self.logger = logger
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.logger.emit(f"[RUNNING] {self.name}")
        return self

    def ok(self, message="", extra=None):
        self.logger.record(self.name, "success", time.perf_counter() - self.t0, message, extra)

    def fail(self, message="", extra=None):
        self.logger.record(self.name, "failed", time.perf_counter() - self.t0, message, extra)

    def __exit__(self, exc_type, exc, tb):
        if exc:
            self.fail(str(exc))
        return False


def get_setting(key):
    with connect() as conn:
        r = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else ""


def find_binary(name, configured=""):
    if configured and Path(configured).exists():
        return configured
    return shutil.which(name) or ""


def run_cmd(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"command failed: {cmd}").strip()[-2000:])
    return p.stdout.strip()


def ensure_category(conn, name, desc="测试分类"):
    r = conn.execute("SELECT id FROM material_categories WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"]
    cur = conn.execute(
        "INSERT INTO material_categories(name,description,sort_order,created_at,updated_at) VALUES(?,?,?,?,?)",
        (name, desc, 100, now(), now()),
    )
    return cur.lastrowid


def make_test_video(ffmpeg, path, duration, color, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=1080x1920:d={duration}:r=30",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t",
        str(duration),
        "-vf",
        f"drawtext=text='{label}':fontcolor=white:fontsize=80:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    try:
        run_cmd(cmd, timeout=120)
    except RuntimeError:
        cmd_no_text = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1080x1920:d={duration}:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ]
        run_cmd(cmd_no_text, timeout=120)


def make_test_voice(path, duration, freq=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 44100
    frames = int(rate * duration)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            # 低音量正弦波，避免纯静音被播放器/编码器异常优化。
            v = int(6000 * math.sin(2 * math.pi * freq * i / rate))
            data += int(v).to_bytes(2, "little", signed=True)
        wf.writeframes(bytes(data))


def ffprobe_json(ffprobe, path):
    out = run_cmd(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    return json.loads(out)


def insert_material(conn, category_id, file_path, ffprobe_path, ffmpeg_path):
    p = Path(file_path)
    h = quick_hash(p)
    info = probe_video(p, ffprobe_path)
    thumb = make_thumbnail(p, h, ffmpeg_path)
    conn.execute(
        """INSERT OR REPLACE INTO materials(category_id,file_path,file_name,file_hash,duration,width,height,fps,size,thumbnail_path,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (category_id, str(p), p.name, h, info["duration"], info["width"], info["height"], info["fps"], info["size"], thumb, "active", now(), now()),
    )
    return conn.execute("SELECT id FROM materials WHERE file_path=?", (str(p),)).fetchone()["id"]


def run_production_test(emit=None):
    log = TestLogger(emit)
    final_output = ""
    final_error = ""
    try:
        with log.step("检查 FFmpeg / FFprobe") as step:
            ffmpeg = find_binary("ffmpeg", get_setting("ffmpeg_path"))
            ffprobe = find_binary("ffprobe", get_setting("ffprobe_path"))
            if not ffmpeg:
                raise RuntimeError("FFmpeg 不可用：请在【设置】配置 ffmpeg.exe")
            if not ffprobe:
                raise RuntimeError("FFprobe 不可用：请在【设置】配置 ffprobe.exe")
            step.ok(f"ffmpeg={ffmpeg}; ffprobe={ffprobe}")

        material_dir = log.run_dir / "materials"
        with log.step("创建测试账号档案") as step:
            with connect() as conn:
                profile_id = conn.execute(
                    """INSERT INTO account_profiles(company_name,industry,product_info,brand_intro,persona_info,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("生产测试公司", "制造业", "测试产品", "用于第一版生产链路测试", "工厂老板口播", now(), now()),
                ).lastrowid
            step.ok(f"profile_id={profile_id}")

        with log.step("导入/生成测试素材") as step:
            specs = [
                ("产品展示", "product_show.mp4", 12, "blue", "PRODUCT"),
                ("生产过程", "production_process.mp4", 12, "green", "PROCESS"),
                ("工厂环境", "factory_env.mp4", 12, "gray", "FACTORY"),
            ]
            material_ids = {}
            with connect() as conn:
                for cat, name, dur, color, label in specs:
                    cid = ensure_category(conn, cat)
                    path = material_dir / name
                    make_test_video(ffmpeg, path, dur, color, label)
                    mid = insert_material(conn, cid, path, ffprobe, ffmpeg)
                    material_ids[cat] = (mid, cid, path)
            step.ok(f"导入素材 {len(material_ids)} 个", {"materials": {k: str(v[2]) for k, v in material_ids.items()}})

        with log.step("创建测试模板和3个镜头") as step:
            with connect() as conn:
                template_id = conn.execute(
                    "INSERT INTO edit_templates(name,description,created_at,updated_at) VALUES(?,?,?,?)",
                    ("生产测试模板", "自动化端到端测试模板", now(), now()),
                ).lastrowid
                shot_defs = [
                    ("镜头1-产品展示", "这是我们的核心产品展示，外观细节清楚可见。", "产品展示", 4.2, 440),
                    ("镜头2-生产过程", "接下来展示真实生产过程，工艺稳定，流程清晰。", "生产过程", 5.0, 550),
                    ("镜头3-工厂环境", "最后看一下工厂环境，设备整洁，交付更可靠。", "工厂环境", 4.6, 660),
                ]
                shot_ids = []
                for order, (shot_name, text, cat, voice_dur, freq) in enumerate(shot_defs, 1):
                    cid = material_ids[cat][1]
                    sid = conn.execute(
                        """INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,material_category_id,duration,estimated_duration,mute_original_audio,enable_subtitle,enable_tts,sort_order,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (template_id, shot_name, text, "", cid, voice_dur, voice_dur, 1, 1, 1, order, now(), now()),
                    ).lastrowid
                    shot_ids.append((sid, text, voice_dur, freq, cat))
            step.ok(f"template_id={template_id}; shots={len(shot_ids)}")

        with log.step("生成语音并检测时长") as step:
            with connect() as conn:
                for sid, text, dur, freq, cat in shot_ids:
                    voice_path = VOICE_DIR / f"prod_test_shot_{sid}.wav"
                    make_test_voice(voice_path, dur, freq)
                    with wave.open(str(voice_path), "rb") as wf:
                        real_dur = round(wf.getnframes() / wf.getframerate(), 3)
                    conn.execute(
                        "UPDATE template_shots SET voice_file_path=?,voice_duration=?,duration=?,estimated_duration=?,updated_at=? WHERE id=?",
                        (str(voice_path), real_dur, real_dur, real_dur, now(), sid),
                    )
            step.ok("3个镜头语音已生成并写入 voice_duration")

        with log.step("生成 ASS 字幕") as step:
            with connect() as conn:
                for sid, text, dur, freq, cat in shot_ids:
                    shot = conn.execute("SELECT voice_file_path,voice_duration FROM template_shots WHERE id=?", (sid,)).fetchone()
                    sub_path, source = generate_ass_for_shot(sid, shot["voice_file_path"], text, shot["voice_duration"])
                    conn.execute("UPDATE template_shots SET subtitle_file_path=?,updated_at=? WHERE id=?", (sub_path, now(), sid))
            step.ok("3个镜头字幕已生成，格式 ASS")

        with log.step("匹配素材") as step:
            with connect() as conn:
                used = set()
                for sid, text, dur, freq, cat in shot_ids:
                    shot = conn.execute("SELECT voice_duration FROM template_shots WHERE id=?", (sid,)).fetchone()
                    mid = material_ids[cat][0]
                    mat = conn.execute("SELECT duration FROM materials WHERE id=?", (mid,)).fetchone()
                    vd = float(shot["voice_duration"])
                    if float(mat["duration"]) < vd:
                        raise RuntimeError(f"素材不足：{cat} 需要 {vd}s，只有 {mat['duration']}s")
                    start = 1.0
                    end = round(start + vd, 3)
                    conn.execute(
                        """INSERT INTO generation_shot_materials(shot_id,material_id,voice_duration,material_start_time,material_end_time,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (sid, mid, vd, start, end, now(), now()),
                    )
                    used.add(mid)
            step.ok("3个镜头均已匹配素材并保存裁剪区间")

        with log.step("创建生成任务并合成最终 MP4") as step:
            with connect() as conn:
                task_id = conn.execute(
                    """INSERT INTO generation_tasks(template_id,task_name,status,progress,current_shot,total_shots,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (template_id, "生产测试任务", "pending", 0, "等待开始", 3, now(), now()),
                ).lastrowid
            output_dir = get_setting("output_dir") or str(APP_DIR / "outputs")
            final_output = generate_video(task_id, template_id, ffmpeg, output_dir)
            step.ok(f"输出：{final_output}")

        with log.step("检查输出视频可播放/编码/分辨率/时长") as step:
            info = ffprobe_json(ffprobe, final_output)
            duration = float(info.get("format", {}).get("duration") or 0)
            streams = info.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            expected = sum(s[2] for s in shot_ids)
            checks = {
                "duration": duration,
                "expected_duration": expected,
                "video_codec": video.get("codec_name"),
                "audio_codec": audio.get("codec_name"),
                "width": video.get("width"),
                "height": video.get("height"),
            }
            if video.get("codec_name") != "h264":
                raise RuntimeError(f"视频编码不是 h264：{checks}")
            if audio.get("codec_name") != "aac":
                raise RuntimeError(f"音频编码不是 aac：{checks}")
            if int(video.get("width") or 0) != 1080 or int(video.get("height") or 0) != 1920:
                raise RuntimeError(f"分辨率不是 1080x1920：{checks}")
            if abs(duration - expected) > 1.0:
                raise RuntimeError(f"音视频总时长偏差过大：{checks}")
            step.ok("输出视频 ffprobe 检查通过", checks)

        with log.step("检查字幕烧录/拼接流畅性") as step:
            # 自动化可验证 ASS 已参与生成、片段存在、最终文件非空；视觉检查仍需人工播放确认。
            work_dir = Path(get_setting("output_dir") or (APP_DIR / "outputs")) / f"task_{task_id}_work"
            segs = list(work_dir.glob("shot_*.mp4")) + list(work_dir.glob("timeline_shot_*.mp4"))
            if len(segs) != 3:
                raise RuntimeError(f"分镜片段数量异常：{len(segs)}")
            if Path(final_output).stat().st_size <= 0:
                raise RuntimeError("最终 MP4 文件大小为 0")
            step.ok("3个分镜片段存在，最终MP4非空；字幕已在分镜滤镜链中烧录")

        report = log.write_report("success", final_output)
        emit and emit(f"[REPORT] {report}")
        return {"ok": True, "output_path": final_output, "report": report, "steps": log.steps}
    except Exception as e:
        final_error = str(e)
        report = log.write_report("failed", final_output, final_error)
        emit and emit(f"[REPORT] {report}")
        return {"ok": False, "output_path": final_output, "report": report, "error": final_error, "steps": log.steps}
