"""Local shot-boundary detection and precise segment export for Mixcut Studio.

This module deliberately owns its own worker, cancellation state and SQLite rows.
It never touches the generation queue or generation task tables.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

from db import DATA_DIR, connect, now
from runtime_paths import LOG_DIR, MediaRuntimeError, get_ffmpeg_path, get_ffprobe_path
from subprocess_utils import background_process_kwargs
from video_scan import VIDEO_EXTS, quick_hash


MAX_SMART_SPLIT_CONCURRENCY = 1
SMART_SPLIT_DIR = DATA_DIR / "smart_split"
THUMBNAIL_DIR = SMART_SPLIT_DIR / "thumbnails"
SENSITIVITY_THRESHOLDS = {
    "conservative": 0.42,
    "standard": 0.30,
    "sensitive": 0.24,
}
DEFAULT_MIN_SEGMENT_DURATION = 1.0
DEFAULT_MIN_CUT_GAP = 0.65
MAX_SEGMENTS = 600

_WORKER_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_CANCEL_EVENTS: dict[int, threading.Event] = {}
_PROCESSES: dict[int, subprocess.Popen] = {}


class SmartSplitError(RuntimeError):
    pass


def _row(sql: str, args=()) -> dict | None:
    with connect() as conn:
        value = conn.execute(sql, args).fetchone()
        return dict(value) if value else None


def _rows(sql: str, args=()) -> list[dict]:
    with connect() as conn:
        return [dict(value) for value in conn.execute(sql, args).fetchall()]


def _settings() -> dict[str, str]:
    with connect() as conn:
        return {str(row["key"]): str(row["value"] or "") for row in conn.execute("SELECT key,value FROM app_settings")}


def _update_job(job_id: int, **values) -> None:
    if not values:
        return
    values["updated_at"] = now()
    columns = ",".join(f"{key}=?" for key in values)
    with connect() as conn:
        conn.execute(f"UPDATE smart_split_jobs SET {columns} WHERE id=?", (*values.values(), int(job_id)))


def _set_process(job_id: int, process: subprocess.Popen | None) -> None:
    with _STATE_LOCK:
        if process is None:
            _PROCESSES.pop(int(job_id), None)
        else:
            _PROCESSES[int(job_id)] = process


def _cancelled(job_id: int) -> bool:
    with _STATE_LOCK:
        event = _CANCEL_EVENTS.get(int(job_id))
    if event and event.is_set():
        return True
    job = _row("SELECT cancel_requested FROM smart_split_jobs WHERE id=?", (int(job_id),))
    return bool(job and int(job.get("cancel_requested") or 0))


def _safe_error(exc: Exception) -> str:
    text = str(exc or "").strip()
    lower = text.lower()
    if isinstance(exc, FileNotFoundError):
        return "源视频文件不存在"
    if isinstance(exc, PermissionError):
        return "输出目录没有写入权限"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "FFmpeg 处理超时"
    if "no space" in lower or "not enough space" in lower:
        return "磁盘空间不足"
    if "invalid data" in lower or "moov atom" in lower:
        return "视频文件损坏或无法解析"
    if text.startswith("[") or "traceback" in lower:
        return "视频处理失败，请检查源视频和 FFmpeg 配置"
    return text[:240] or "视频处理失败"


def select_video_file() -> str:
    if os.name != "nt":
        return ""
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择需要智能分割的视频'
$dialog.Filter = '视频文件|*.mp4;*.mov;*.mkv;*.m4v;*.avi;*.webm;*.wmv;*.mpeg;*.mpg|所有文件|*.*'
$dialog.Multiselect = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.FileName) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded], "smart_split_folder_dialog"),
    )
    return (result.stdout or "").strip()


def _configured_executable(kind: str) -> str:
    try:
        return get_ffprobe_path() if kind == "ffprobe" else get_ffmpeg_path()
    except MediaRuntimeError as exc:
        raise SmartSplitError(str(exc)) from exc


def probe_video(source_path: str | Path, ffprobe_path: str = "") -> dict:
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in VIDEO_EXTS:
        raise SmartSplitError("当前视频格式不受支持")
    ffprobe = _configured_executable("ffprobe")
    command = [
        ffprobe, "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", str(source),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace", **background_process_kwargs(command, "smart_split_probe"),
    )
    if result.returncode != 0:
        raise SmartSplitError("视频文件损坏或 FFprobe 无法解析")
    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        video = next(item for item in streams if item.get("codec_type") == "video")
        fmt = payload.get("format") or {}
        duration = float(fmt.get("duration") or 0)
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
        if "/" in rate:
            numerator, denominator = rate.split("/", 1)
            fps = float(numerator or 0) / max(float(denominator or 1), 1e-9)
        else:
            fps = float(rate or 0)
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SmartSplitError("视频元数据不完整") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise SmartSplitError("视频时长异常")
    stat = source.stat()
    return {
        "file_name": source.name,
        "file_path": str(source.resolve()),
        "duration": round(duration, 6),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
        "size": int(fmt.get("size") or stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "codec": str(video.get("codec_name") or ""),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
    }


def source_fingerprint(source_path: str | Path) -> dict:
    source = Path(source_path)
    stat = source.stat()
    return {
        "hash": quick_hash(source),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def normalize_cut_points(
    candidates: Iterable[dict | tuple | float], duration: float, min_cut_gap: float = DEFAULT_MIN_CUT_GAP,
) -> list[dict]:
    duration = max(0.0, float(duration))
    normalized: list[dict] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            cut_time = float(candidate.get("time") or 0)
            score = float(candidate.get("score") or 0)
        elif isinstance(candidate, tuple):
            cut_time = float(candidate[0])
            score = float(candidate[1] if len(candidate) > 1 else 0)
        else:
            cut_time, score = float(candidate), 0.0
        if math.isfinite(cut_time) and 0 < cut_time < duration:
            normalized.append({"time": cut_time, "score": max(0.0, score)})
    normalized.sort(key=lambda item: item["time"])
    clusters: list[list[dict]] = []
    for item in normalized:
        if not clusters or item["time"] - clusters[-1][-1]["time"] >= float(min_cut_gap):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    # Fades and soft transitions can trigger several adjacent candidates.  Keep
    # only the strongest score in each temporal cluster.
    return [max(cluster, key=lambda item: (item["score"], item["time"])) for cluster in clusters]


def build_segments(cut_points: Iterable[dict], duration: float) -> list[dict]:
    duration = round(max(0.0, float(duration)), 6)
    cuts = [item for item in cut_points if 0 < float(item["time"]) < duration]
    boundaries = [0.0] + [round(float(item["time"]), 6) for item in cuts] + [duration]
    scores = {round(float(item["time"]), 6): float(item.get("score") or 0) for item in cuts}
    segments: list[dict] = []
    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end <= start:
            continue
        segments.append({
            "segment_index": index + 1, "start_time": start, "end_time": end,
            "duration": round(end - start, 6), "scene_score": scores.get(end, 0.0),
            "selected": 1, "category": "", "thumbnail_path": "", "output_path": "",
            "status": "detected", "error_message": "",
        })
    return segments or [{
        "segment_index": 1, "start_time": 0.0, "end_time": duration,
        "duration": duration, "scene_score": 0.0, "selected": 1, "category": "",
        "thumbnail_path": "", "output_path": "", "status": "detected", "error_message": "",
    }]


def merge_short_segments(segments: list[dict], min_duration: float = DEFAULT_MIN_SEGMENT_DURATION) -> list[dict]:
    result = [dict(item) for item in segments]
    minimum = max(0.1, float(min_duration))
    while len(result) > 1:
        short_index = next((i for i, item in enumerate(result) if float(item["duration"]) < minimum), None)
        if short_index is None:
            break
        if short_index == 0:
            neighbor = 1
        elif short_index == len(result) - 1:
            neighbor = short_index - 1
        else:
            left_duration = float(result[short_index - 1]["duration"])
            right_duration = float(result[short_index + 1]["duration"])
            neighbor = short_index - 1 if left_duration >= right_duration else short_index + 1
        first, second = sorted((short_index, neighbor))
        merged = dict(result[first])
        merged["start_time"] = float(result[first]["start_time"])
        merged["end_time"] = float(result[second]["end_time"])
        merged["duration"] = round(merged["end_time"] - merged["start_time"], 6)
        merged["scene_score"] = min(float(result[first].get("scene_score") or 0), float(result[second].get("scene_score") or 0))
        result[first:second + 1] = [merged]
    for index, item in enumerate(result, 1):
        item["segment_index"] = index
        item["duration"] = round(float(item["end_time"]) - float(item["start_time"]), 6)
    return result


_TIME_RE = re.compile(r"pts_time:([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def detect_scene_changes(
    source_path: str | Path, threshold: float, min_cut_gap: float = DEFAULT_MIN_CUT_GAP,
    ffmpeg_path: str = "", cancel_check: Callable[[], bool] | None = None,
) -> list[dict]:
    source = Path(source_path)
    ffmpeg = _configured_executable("ffmpeg")
    threshold = min(0.9, max(0.05, float(threshold)))
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
        "-vf", f"scale=640:-2:flags=bilinear,select='gt(scene,{threshold:.4f})',metadata=print:file=-",
        "-an", "-f", "null", os.devnull,
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
        **background_process_kwargs(command, "smart_split_scene_detection"),
    )
    candidates: list[dict] = []
    pending_time: float | None = None
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if cancel_check and cancel_check():
                process.terminate()
                raise SmartSplitError("任务已取消")
            time_match = _TIME_RE.search(line)
            if time_match:
                pending_time = float(time_match.group(1))
            score_match = _SCORE_RE.search(line)
            if score_match and pending_time is not None:
                candidates.append({"time": pending_time, "score": float(score_match.group(1))})
                pending_time = None
                if len(candidates) > MAX_SEGMENTS * 3:
                    process.terminate()
                    raise SmartSplitError("检测到过多切点，请使用更保守的灵敏度")
        return_code = process.wait(timeout=10)
        if return_code != 0:
            raise SmartSplitError("FFmpeg 场景检测失败")
    finally:
        if process.poll() is None:
            process.kill()
    return candidates


def _persist_segments(job_id: int, segments: list[dict]) -> None:
    stamp = now()
    with connect() as conn:
        conn.execute("DELETE FROM smart_split_segments WHERE job_id=?", (int(job_id),))
        for index, item in enumerate(segments, 1):
            conn.execute(
                """INSERT INTO smart_split_segments
                   (job_id,segment_index,start_time,end_time,duration,scene_score,selected,category,
                    thumbnail_path,output_path,status,error_message,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(job_id), index, float(item["start_time"]), float(item["end_time"]),
                 float(item["end_time"]) - float(item["start_time"]), float(item.get("scene_score") or 0),
                 int(bool(item.get("selected", 1))), str(item.get("category") or ""),
                 str(item.get("thumbnail_path") or ""), str(item.get("output_path") or ""),
                 str(item.get("status") or "detected"), str(item.get("error_message") or ""), stamp, stamp),
            )


