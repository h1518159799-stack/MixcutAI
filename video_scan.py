
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

from db import THUMB_DIR
from runtime_paths import MediaRuntimeError, get_ffmpeg_path, get_ffprobe_path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpeg", ".mpg"}


def find_executable(name: str, configured: str = "") -> Optional[str]:
    try:
        return get_ffprobe_path() if name.lower().startswith("ffprobe") else get_ffmpeg_path()
    except MediaRuntimeError:
        return None


def scan_video_files(folder: str) -> List[Path]:
    root = Path(folder)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]


def quick_hash(path: Path) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(max(0, size - 1024 * 1024))
            h.update(f.read(1024 * 1024))
    h.update(str(size).encode())
    return h.hexdigest()


def probe_video(path: Path, ffprobe_path: str = "") -> Dict:
    result = {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "size": path.stat().st_size if path.exists() else 0,
        "codec": "",
        "error": "",
    }
    ffprobe = find_executable("ffprobe", ffprobe_path)
    if not ffprobe:
        result["error"] = "视频处理组件缺失或损坏，请重新安装Mixcut Studio"
        return result
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,codec_name:format=duration,size",
        "-of", "json",
        str(path),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15, encoding="utf-8", errors="ignore",
                           **background_process_kwargs(cmd, "media_probe"))
        if p.returncode != 0:
            detail = (p.stderr or "").lower()
            result["error"] = "文件正在被其他程序占用" if "permission denied" in detail else "素材无法读取或视频格式异常"
            return result
        data = json.loads(p.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            result["error"] = "未检测到有效视频轨道"
            return result
        stream = streams[0]
        fmt = data.get("format") or {}
        result["width"] = int(stream.get("width") or 0)
        result["height"] = int(stream.get("height") or 0)
        result["codec"] = str(stream.get("codec_name") or "")
        result["duration"] = float(fmt.get("duration") or 0)
        result["size"] = int(fmt.get("size") or result["size"] or 0)
        rate = stream.get("r_frame_rate") or "0/1"
        if "/" in rate:
            a, b = rate.split("/", 1)
            result["fps"] = round(float(a) / float(b), 3) if float(b or 1) else 0.0
        else:
            result["fps"] = float(rate or 0)
        if result["duration"] <= 0 or result["width"] <= 0 or result["height"] <= 0:
            result["error"] = "视频元数据不完整"
    except subprocess.TimeoutExpired:
        result["error"] = "FFprobe处理超时"
    except PermissionError:
        result["error"] = "文件正在被其他程序占用"
    except (OSError, ValueError, json.JSONDecodeError):
        result["error"] = "素材无法读取或视频格式异常"
    return result


def make_thumbnail_with_status(path: Path, file_hash: str, ffmpeg_path: str = "") -> tuple[str, str]:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"{file_hash[:16]}.jpg"
    ffmpeg = find_executable("ffmpeg", ffmpeg_path)
    if ffmpeg:
        cmd = [
            ffmpeg, "-y", "-ss", "00:00:01", "-i", str(path),
            "-frames:v", "1", "-vf", "scale=320:-1", str(out)
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25, encoding="utf-8", errors="ignore",
                               **background_process_kwargs(cmd, "thumbnail_export"))
            if p.returncode == 0 and out.exists():
                return str(out), ""
            return create_placeholder_thumbnail(path, out), "缩略图生成失败，已使用占位图"
        except subprocess.TimeoutExpired:
            return create_placeholder_thumbnail(path, out), "缩略图生成超时，已使用占位图"
        except Exception:
            return create_placeholder_thumbnail(path, out), "缩略图生成失败，已使用占位图"
    return create_placeholder_thumbnail(path, out), "视频处理组件缺失或损坏，已使用占位图"


def make_thumbnail(path: Path, file_hash: str, ffmpeg_path: str = "") -> str:
    return make_thumbnail_with_status(path, file_hash, ffmpeg_path)[0]


def create_placeholder_thumbnail(path: Path, out: Path) -> str:
    img = Image.new("RGB", (320, 180), color=(22, 28, 36))
    draw = ImageDraw.Draw(img)
    accent = (55, 135, 245)
    draw.rectangle((0, 0, 319, 179), outline=(45, 55, 72), width=3)
    draw.polygon([(132, 55), (132, 125), (198, 90)], fill=accent)
    name = path.name
    if len(name) > 34:
        name = name[:31] + "..."
    draw.text((16, 142), name, fill=(230, 235, 245))
    img.save(out, "JPEG", quality=90)
    return str(out)
from subprocess_utils import background_process_kwargs
