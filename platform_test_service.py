"""Administrator-only real publication acceptance runs and evidence reports."""

from __future__ import annotations

import json
import os
import threading
import traceback
import re
import time
from pathlib import Path
from datetime import datetime

from db import DATA_DIR, connect, now
from publishers import get_publisher
from publishers.youtube import verify_youtube_account


CAPABILITY_STATUSES = {"not_implemented", "untested", "testing", "passed", "failed", "limited"}
REPORT_ROOT = DATA_DIR / "platform_test_reports"
REPORT_PLATFORM_CODES = (
    "douyin", "youtube", "tiktok", "instagram", "facebook",
    "xiaohongshu", "kuaishou", "wechat_channels", "bilibili", "x",
)

FIELD_SUPPORT = {
    "youtube": {"title": True, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": True},
    "tiktok": {"title": False, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": False},
    "instagram": {"title": False, "description": True, "tags": True, "cover": True, "visibility": False, "schedule": False},
    "facebook": {"title": True, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": True},
    "douyin": {"title": True, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": True},
    "xiaohongshu": {"title": True, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": True},
    "kuaishou": {"title": True, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": True},
    "wechat_channels": {"title": True, "description": True, "tags": True, "cover": True, "visibility": True, "schedule": True},
}


def ensure_report_dirs() -> Path:
    """Create stable per-platform evidence directories without inventing reports."""
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    for code in REPORT_PLATFORM_CODES:
        (REPORT_ROOT / code).mkdir(parents=True, exist_ok=True)
    return REPORT_ROOT


def _sanitize_report(value):
    sensitive_keys = {"access_token", "refresh_token", "client_secret", "access_token_encrypted",
                      "refresh_token_encrypted", "client_secret_encrypted", "authorization"}
    if isinstance(value, dict):
        return {key: ("***" if str(key).lower() in sensitive_keys else _sanitize_report(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_report(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"Bearer\s+[A-Za-z0-9._~-]+", "Bearer ***", value, flags=re.I)
        value = re.sub(r"ya29\.[A-Za-z0-9._~-]+", "ya29.***", value)
    return value


def is_admin_mode() -> bool:
    if str(os.getenv("MIXCUT_ADMIN_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key='admin_mode'").fetchone()
        return bool(row and str(row["value"]).strip().lower() in {"1", "true", "yes", "on"})
    except Exception:
        return False


def _require_admin():
    if not is_admin_mode():
        raise PermissionError("平台发布测试仅管理员模式可用")


def platform_support(platform_code: str) -> dict:
    support = FIELD_SUPPORT.get(platform_code, {})
    labels = {"title": "标题", "description": "简介", "tags": "话题标签", "cover": "封面", "visibility": "可见性", "schedule": "定时发布"}
    return {key: {"supported": bool(support.get(key)), "label": label,
                  "message": "支持" if support.get(key) else "不支持"} for key, label in labels.items()}


def list_test_runs(limit=100):
    ensure_report_dirs()
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*,p.code platform_code,p.name platform_name,a.account_name
               FROM platform_test_runs r JOIN platforms p ON p.id=r.platform_id
               JOIN publish_accounts a ON a.id=r.account_id ORDER BY r.id DESC LIMIT ?""", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def create_platform_test(data: dict) -> int:
    _require_admin()
    ensure_report_dirs()
    platform_id = int(data.get("platform_id") or 0)
    account_id = int(data.get("account_id") or 0)
    video_path = Path(str(data.get("video_path") or ""))
    if not video_path.is_file():
        raise ValueError("请选择存在的测试视频")
    with connect() as conn:
        account = conn.execute(
            """SELECT a.id,a.platform_id,p.code FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id
               WHERE a.id=? AND a.platform_id=? AND a.status='active'""", (account_id, platform_id)
        ).fetchone()
        if not account:
            raise ValueError("测试账号与平台不匹配")
        if account["code"] == "youtube":
            data = dict(data)
            data.update({
                "title": "MixcutAI YouTube 发布测试 " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "description": "这是MixcutAI平台发布功能的私密验收视频。",
                "tags": ["MixcutAI", "upload-test", "private-test"],
                "visibility": "private",
                "test_mode": "private",
                "scheduled_at": "",
            })
        stamp = now()
        cursor = conn.execute(
            """INSERT INTO platform_test_runs(platform_id,account_id,video_path,title,description,tags_json,
               cover_path,visibility,test_mode,scheduled_at,status,current_step,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'pending','等待执行',?,?)""",
            (platform_id, account_id, str(video_path), str(data.get("title") or "").strip(),
             str(data.get("description") or "").strip(), json.dumps(data.get("tags") or [], ensure_ascii=False),
             str(data.get("cover_path") or ""), str(data.get("visibility") or "private"),
             str(data.get("test_mode") or "private"), str(data.get("scheduled_at") or ""), stamp, stamp),
        )
        run_id = int(cursor.lastrowid)
    confirmations = data.get("confirmations") or {}
    threading.Thread(target=_execute_test, args=(run_id, confirmations), daemon=True, name=f"platform-test-{run_id}").start()
    return run_id


def retry_platform_test(run_id: int) -> int:
    _require_admin()
    with connect() as conn:
        row = conn.execute("SELECT * FROM platform_test_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("测试记录不存在")
        conn.execute(
            "UPDATE platform_test_runs SET status='pending',current_step='等待重试',error_message='',retry_count=retry_count+1,updated_at=? WHERE id=?",
            (now(), run_id),
        )
    threading.Thread(target=_execute_test, args=(run_id, {}), daemon=True, name=f"platform-retry-{run_id}").start()
    return run_id


def _execute_test(run_id: int, confirmations: dict):
    steps = []
    result = {}
    artifact_dir = None
    step_clock = time.monotonic()
    try:
        with connect() as conn:
            run = conn.execute("SELECT * FROM platform_test_runs WHERE id=?", (run_id,)).fetchone()
            account = conn.execute(
                """SELECT a.*,p.code platform_code,p.name platform_name,p.integration_type,p.capability_status
                   FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id WHERE a.id=?""", (run["account_id"],)
            ).fetchone()
            platform = conn.execute("SELECT * FROM platforms WHERE id=?", (run["platform_id"],)).fetchone()
        run, account, platform = dict(run), dict(account), dict(platform)
        artifact_dir = REPORT_ROOT / platform["code"] / f"run_{run_id}_{now().replace(':','').replace(' ','_')}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with connect() as conn:
            conn.execute("UPDATE platforms SET capability_status='testing',capability_status_reason='真实发布测试执行中',updated_at=? WHERE id=?", (now(), platform["id"]))
            conn.execute("UPDATE platform_test_runs SET status='testing',current_step='检查账号状态',started_at=?,report_path=?,updated_at=? WHERE id=?", (now(), str(artifact_dir / "report.json"), now(), run_id))

        def add(name, status, detail="", evidence=""):
            nonlocal step_clock
            current = time.monotonic()
            steps.append({"name": name, "status": status, "detail": detail, "evidence": evidence,
                          "time": now(), "elapsed_seconds": round(current - step_clock, 3)})
            step_clock = current

        api_mode = platform["integration_type"] == "api"
        account_ok = account.get("authorization_status") == "authorized" if api_mode else account.get("login_status") == "logged_in"
        add("账号授权或登录", "passed" if account_ok else "failed", "OAuth 已授权" if api_mode else "浏览器会话已登录")
        if not account_ok:
            raise RuntimeError("账号授权已过期，请重新登录授权" if api_mode else "浏览器登录已失效，请重新扫码登录")
        if platform["code"] == "youtube":
            video_path = Path(run["video_path"])
            if video_path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mpeg", ".mpg"}:
                raise RuntimeError("YouTube测试视频格式不受支持，请使用MP4、MOV、WebM或常见视频格式")
            cover_path = Path(str(run.get("cover_path") or ""))
            if not cover_path.is_file():
                raise RuntimeError("YouTube真实验收必须选择有效封面文件")
            if cover_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                raise RuntimeError("YouTube测试封面只支持JPG或PNG")
            if cover_path.stat().st_size > 2 * 1024 * 1024:
                raise RuntimeError("YouTube测试封面不能超过2MB")
            preflight = verify_youtube_account(account)
            add("OAuth Client配置", "passed", "Client ID与Client Secret已加密配置")
            add("Refresh Token", "passed", "Refresh Token存在且可读取")
            add("YouTube API网络", "passed", "channels.list(mine=true)访问成功")
            add("目标频道", "passed", f"{preflight.get('channel_title')} · {preflight.get('channel_id')}")
            add("测试可见性", "passed", "private")
        persistence_ok = bool(confirmations.get("restart_persistence"))
        add("重启后登录状态", "passed" if persistence_ok else "not_run", "管理员已确认重启读取" if persistence_ok else "尚未执行重启验证")
        add("测试视频文件", "passed", run["video_path"])
        support = platform_support(platform["code"])
        for field, info in support.items():
            if not info["supported"]:
                add(info["label"], "unsupported", "该平台明确不支持此字段")

        with connect() as conn:
            conn.execute("UPDATE platform_test_runs SET current_step='上传并等待平台处理',updated_at=? WHERE id=?", (now(), run_id))

        def progress(percent):
            with connect() as conn:
                conn.execute("UPDATE platform_test_runs SET current_step=?,updated_at=? WHERE id=?", (f"视频上传 {percent}%", now(), run_id))

        metadata = {
            "title": run["title"], "description": run["description"],
            "tags": json.loads(run["tags_json"] or "[]"), "cover_path": run["cover_path"],
            "visibility": run["visibility"], "schedule": run["scheduled_at"],
            "test_mode": run["test_mode"], "artifact_dir": str(artifact_dir), "progress_callback": progress,
        }
        result = get_publisher(platform["code"]).publish(run["video_path"], account, metadata) or {}
        if result.get("verified") is not True or not result.get("id") or not result.get("url"):
            raise RuntimeError("平台未返回可验证的作品ID和作品链接")
        add("视频上传", "passed", "平台已接收完整视频")
        add("平台处理", "passed", str(result.get("processing_status") or result.get("signal") or "已完成"))
        add("作品ID", "passed", str(result["id"]))
        add("作品链接", "passed", str(result["url"]))
        if platform["code"] == "youtube":
            refreshed = verify_youtube_account(account, force_refresh=True)
            result["forced_token_refresh"] = refreshed
            add("Token过期自动刷新", "passed", "已强制使用Refresh Token换取新Access Token并重新读取频道")
        for field, field_result in (result.get("field_results") or {}).items():
            passed = field_result.get("passed") if isinstance(field_result, dict) else bool(field_result)
            add(field, "passed" if passed else "failed", str(field_result.get("message") if isinstance(field_result, dict) else "回读校验"))
        retry_ok = bool(confirmations.get("network_retry"))
        token_ok = bool(confirmations.get("token_expiry"))
        add("网络中断重试", "passed" if retry_ok else "not_run", "已验证断点续传与重试" if retry_ok else "尚未执行网络中断验证")
        add("Token或Cookie过期", "passed" if token_ok else "not_run", "已验证过期后重新登录提示" if token_ok else "尚未执行失效验证")

        required_complete = all(item["status"] in {"passed", "unsupported"} for item in steps)
        final_status = "passed" if required_complete else "limited"
        conclusion = "全部必测项目通过" if final_status == "passed" else "真实发布成功，但仍有必测项目未执行"
        with connect() as conn:
            conn.execute(
                "UPDATE platforms SET capability_status=?,capability_status_reason=?,last_tested_at=?,updated_at=? WHERE id=?",
                (final_status, conclusion, now(), now(), platform["id"]),
            )
            conn.execute(
                """UPDATE platform_test_runs SET status=?,current_step='测试完成',remote_post_id=?,remote_url=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (final_status, str(result["id"]), str(result["url"]), now(), now(), run_id),
            )
    except Exception as exc:
        error = str(exc)
        result["api_error_original"] = error
        if artifact_dir is None:
            artifact_dir = REPORT_ROOT / "unknown" / f"run_{run_id}"
            artifact_dir.mkdir(parents=True, exist_ok=True)
        add = lambda name, status, detail="", evidence="": steps.append({
            "name": name, "status": status, "detail": detail, "evidence": evidence,
            "time": now(), "elapsed_seconds": round(time.monotonic() - step_clock, 3),
        })
        add("失败原因", "failed", error)
        with connect() as conn:
            run = conn.execute("SELECT platform_id FROM platform_test_runs WHERE id=?", (run_id,)).fetchone()
            if run:
                conn.execute("UPDATE platforms SET capability_status='failed',capability_status_reason=?,last_tested_at=?,updated_at=? WHERE id=?", (error[:500], now(), now(), run["platform_id"]))
            conn.execute("UPDATE platform_test_runs SET status='failed',current_step='测试失败',error_message=?,finished_at=?,updated_at=? WHERE id=?", (error[:1000], now(), now(), run_id))
        result["traceback"] = traceback.format_exc()
    finally:
        with connect() as conn:
            run = conn.execute(
                """SELECT r.*,p.code platform_code,p.name platform_name,p.integration_type,a.account_name
                   FROM platform_test_runs r JOIN platforms p ON p.id=r.platform_id
                   JOIN publish_accounts a ON a.id=r.account_id WHERE r.id=?""", (run_id,)
            ).fetchone()
        if run:
            report = {
                "platform": run["platform_name"], "platform_code": run["platform_code"],
                "publishing_method": run["integration_type"], "software_version": "1.3.6",
                "test_account": run["account_name"], "test_time": run["started_at"] or run["created_at"],
                "test_video": run["video_path"], "visibility": run["visibility"],
                "steps": steps, "work_id": result.get("id") or run["remote_post_id"],
                "work_url": result.get("url") or run["remote_url"], "error": run["error_message"],
                "screenshots": result.get("screenshots") or [], "artifacts": result.get("artifacts") or {},
                "channel_id": result.get("channel_id") or "",
                "processing_status": result.get("processing_status") or "",
                "privacy_status": result.get("privacy_status") or "",
                "token_refresh_result": result.get("forced_token_refresh") or result.get("token_refresh") or {},
                "network_retry_result": result.get("network_retry") or {},
                "step_timings": result.get("timings") or {},
                "api_error_original": result.get("api_error_original") or "",
                "field_support": platform_support(run["platform_code"]), "conclusion": run["status"],
            }
            report_path = Path(run["report_path"] or (artifact_dir / "report.json"))
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(_sanitize_report(report), ensure_ascii=False, indent=2), encoding="utf-8")
            with connect() as conn:
                conn.execute("UPDATE platform_test_runs SET report_path=?,updated_at=? WHERE id=?", (str(report_path), now(), run_id))