def generate_preview_thumbnails(job_id: int, source_path: str | Path, segments: list[dict], ffmpeg_path: str = "") -> list[dict]:
    ffmpeg = _configured_executable("ffmpeg")
    target_dir = THUMBNAIL_DIR / str(int(job_id))
    target_dir.mkdir(parents=True, exist_ok=True)
    result: list[dict] = []
    for index, item in enumerate(segments, 1):
        if _cancelled(job_id):
            raise SmartSplitError("任务已取消")
        current = dict(item)
        midpoint = float(item["start_time"]) + max(0.05, float(item["duration"]) * 0.5)
        output = target_dir / f"segment_{index:03d}.jpg"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{midpoint:.6f}",
            "-i", str(source_path), "-frames:v", "1", "-vf", "scale=320:-2:flags=lanczos",
            "-q:v", "3", str(output),
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   **background_process_kwargs(command, "smart_split_thumbnail"))
        _set_process(job_id, process)
        stdout, stderr = process.communicate(timeout=45)
        _set_process(job_id, None)
        if process.returncode == 0 and output.is_file():
            current["thumbnail_path"] = str(output)
        else:
            current["thumbnail_path"] = ""
        result.append(current)
        _update_job(job_id, progress=min(95, 65 + int(index / max(1, len(segments)) * 30)))
    return result


