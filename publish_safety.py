"""Fail-closed publication guards for unverified domestic platforms.

This module intentionally excludes Douyin.  Its validated publisher and
browser profile flow remain independent and unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from db import connect
from runtime_paths import BUNDLED_FFMPEG_EXE, BUNDLED_FFPROBE_EXE
from subprocess_utils import background_process_kwargs


def find_executable(name: str, configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    bundled = BUNDLED_FFPROBE_EXE if name.lower().startswith("ffprobe") else BUNDLED_FFMPEG_EXE
    if bundled.is_file():
        return str(bundled)
    return shutil.which(name)


def quick_hash(path: Path) -> str:
    digest = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            stream.seek(max(0, size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


GUARDED_DOMESTIC_PLATFORMS = {"kuaishou", "xiaohongshu", "wechat_channels", "bilibili"}
PUBLISH_STATUSES = {
    "draft", "prechecking", "ready", "uploading", "submitting", "processing",
    "waiting_confirmation", "succeeded", "failed", "needs_login",
    "needs_manual_action", "cancelled",
}
ACTIVE_STATUSES = {"prechecking", "ready", "uploading", "submitting", "processing", "waiting_confirmation"}

UPLOAD_URLS = {
    "kuaishou": "https://cp.kuaishou.com/article/publish/video",
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish",
    "wechat_channels": "https://channels.weixin.qq.com/platform/post/create",
    "bilibili": "https://member.bilibili.com/platform/upload/video/frame",
}
PLATFORM_NAMES = {"kuaishou": "快手", "xiaohongshu": "小红书", "wechat_channels": "视频号", "bilibili": "B站"}
LIMITS = {
    "kuaishou": {"title": 50, "description": 1000},
    "xiaohongshu": {"title": 20, "description": 1000},
    "wechat_channels": {"title": 30, "description": 1000},
    "bilibili": {"title": 80, "description": 2000},
}

ERROR_DEFINITIONS = {
    "AUTH_EXPIRED": ("登录状态已失效", "请重新登录平台账号后再发布", True, True, False),
    "ACCOUNT_NOT_FOUND": ("发布账号不存在", "所选账号已删除、停用或与平台不匹配", False, False, True),
    "PLATFORM_UNTESTED": ("平台尚未完成真实发布测试", "普通用户暂时不能使用该平台发布，仅管理员可执行验收", False, False, True),
    "FILE_NOT_FOUND": ("视频文件不存在", "请检查输出目录或重新生成视频", False, False, False),
    "FILE_UNREADABLE": ("视频文件无法读取", "文件可能被占用或当前用户没有读取权限", True, False, False),
    "VIDEO_INVALID": ("视频文件损坏或无法解析", "FFprobe未检测到有效的视频流、时长或画面尺寸", False, False, False),
    "VIDEO_TOO_LARGE": ("视频文件过大", "视频超过当前平台安全预检上限，请压缩后重试", False, False, False),
    "TITLE_TOO_LONG": ("发布标题不符合要求", "标题为空或超过当前平台允许长度", False, False, False),
    "COVER_INVALID": ("封面文件无效", "封面不存在、不可读取或不是有效图片", False, False, False),
    "NETWORK_TIMEOUT": ("网络连接失败", "无法连接平台创作中心，请检查网络后重试", True, False, False),
    "RATE_LIMITED": ("平台请求频率受限", "操作过于频繁，请稍后重试", True, False, False),
    "UPLOAD_FAILED": ("视频上传失败", "平台未接收完整视频，请检查网络后重试", True, False, False),
    "SUBMIT_FAILED": ("发布提交失败", "平台拒绝提交或必填项尚未完成", True, False, True),
    "PROCESSING_FAILED": ("平台处理失败", "上传后平台转码或审核前处理失败", True, False, True),
    "PAGE_CHANGED": ("当前平台页面可能已经更新", "关键页面元素缺失，已停止操作，请更新发布适配器", False, False, True),
    "CAPTCHA_REQUIRED": ("需要完成平台安全验证", "请在浏览器中完成验证码或扫码后继续", True, False, True),
    "MANUAL_CONFIRM_REQUIRED": ("需要人工确认", "平台要求手机确认、扫码或人工选择必填项", True, False, True),
    "DUPLICATE_TASK": ("该视频正在发布", "检测到相同账号、视频、标题和发布时间的任务，请勿重复操作", False, False, False),
    "BROWSER_CRASHED": ("发布浏览器异常关闭", "浏览器被关闭或崩溃，本次发布已安全停止", True, False, True),
    "UNKNOWN_ERROR": ("发布操作失败", "发生未识别错误，已停止发布并保留技术日志编号", True, False, True),
}


@dataclass
class PublishGuardError(RuntimeError):
    code: str
    detail: str = ""
    log_id: str = ""

    def __post_init__(self):
        if self.code not in ERROR_DEFINITIONS:
            self.code = "UNKNOWN_ERROR"
        if not self.log_id:
            self.log_id = "PUB-" + uuid.uuid4().hex[:10].upper()
        RuntimeError.__init__(self, self.user_message)

    @property
    def definition(self):
        return ERROR_DEFINITIONS[self.code]

    @property
    def user_message(self) -> str:
        title, explanation, *_ = self.definition
        message = self.detail or explanation
        return f"{title}：{message}（日志编号 {self.log_id}）"

    def to_dict(self) -> dict:
        title, explanation, retryable, needs_login, manual = self.definition
        return {
            "code": self.code, "title": title, "message": self.detail or explanation,
            "retryable": retryable, "needs_login": needs_login,
            "needs_manual_action": manual, "technical_log_id": self.log_id,
        }


def guarded_error(code: str, detail: str = "") -> PublishGuardError:
    return PublishGuardError(code, detail)


def map_publish_exception(exc: Exception) -> PublishGuardError:
    if isinstance(exc, PublishGuardError):
        return exc
    text = str(exc or "")
    lowered = text.lower()
    if any(word in lowered for word in ("401", "unauthorized", "login expired", "登录过期", "登录失效", "cookie expired")):
        return guarded_error("AUTH_EXPIRED")
    if any(word in lowered for word in ("captcha", "验证码", "扫码")):
        return guarded_error("CAPTCHA_REQUIRED")
    if any(word in lowered for word in ("phone confirm", "manual confirm", "手机确认", "人工确认")):
        return guarded_error("MANUAL_CONFIRM_REQUIRED")
    if any(word in lowered for word in ("429", "rate limit", "too many requests", "频率受限")):
        return guarded_error("RATE_LIMITED")
    if any(word in lowered for word in ("upload failed", "上传失败")):
        return guarded_error("UPLOAD_FAILED")
    if any(word in lowered for word in ("submit failed", "提交失败", "publish rejected")):
        return guarded_error("SUBMIT_FAILED")
    if any(word in lowered for word in ("processing failed", "transcode failed", "处理失败", "转码失败")):
        return guarded_error("PROCESSING_FAILED")
    if any(word in lowered for word in ("timeout", "timed out", "网络")):
        return guarded_error("NETWORK_TIMEOUT")
    if any(word in lowered for word in ("target closed", "browser has been closed", "crash")):
        return guarded_error("BROWSER_CRASHED")
    if any(word in lowered for word in ("locator", "selector", "element", "页面元素")):
        return guarded_error("PAGE_CHANGED")
    return guarded_error("UNKNOWN_ERROR")


def title_hash(title: str) -> str:
    normalized = re.sub(r"\s+", " ", str(title or "").strip()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def idempotency_key(platform: str, account_id: int, video_hash: str, title_digest: str, scheduled_time: str) -> str:
    raw = "\n".join((platform, str(account_id), video_hash, title_digest, scheduled_time or "immediate"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _probe_video(path: Path, configured_ffprobe: str = "") -> dict:
    ffprobe = find_executable("ffprobe", configured_ffprobe)
    if not ffprobe:
        raise guarded_error("VIDEO_INVALID", "未找到FFprobe，请先在系统设置中配置")
    command = [
        ffprobe, "-v", "error", "-show_entries",
        "format=duration,format_name,size:stream=index,codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                 encoding="utf-8", errors="replace", **background_process_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise guarded_error("VIDEO_INVALID", "FFprobe执行失败") from exc
    if process.returncode != 0:
        raise guarded_error("VIDEO_INVALID")
    try:
        data = json.loads(process.stdout or "{}")
        streams = data.get("streams") or []
        video = next(item for item in streams if item.get("codec_type") == "video")
        fmt = data.get("format") or {}
        result = {
            "duration": float(fmt.get("duration") or 0), "size": int(fmt.get("size") or path.stat().st_size),
            "format": str(fmt.get("format_name") or ""), "codec": str(video.get("codec_name") or ""),
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
        }
    except (StopIteration, TypeError, ValueError, KeyError) as exc:
        raise guarded_error("VIDEO_INVALID") from exc
    if result["duration"] <= 0 or result["width"] <= 0 or result["height"] <= 0 or not result["codec"] or not result["format"]:
        raise guarded_error("VIDEO_INVALID")
    ratio = result["width"] / result["height"]
    if not 0.35 <= ratio <= 2.85:
        raise guarded_error("VIDEO_INVALID", "视频画面比例异常")
    if result["size"] > 20 * 1024 * 1024 * 1024:
        raise guarded_error("VIDEO_TOO_LARGE")
    return result


def _validate_cover(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise guarded_error("COVER_INVALID")
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise guarded_error("COVER_INVALID") from exc
    return str(path.resolve())


def _validate_schedule(value: str) -> str:
    value = str(value or "").strip().replace("T", " ")
    if not value:
        return ""
    try:
        scheduled = datetime.fromisoformat(value)
    except ValueError as exc:
        raise guarded_error("SUBMIT_FAILED", "定时发布时间格式无效") from exc
    if scheduled <= datetime.now():
        raise guarded_error("SUBMIT_FAILED", "定时发布时间必须晚于当前时间")
    return scheduled.strftime("%Y-%m-%d %H:%M:%S")


def default_network_check(url: str, timeout: float = 4.0) -> None:
    host = urlparse(url).hostname
    if not host:
        raise guarded_error("NETWORK_TIMEOUT")
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return
    except OSError as exc:
        raise guarded_error("NETWORK_TIMEOUT") from exc


def preflight_publish_check(
    platform: dict,
    account: dict,
    video_path: str,
    metadata: dict,
    *,
    allow_unverified: bool = False,
    check_network: bool = True,
    network_checker=default_network_check,
    check_duplicate: bool = True,
) -> dict:
    code = str(platform.get("code") or platform.get("platform_code") or "")
    if code not in GUARDED_DOMESTIC_PLATFORMS:
        raise ValueError("该预检仅用于快手、小红书、视频号和B站")
    name = PLATFORM_NAMES[code]
    if not allow_unverified and platform.get("capability_status") != "passed":
        raise guarded_error("PLATFORM_UNTESTED", f"{name}尚未完成真实发布验收")
    if not account or int(account.get("id") or 0) <= 0 or str(account.get("status") or "active") != "active":
        raise guarded_error("ACCOUNT_NOT_FOUND", f"{name}发布账号不存在")
    if int(account.get("platform_id") or 0) != int(platform.get("id") or account.get("platform_id") or 0):
        raise guarded_error("ACCOUNT_NOT_FOUND", "账号与平台不匹配")
    if str(account.get("login_status") or "") != "logged_in":
        raise guarded_error("AUTH_EXPIRED", f"{name}账号尚未登录")
    profile = Path(str(account.get("browser_profile_path") or "")).expanduser()
    if not profile.is_dir():
        raise guarded_error("AUTH_EXPIRED", "浏览器登录环境不存在，请重新登录")
    path = Path(str(video_path or "")).expanduser()
    if not path.is_file():
        raise guarded_error("FILE_NOT_FOUND")
    if not os.access(path, os.R_OK):
        raise guarded_error("FILE_UNREADABLE")
    try:
        with path.open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        raise guarded_error("FILE_UNREADABLE") from exc
    with connect() as conn:
        ffprobe_row = conn.execute("SELECT value FROM app_settings WHERE key='ffprobe_path'").fetchone()
    video_info = _probe_video(path, str(ffprobe_row["value"] if ffprobe_row else ""))
    limits = LIMITS[code]
    title = str(metadata.get("title") or "").strip()
    if not title or len(title) > limits["title"]:
        raise guarded_error("TITLE_TOO_LONG", f"{name}标题应为1至{limits['title']}个字符")
    description = str(metadata.get("description") or "").strip()
    if len(description) > limits["description"]:
        raise guarded_error("SUBMIT_FAILED", f"{name}简介不能超过{limits['description']}个字符")
    tags = metadata.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 30 or any(not re.fullmatch(r"[^#\s]{1,50}", str(tag).lstrip("#")) for tag in tags):
        raise guarded_error("SUBMIT_FAILED", "话题格式不正确，请使用不含空格的独立话题")
    cover = _validate_cover(metadata.get("cover_path") or "")
    scheduled = _validate_schedule(metadata.get("schedule") or metadata.get("scheduled_at") or "")
    if scheduled:
        raise guarded_error("SUBMIT_FAILED", f"{name}定时发布尚未完成真实验收，当前版本不允许静默忽略该设置")
    if str(metadata.get("visibility") or "").strip():
        raise guarded_error("MANUAL_CONFIRM_REQUIRED", f"{name}可见范围控件尚未完成真实验收，请先更新适配器")
    from publishers import get_publisher
    try:
        publisher = get_publisher(code)
    except Exception as exc:
        raise guarded_error("PAGE_CHANGED", "平台发布适配器无法加载") from exc
    if getattr(publisher, "platform_code", "") != code:
        raise guarded_error("PAGE_CHANGED", "平台发布适配器加载错误")
    if check_network:
        network_checker(UPLOAD_URLS[code])
    video_digest = quick_hash(path)
    title_digest = title_hash(title)
    key = idempotency_key(code, int(account["id"]), video_digest, title_digest, scheduled)
    if check_duplicate:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with connect() as conn:
            duplicate = conn.execute(
                f"SELECT id FROM publish_channels WHERE idempotency_key=? AND status IN ({marks}) LIMIT 1",
                (key, *sorted(ACTIVE_STATUSES)),
            ).fetchone()
        if duplicate:
            raise guarded_error("DUPLICATE_TASK")
    return {
        "platform": code, "account_id": int(account["id"]), "video_path": str(path.resolve()),
        "video_file_hash": video_digest, "title_hash": title_digest, "idempotency_key": key,
        "scheduled_time": scheduled, "cover_path": cover, "video": video_info,
        "adapter": publisher.__class__.__name__, "status": "ready",
    }


def status_for_error(error: PublishGuardError) -> str:
    if error.code in {"AUTH_EXPIRED"}:
        return "needs_login"
    if error.code in {"CAPTCHA_REQUIRED", "MANUAL_CONFIRM_REQUIRED", "PAGE_CHANGED", "BROWSER_CRASHED", "DUPLICATE_TASK"}:
        return "needs_manual_action"
    return "failed"


def recover_guarded_channels() -> int:
    """Never blindly resubmit an interrupted domestic browser publication."""
    interrupted = {"prechecking", "uploading", "submitting", "processing", "waiting_confirmation"}
    marks = ",".join("?" for _ in interrupted)
    guarded = ",".join("?" for _ in GUARDED_DOMESTIC_PLATFORMS)
    message = "软件上次退出时平台结果未确认，请先在平台后台核对作品"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT c.id,c.publish_task_id FROM publish_channels c JOIN platforms p ON p.id=c.platform_id
                WHERE p.code IN ({guarded}) AND c.status IN ({marks})""",
            (*sorted(GUARDED_DOMESTIC_PLATFORMS), *sorted(interrupted)),
        ).fetchall()
        for item in rows:
            log_id = "PUB-" + uuid.uuid4().hex[:10].upper()
            conn.execute(
                "UPDATE publish_channels SET status='needs_manual_action',error_code='MANUAL_CONFIRM_REQUIRED',error_message=?,technical_log_id=?,updated_at=datetime('now','localtime') WHERE id=?",
                (message, log_id, item["id"]),
            )
            conn.execute(
                "UPDATE publish_tasks SET status='needs_manual_action',last_error=?,updated_at=datetime('now','localtime') WHERE id=?",
                (message, item["publish_task_id"]),
            )
    return len(rows)