def create_detection_job(
    source_path: str, sensitivity: str = "standard", threshold: float | None = None,
    min_segment_duration: float = DEFAULT_MIN_SEGMENT_DURATION, force: bool = False,
) -> dict:
    source = Path(source_path).resolve()
    metadata = probe_video(source)
    fingerprint = source_fingerprint(source)
    sensitivity = sensitivity if sensitivity in SENSITIVITY_THRESHOLDS else "standard"
    selected_threshold = float(threshold if threshold is not None else SENSITIVITY_THRESHOLDS[sensitivity])
    selected_threshold = min(0.9, max(0.05, selected_threshold))
    minimum = min(30.0, max(0.3, float(min_segment_duration)))
    cached = None if force else _row(
        """SELECT id FROM smart_split_jobs
           WHERE source_hash=? AND source_size=? AND source_mtime_ns=? AND threshold=?
             AND min_segment_duration=? AND status IN ('waiting_confirmation','completed')
           ORDER BY id DESC LIMIT 1""",
        (fingerprint["hash"], fingerprint["size"], fingerprint["mtime_ns"], selected_threshold, minimum),
    )
    if cached:
        result = get_job(int(cached["id"]))
        result["cache_found"] = True
        return result
    active = _row("SELECT id FROM smart_split_jobs WHERE status IN ('pending','detecting','exporting') ORDER BY id LIMIT 1")
    if active:
        raise SmartSplitError("已有智能分割任务正在处理，请完成或取消后再试")
    stamp = now()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO smart_split_jobs
               (source_path,source_hash,source_size,source_mtime_ns,source_duration,source_width,source_height,
                source_fps,has_audio,sensitivity,threshold,min_segment_duration,min_cut_gap,status,stage,
                progress,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','读取视频信息',5,?,?)""",
            (str(source), fingerprint["hash"], fingerprint["size"], fingerprint["mtime_ns"],
             metadata["duration"], metadata["width"], metadata["height"], metadata["fps"],
             int(metadata["has_audio"]), sensitivity, selected_threshold, minimum, DEFAULT_MIN_CUT_GAP,
             stamp, stamp),
        )
        job_id = int(cursor.lastrowid)
    start_detection(job_id)
    return get_job(job_id)


def start_detection(job_id: int) -> None:
    thread = threading.Thread(target=_detection_worker, args=(int(job_id),), name=f"smart-split-detect-{job_id}", daemon=True)
    thread.start()


def _detection_worker(job_id: int) -> None:
    event = threading.Event()
    with _STATE_LOCK:
        _CANCEL_EVENTS[job_id] = event
    if not _WORKER_LOCK.acquire(blocking=False):
        _update_job(job_id, status="failed", stage="", error_message="已有智能分割任务正在处理")
        with _STATE_LOCK:
            _CANCEL_EVENTS.pop(job_id, None)
        return
    try:
        job = _row("SELECT * FROM smart_split_jobs WHERE id=?", (job_id,))
        if not job:
            return
        _update_job(job_id, status="detecting", stage="检测镜头变化", progress=20, error_message="")
        ffmpeg = _configured_executable("ffmpeg")
        # Keep a cancellable process reference while parsing scene metadata.
        source = Path(job["source_path"])
        candidates = _detect_for_job(job_id, source, float(job["threshold"]), float(job["min_cut_gap"]), ffmpeg)
        if _cancelled(job_id):
            raise SmartSplitError("任务已取消")
        _update_job(job_id, stage="整理切点", progress=62)
        cuts = normalize_cut_points(candidates, float(job["source_duration"]), float(job["min_cut_gap"]))
        segments = merge_short_segments(build_segments(cuts, float(job["source_duration"])), float(job["min_segment_duration"]))
        if len(segments) > MAX_SEGMENTS:
            raise SmartSplitError("检测到过多镜头，请调高灵敏度后重试")
        _persist_segments(job_id, segments)
        _update_job(job_id, stage="生成缩略图", progress=65)
        segments = generate_preview_thumbnails(job_id, source, segments, ffmpeg)
        _persist_segments(job_id, segments)
        message = "未检测到明显镜头切换，当前视频可能是一个连续镜头" if len(segments) == 1 else ""
        _update_job(job_id, status="waiting_confirmation", stage="完成", progress=100, error_message=message)
    except Exception as exc:
        cancelled = _cancelled(job_id) or str(exc) == "任务已取消"
        _update_job(job_id, status="cancelled" if cancelled else "failed", stage="", progress=0 if cancelled else 100, error_message="" if cancelled else _safe_error(exc), finished_at=now())
    finally:
        _set_process(job_id, None)
        with _STATE_LOCK:
            _CANCEL_EVENTS.pop(job_id, None)
        _WORKER_LOCK.release()


def _detect_for_job(job_id: int, source: Path, threshold: float, min_gap: float, ffmpeg: str) -> list[dict]:
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
        "-vf", f"scale=640:-2:flags=bilinear,select='gt(scene,{threshold:.4f})',metadata=print:file=-",
        "-an", "-f", "null", os.devnull,
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", bufsize=1,
                               **background_process_kwargs(command, "smart_split_detection"))
    _set_process(job_id, process)
    candidates: list[dict] = []
    pending_time: float | None = None
    assert process.stdout is not None
    for line in process.stdout:
        if _cancelled(job_id):
            process.terminate()
            raise SmartSplitError("任务已取消")
        time_match = _TIME_RE.search(line)
        if time_match:
            pending_time = float(time_match.group(1))
        score_match = _SCORE_RE.search(line)
        if score_match and pending_time is not None:
            candidates.append({"time": pending_time, "score": float(score_match.group(1))})
            pending_time = None
            if len(candidates) > MAX_SEGMENTS * 3:
                process.terminate()
                raise SmartSplitError("检测到过多切点，请使用更保守的灵敏度")
    code = process.wait(timeout=10)
    _set_process(job_id, None)
    if code != 0:
        raise SmartSplitError("FFmpeg 场景检测失败")
    return candidates


def get_job(job_id: int) -> dict:
    job = _row("SELECT * FROM smart_split_jobs WHERE id=?", (int(job_id),))
    if not job:
        raise SmartSplitError("智能分割任务不存在")
    segments = _rows("SELECT * FROM smart_split_segments WHERE job_id=? ORDER BY segment_index,id", (int(job_id),))
    for segment in segments:
        segment["thumbnail_url"] = f"/media?path={quote(str(segment['thumbnail_path']))}" if segment.get("thumbnail_path") else ""
        segment["output_exists"] = bool(segment.get("output_path") and Path(segment["output_path"]).is_file())
    job["segments"] = segments
    job["segment_count"] = len(segments)
    job["selected_count"] = sum(1 for item in segments if int(item.get("selected") or 0))
    job["source_exists"] = Path(job["source_path"]).is_file()
    return job


def list_jobs(limit: int = 20) -> list[dict]:
    jobs = _rows("SELECT * FROM smart_split_jobs ORDER BY id DESC LIMIT ?", (max(1, min(100, int(limit))),))
    for job in jobs:
        job["segment_count"] = int(_row("SELECT COUNT(*) count FROM smart_split_segments WHERE job_id=?", (job["id"],))["count"])
    return jobs


def update_segment(job_id: int, segment_id: int, data: dict) -> dict:
    job = get_job(job_id)
    if job["status"] in {"detecting", "exporting"}:
        raise SmartSplitError("任务处理中，暂时不能修改镜头")
    segments = [dict(item) for item in job["segments"]]
    index = next((i for i, item in enumerate(segments) if int(item["id"]) == int(segment_id)), None)
    if index is None:
        raise SmartSplitError("镜头不存在")
    current = segments[index]
    if "selected" in data:
        current["selected"] = int(bool(data["selected"]))
    if "category" in data:
        current["category"] = str(data.get("category") or "")
    source_duration = float(job["source_duration"])
    start = float(data.get("start_time", current["start_time"]))
    end = float(data.get("end_time", current["end_time"]))
    start = max(0.0, min(source_duration, start))
    end = max(0.0, min(source_duration, end))
    if end - start < 0.1:
        raise SmartSplitError("镜头时长不能小于 0.1 秒")
    current["start_time"], current["end_time"] = start, end
    if index > 0:
        segments[index - 1]["end_time"] = start
    if index + 1 < len(segments):
        segments[index + 1]["start_time"] = end
    for item in segments:
        item["duration"] = round(float(item["end_time"]) - float(item["start_time"]), 6)
        if item["duration"] <= 0:
            raise SmartSplitError("调整后的镜头时间发生重叠")
    _persist_segments(job_id, segments)
    return get_job(job_id)


def set_all_selected(job_id: int, selected: bool) -> dict:
    with connect() as conn:
        conn.execute("UPDATE smart_split_segments SET selected=?,updated_at=? WHERE job_id=?", (int(bool(selected)), now(), int(job_id)))
    return get_job(job_id)


def set_selected_category(job_id: int, category: str, segment_ids: list[int] | None = None) -> dict:
    ids = [int(value) for value in (segment_ids or []) if int(value) > 0]
    with connect() as conn:
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"UPDATE smart_split_segments SET category=?,updated_at=? WHERE job_id=? AND id IN ({marks})", (str(category or ""), now(), int(job_id), *ids))
        else:
            conn.execute("UPDATE smart_split_segments SET category=?,updated_at=? WHERE job_id=? AND selected=1", (str(category or ""), now(), int(job_id)))
    return get_job(job_id)


def merge_segments(job_id: int, segment_ids: list[int]) -> dict:
    job = get_job(job_id)
    ids = {int(value) for value in segment_ids}
    indexes = [i for i, item in enumerate(job["segments"]) if int(item["id"]) in ids]
    if len(indexes) != 2 or indexes[1] != indexes[0] + 1:
        raise SmartSplitError("请选择两个相邻镜头进行合并")
    segments = [dict(item) for item in job["segments"]]
    first, second = indexes
    merged = dict(segments[first])
    merged["end_time"] = float(segments[second]["end_time"])
    merged["duration"] = merged["end_time"] - float(merged["start_time"])
    merged["selected"] = int(bool(segments[first].get("selected") or segments[second].get("selected")))
    merged["scene_score"] = min(float(segments[first].get("scene_score") or 0), float(segments[second].get("scene_score") or 0))
    merged["thumbnail_path"] = segments[first].get("thumbnail_path") or segments[second].get("thumbnail_path") or ""
    segments[first:second + 1] = [merged]
    _persist_segments(job_id, segments)
    return get_job(job_id)


def split_segment(job_id: int, cut_time: float) -> dict:
    job = get_job(job_id)
    cut_time = float(cut_time)
    segments = [dict(item) for item in job["segments"]]
    index = next((i for i, item in enumerate(segments) if float(item["start_time"]) + 0.1 < cut_time < float(item["end_time"]) - 0.1), None)
    if index is None:
        raise SmartSplitError("当前播放位置太靠近镜头边界")
    source = segments[index]
    left, right = dict(source), dict(source)
    left["end_time"] = cut_time
    right["start_time"] = cut_time
    left["duration"] = cut_time - float(left["start_time"])
    right["duration"] = float(right["end_time"]) - cut_time
    left["scene_score"], right["scene_score"] = 0.0, 0.0
    left["thumbnail_path"], right["thumbnail_path"] = "", ""
    segments[index:index + 1] = [left, right]
    _persist_segments(job_id, segments)
    # Generate only missing thumbnails; a failure does not invalidate editing.
    try:
        refreshed = get_job(job_id)["segments"]
        thumbs = generate_preview_thumbnails(job_id, job["source_path"], refreshed)
        _persist_segments(job_id, thumbs)
        _update_job(job_id, progress=100)
    except Exception:
        pass
    return get_job(job_id)


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return (cleaned[:80] or "video")


def _unique_output(directory: Path, stem: str, index: int) -> Path:
    base = directory / f"{stem}_{index:03d}.mp4"
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = directory / f"{stem}_{index:03d}_{suffix}.mp4"
        if not candidate.exists():
            return candidate
        suffix += 1


def _export_video_options(width: int, height: int) -> list[str]:
    """Keep native resolution while bounding x264 memory for 4K sources."""
    pixels = max(0, int(width)) * max(0, int(height))
    if pixels >= 3840 * 2160:
        return ["-c:v", "libx264", "-preset", "veryfast", "-threads", "4", "-crf", "20", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


def start_export(job_id: int, output_root: str = "") -> dict:
    job = get_job(job_id)
    if job["status"] not in {"waiting_confirmation", "completed", "failed", "cancelled"}:
        raise SmartSplitError("当前任务状态不能开始分割")
    if not any(int(item.get("selected") or 0) for item in job["segments"]):
        raise SmartSplitError("请至少选择一个镜头")
    if _row("SELECT id FROM smart_split_jobs WHERE id<>? AND status IN ('pending','detecting','exporting') LIMIT 1", (int(job_id),)):
        raise SmartSplitError("已有智能分割任务正在处理")
    _update_job(job_id, status="exporting", stage="准备分割", progress=0, cancel_requested=0, error_message="", exported_count=0, failed_count=0)
    thread = threading.Thread(target=_export_worker, args=(int(job_id), str(output_root or "")), name=f"smart-split-export-{job_id}", daemon=True)
    thread.start()
    return get_job(job_id)


def _export_worker(job_id: int, output_root: str) -> None:
    event = threading.Event()
    with _STATE_LOCK:
        _CANCEL_EVENTS[job_id] = event
    if not _WORKER_LOCK.acquire(blocking=False):
        _update_job(job_id, status="failed", error_message="已有智能分割任务正在处理")
        with _STATE_LOCK:
            _CANCEL_EVENTS.pop(job_id, None)
        return
    try:
        job = get_job(job_id)
        source = Path(job["source_path"])
        if not source.is_file():
            raise FileNotFoundError(source)
        config = _settings()
        root = Path(output_root or config.get("material_root") or "")
        if not root.is_dir():
            raise SmartSplitError("当前素材目录不可用，请选择输出目录")
        output_dir = root / "SmartSplit" / _safe_stem(source.stem)
        output_dir.mkdir(parents=True, exist_ok=True)
        test_file = output_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        selected_all = [item for item in job["segments"] if int(item.get("selected") or 0)]
        completed = [
            item for item in selected_all
            if str(item.get("status") or "") == "completed"
            and Path(str(item.get("output_path") or "")).is_file()
        ]
        # A cancelled export is resumable.  Keep already verified clips and only
        # render missing/failed clips instead of creating duplicate files.
        selected = [item for item in selected_all if item not in completed]
        total_duration = sum(float(item["duration"]) for item in selected)
        if selected:
            estimated_bytes = int(max(100 * 1024 * 1024, int(job["source_size"]) * total_duration / max(float(job["source_duration"]), 0.1) * 1.5))
            if shutil.disk_usage(output_dir).free < estimated_bytes:
                raise SmartSplitError("磁盘空间不足")
        ffmpeg = _configured_executable("ffmpeg")
        exported, failed = len(completed), 0
        _update_job(
            job_id,
            exported_count=exported,
            failed_count=0,
            progress=int(exported / max(len(selected_all), 1) * 100),
        )
        for output_index, segment in enumerate(selected, 1):
            if _cancelled(job_id):
                raise SmartSplitError("任务已取消")
            output = _unique_output(output_dir, _safe_stem(source.stem), int(segment["segment_index"]))
            partial = output.with_name(output.stem + ".partial.mp4")
            partial.unlink(missing_ok=True)
            duration = float(segment["end_time"]) - float(segment["start_time"])
            completed_before = exported
            _update_job(
                job_id,
                stage=f"正在生成 {output.name}",
                progress=int(completed_before / max(len(selected_all), 1) * 100),
                output_dir=str(output_dir),
            )
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{float(segment['start_time']):.6f}", "-i", str(source),
                "-t", f"{duration:.6f}", "-map", "0:v:0",
            ]
            if int(job["has_audio"]):
                command += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]
            else:
                command += ["-an"]
            command += _export_video_options(int(job.get("source_width") or 0), int(job.get("source_height") or 0))
            command += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(partial)]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       **background_process_kwargs(command, "smart_split_export"))
            _set_process(job_id, process)
            _, stderr = process.communicate()
            _set_process(job_id, None)
            if _cancelled(job_id):
                partial.unlink(missing_ok=True)
                raise SmartSplitError("任务已取消")
            if process.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
                partial.unlink(missing_ok=True)
                failed += 1
                with connect() as conn:
                    conn.execute("UPDATE smart_split_segments SET status='failed',error_message=?,updated_at=? WHERE id=?", ("该镜头导出失败", now(), int(segment["id"])))
                continue
            os.replace(partial, output)
            try:
                exported_probe = probe_video(output)
                if abs(float(exported_probe["duration"]) - duration) > 0.35:
                    raise SmartSplitError("导出镜头时长校验失败")
                if int(job["has_audio"]) and not exported_probe["has_audio"]:
                    raise SmartSplitError("导出镜头缺少音频")
            except Exception as exc:
                output.unlink(missing_ok=True)
                failed += 1
                with connect() as conn:
                    conn.execute("UPDATE smart_split_segments SET status='failed',error_message=?,updated_at=? WHERE id=?", (_safe_error(exc), now(), int(segment["id"])))
                continue
            exported += 1
            with connect() as conn:
                conn.execute("UPDATE smart_split_segments SET output_path=?,status='completed',error_message='',updated_at=? WHERE id=?", (str(output), now(), int(segment["id"])))
            processed = exported + failed
            _update_job(
                job_id,
                exported_count=exported,
                failed_count=failed,
                progress=int(processed / max(len(selected_all), 1) * 100),
            )
        final_status = "completed" if exported > 0 else "failed"
        _update_job(job_id, status=final_status, stage="分割完成" if exported else "分割失败", progress=100, output_dir=str(output_dir), exported_count=exported, failed_count=failed, finished_at=now(), error_message="" if exported else "没有成功生成任何镜头")
    except Exception as exc:
        cancelled = _cancelled(job_id) or str(exc) == "任务已取消"
        _update_job(job_id, status="cancelled" if cancelled else "failed", stage="", error_message="" if cancelled else _safe_error(exc), finished_at=now())
    finally:
        _set_process(job_id, None)
        with _STATE_LOCK:
            _CANCEL_EVENTS.pop(job_id, None)
        _WORKER_LOCK.release()


def cancel_job(job_id: int) -> dict:
    _update_job(job_id, cancel_requested=1)
    with _STATE_LOCK:
        event = _CANCEL_EVENTS.setdefault(int(job_id), threading.Event())
        event.set()
        process = _PROCESSES.get(int(job_id))
    if process and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    return get_job(job_id)


def open_output_directory(job_id: int) -> dict:
    job = get_job(job_id)
    output = Path(str(job.get("output_dir") or "")).resolve()
    log_path = LOG_DIR / "smart-split-open-folder.log"

    def log_open(result: str, exception: str = "") -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "event": "open_folder_called", "job_id": int(job_id),
                "folder_path": str(output), "result": result,
                "exception": str(exception or ""), "timestamp": now(),
            }
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    if not output.is_dir():
        log_open("failed", "输出目录不存在")
        raise SmartSplitError("输出目录不存在")
    command = ["explorer.exe", str(output)] if os.name == "nt" else ["xdg-open", str(output)]
    try:
        subprocess.Popen(
            command, close_fds=True,
            **background_process_kwargs(command, "open_split_output"),
        )
    except OSError as exc:
        log_open("failed", f"{type(exc).__name__}: {exc}")
        raise SmartSplitError(f"无法打开输出目录：{exc}") from exc
    log_open("opened")
    return {"opened": True, "path": str(output)}


def recover_smart_split_jobs() -> int:
    """Make interrupted standalone jobs readable without auto-restarting FFmpeg."""
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE smart_split_jobs SET status='failed',stage='',error_message='上次任务被软件退出中断',
               updated_at=?,finished_at=? WHERE status IN ('pending','detecting','exporting')""",
            (now(), now()),
        )
        return int(cursor.rowcount)
