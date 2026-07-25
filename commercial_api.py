from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import sqlite3
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from db import APP_DIR, DB_PATH, THUMB_DIR, VOICE_DIR, connect, init_db, now
from bgm_service import scan_bgm_folder
from sfx_service import scan_sfx_folder
from subtitle_service import generate_ass_for_shot, generate_ass_from_segments, split_text, subtitle_signature
from tts_service import audio_duration, synthesize_windows_tts
from video_engine import find_ffmpeg, generate_video, title_config_from_settings
from timeline_engine import ffmpeg_filter_path, render_title_ass
from video_scan import make_thumbnail, probe_video, quick_hash, scan_video_files
from ai_services import concatenate_tts_wavs, chat_completion, synthesize_tts, test_ai_service, test_tts_service
from runtime_paths import RESOURCE_DIR
from subprocess_utils import background_process_kwargs
from batch_service import BATCH_QUEUE, create_batch_task, recover_and_enqueue
from update_service import UpdateError, UpdateManifest, check_for_update, configured_manifest_url, current_version, stage_and_launch
from browser_profile_service import (
    detect_login_status,
    ensure_profile,
    open_profile_folder,
    profile_path,
    publishing_capabilities,
)
from oauth_service import authorization_status, cancel_authorization, complete_authorization, start_authorization
from oauth_token_store import protect
from platforms import adapter_catalog, get_adapter
from publishers import publisher_catalog
from publishers.youtube import verify_youtube_account
from publish_queue import PUBLISH_WORKER, create_publish_task, delete_publish_task, enqueue_publish_task, start_publish_worker, sync_missing_publish_videos
from ai_publish_service import generate_publish_content
from cover_service import normalize_cover_path
from script_segmentation import AI_SEGMENTATION_INSTRUCTION, smart_segment_text
from language_service import LANGUAGE_VOICE_PREFIXES, contains_cjk, detect_text_language, voice_matches_language
from voice_clone_service import clone_minimax_voice
from platform_test_service import create_platform_test, is_admin_mode, list_test_runs, platform_support, retry_platform_test


MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".wav", ".mp3", ".m4a", ".ass", ".srt"}


def rows(sql: str, args=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(sql: str, args=()):
    with connect() as conn:
        value = conn.execute(sql, args).fetchone()
        return dict(value) if value else None


def shot_script_text(shot: dict | None) -> str:
    """Return the exact text used for TTS/video generation."""
    shot = shot or {}
    return smart_segment_text(shot.get("ai_text") or shot.get("original_text") or "")


def normalize_rewrite_text(text: str) -> str:
    """Apply the single source of truth for rewrite, TTS and subtitle cuts."""
    return smart_segment_text(text)


def script_hash(text: str) -> str:
    text = str(text or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def voice_script_hash(text: str) -> str:
    return script_hash("space-segmented-tts-v1\n" + str(text or "").strip())


def voice_render_hash(text: str, config: dict, voice: dict, speed: float, segment_gap_ms: int = 60) -> str:
    payload = {
        "version": "voice-render-v2",
        "text": str(text or "").strip(),
        "service_id": voice.get("service_id"),
        "voice_id": voice.get("id"),
        "voice_code": voice.get("voice_code"),
        "provider": config.get("provider"),
        "model": config.get("model"),
        "response_format": config.get("response_format"),
        "speed": round(float(speed or 1.0), 3),
        "segment_gap_ms": int(segment_gap_ms or 0),
    }
    return script_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def resolve_tts_voice(cfg: dict, text: str = ""):
    voice = None
    if str(cfg.get("default_tts_voice_id") or "").strip():
        voice = row("SELECT * FROM tts_voices WHERE id=?", (int(cfg["default_tts_voice_id"]),))
    if not voice:
        voice = row(
            """SELECT v.* FROM tts_voices v JOIN tts_service_configs s ON s.id=v.service_id
               WHERE s.is_enabled=1 ORDER BY s.is_default DESC,v.is_default DESC,v.id LIMIT 1"""
        )
    if not voice:
        raise ValueError("没有可用的 TTS 音色")
    config = row("SELECT * FROM tts_service_configs WHERE id=? AND is_enabled=1", (voice["service_id"],))
    if not config:
        raise ValueError("音色对应的 TTS 服务不可用")
    language = detect_text_language(text)
    if config.get("provider") == "edge_tts" and not voice_matches_language(voice.get("voice_code"), language):
        prefixes = LANGUAGE_VOICE_PREFIXES.get(language, ())
        for prefix in prefixes:
            candidate = row(
                """SELECT v.* FROM tts_voices v JOIN tts_service_configs s ON s.id=v.service_id
                   WHERE s.is_enabled=1 AND s.provider='edge_tts' AND v.voice_code LIKE ?
                   ORDER BY s.is_default DESC,v.is_default DESC,v.id LIMIT 1""",
                (prefix + "%",),
            )
            if candidate:
                voice = candidate
                config = row("SELECT * FROM tts_service_configs WHERE id=? AND is_enabled=1", (voice["service_id"],))
                break
    return voice, config


def settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in rows("SELECT key,value FROM app_settings")}


def publishing_accounts() -> list[dict]:
    accounts = rows(
        """SELECT a.*,p.code platform_code,p.name platform_name,p.region,
           p.network_requirement,p.login_type,p.login_url,p.enabled,p.integration_type,p.adapter_code,
           p.capability_status,p.capability_status_reason,p.last_tested_at,
           p.cookie_domains_json,p.login_cookie_names_json
           FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id
           WHERE a.status='active' ORDER BY p.region,p.id,a.updated_at DESC,a.id DESC"""
    )
    for account in accounts:
        if account.get("integration_type") == "api":
            account["authorization_status"] = authorization_status(int(account["id"]))["status"]
            account["login_status"] = "not_applicable"
    return accounts


def save_publishing_account(data: dict) -> int:
    platform_id = int(data.get("platform_id") or 0)
    account_name = str(data.get("account_name") or "").strip()
    if not account_name:
        raise ValueError("请输入账号名称")
    platform = row("SELECT * FROM platforms WHERE id=? AND enabled=1", (platform_id,))
    if not platform:
        raise ValueError("平台不存在或已停用")
    stamp = now()
    integration_type = str(platform.get("integration_type") or "browser")
    client_secret = str(data.get("oauth_client_secret") or "").strip()
    api_config = {
        "client_id": str(data.get("oauth_client_id") or "").strip(),
        "client_secret_encrypted": protect(client_secret) if client_secret else "",
        "redirect_uri": str(data.get("oauth_redirect_uri") or "").strip(),
    } if integration_type == "api" else {}
    existing = row(
        "SELECT * FROM publish_accounts WHERE platform_id=? AND account_name=?",
        (platform_id, account_name),
    )
    if existing and existing.get("status") == "active":
        raise ValueError("该平台下已存在同名账号")
    if existing:
        account_id = int(existing["id"])
        path = profile_path(platform["code"], account_name, account_id) if integration_type == "browser" else ""
        if path:
            ensure_profile(path)
        with connect() as conn:
            conn.execute(
                """UPDATE publish_accounts SET status='active',login_status='not_logged_in',
                   browser_profile_path=?,publishing_mode=?,api_config_json=?,authorization_status='not_authorized',
                   last_checked_at='',last_error='',updated_at=? WHERE id=?""",
                (str(path), integration_type, json.dumps(api_config, ensure_ascii=False), stamp, account_id),
            )
        return account_id
    try:
        with connect() as conn:
            cursor = conn.execute(
                """INSERT INTO publish_accounts
                   (platform_id,account_name,login_status,browser_profile_path,last_checked_at,last_error,
                    publishing_mode,api_config_json,authorization_status,status,created_at,updated_at)
                   VALUES(?,?,'not_logged_in','','','',?,?,'not_authorized','active',?,?)""",
                (platform_id, account_name, integration_type, json.dumps(api_config, ensure_ascii=False), stamp, stamp),
            )
            account_id = int(cursor.lastrowid)
            path = profile_path(platform["code"], account_name, account_id) if integration_type == "browser" else ""
            if path:
                ensure_profile(path)
            conn.execute("UPDATE publish_accounts SET browser_profile_path=? WHERE id=?", (str(path), account_id))
        return account_id
    except sqlite3.IntegrityError as exc:
        raise ValueError("该平台下已存在同名账号") from exc


def publishing_account_context(account_id: int) -> tuple[dict, dict]:
    account = row("SELECT * FROM publish_accounts WHERE id=? AND status='active'", (account_id,))
    if not account:
        raise ValueError("发布账号不存在")
    platform = row("SELECT * FROM platforms WHERE id=? AND enabled=1", (account["platform_id"],))
    if not platform:
        raise ValueError("平台已停用")
    return platform, account


def start_account_login(account_id: int) -> dict:
    platform, account = publishing_account_context(account_id)
    if platform.get("integration_type") == "api":
        return start_authorization(platform, account)
    with connect() as conn:
        conn.execute(
            "UPDATE publish_accounts SET login_status='login_pending',last_error='',updated_at=? WHERE id=?",
            (now(), account_id),
        )
    try:
        adapter = get_adapter(platform.get("adapter_code") or platform["code"])
        result = adapter.start_authorization(
            account["browser_profile_path"], account_id=account_id,
            cookie_domains_json=platform.get("cookie_domains_json") or "[]",
            login_cookie_names_json=platform.get("login_cookie_names_json") or "[]",
        )
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE publish_accounts SET last_error=?,updated_at=? WHERE id=?", (str(exc)[:500], now(), account_id))
        raise
    return {**result, "status": "login_pending", "network_requirement": platform.get("network_requirement", "")}


def check_account_login(account_id: int) -> dict:
    platform, account = publishing_account_context(account_id)
    if platform.get("integration_type") == "api":
        if platform.get("code") == "youtube":
            stamp = now()
            try:
                verification = verify_youtube_account(account)
                with connect() as conn:
                    conn.execute(
                        """UPDATE publish_accounts SET authorization_status='authorized',channel_id=?,channel_title=?,
                           last_checked_at=?,last_error='',updated_at=? WHERE id=?""",
                        (verification.get("channel_id", ""), verification.get("channel_title", ""), stamp, stamp, account_id),
                    )
                return {"status": "authorized", "message": "YouTube OAuth授权有效，频道信息读取成功",
                        "last_checked_at": stamp, **verification}
            except Exception as exc:
                message = str(exc)
                with connect() as conn:
                    conn.execute(
                        "UPDATE publish_accounts SET authorization_status='expired',last_checked_at=?,last_error=?,updated_at=? WHERE id=?",
                        (stamp, message[:500], stamp, account_id),
                    )
                raise RuntimeError(message) from exc
        result = authorization_status(account_id)
        stamp = now()
        with connect() as conn:
            conn.execute(
                "UPDATE publish_accounts SET authorization_status=?,last_checked_at=?,last_error='',updated_at=? WHERE id=?",
                (result["status"], stamp, stamp, account_id),
            )
        message = "OAuth 授权有效" if result["status"] == "authorized" else "尚未完成 OAuth 授权"
        return {**result, "message": message, "last_checked_at": stamp}
    try:
        result = detect_login_status(platform, account)
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE publish_accounts SET login_status='check_failed',last_checked_at=?,last_error=?,updated_at=? WHERE id=?", (now(), str(exc)[:500], now(), account_id))
        raise
    stamp = now()
    with connect() as conn:
        conn.execute(
            "UPDATE publish_accounts SET login_status=?,last_checked_at=?,last_error='',updated_at=? WHERE id=?",
            (result["status"], stamp, stamp, account_id),
        )
    return {**result, "last_checked_at": stamp}


def open_account_profile(account_id: int) -> dict:
    platform, account = publishing_account_context(account_id)
    if platform.get("integration_type") != "browser":
        raise ValueError("API 账号不使用浏览器 Profile")
    return {"path": open_profile_folder(account["browser_profile_path"])}


def update_status() -> dict:
    config = settings()
    return {
        "current_version": current_version(),
        "auto_check_update": str(config.get("auto_check_update", "1")).lower() in ("1", "true", "on", "yes"),
        "configured": bool(configured_manifest_url(config)),
    }


def check_configured_update() -> dict:
    url = configured_manifest_url(settings())
    if not url:
        raise UpdateError("尚未配置 update.json 的 HTTPS 地址")
    return check_for_update(url)


def apply_configured_update() -> dict:
    result = check_configured_update()
    if not result.get("available"):
        raise UpdateError("当前已经是最新版本")
    manifest = UpdateManifest(**result["manifest"])
    staged = stage_and_launch(manifest)
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return staged


def media_url(path: str) -> str:
    if not path:
        return ""
    from urllib.parse import quote
    return "/media?path=" + quote(str(path), safe="")


def generate_title_preview(data):
    """Render the settings preview through the same libass pipeline as final video."""
    merged = settings()
    merged.update({key: str(value) for key, value in (data or {}).items()})
    title = title_config_from_settings(merged)
    if not title:
        title = title_config_from_settings({**merged, "title_main": "主标题样式预览"})
    preview_dir = APP_DIR / "data" / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    ass_path = preview_dir / "title_preview.ass"
    output_path = preview_dir / "title_preview.jpg"
    item = {
        "text": title.pop("main", ""),
        "sub_text": title.pop("sub", ""),
        "start_time": 0,
        "end_time": 1,
        **title,
    }
    render_title_ass(item, {"start_time": 0}, 1, 1080, 1920, ass_path)
    title_filter = f"ass='{ffmpeg_filter_path(ass_path)}'"
    font_dir = str(item.get("font_dir") or "").strip()
    if font_dir and Path(font_dir).is_dir():
        title_filter += f":fontsdir='{ffmpeg_filter_path(font_dir)}'"
    ffmpeg = find_ffmpeg(merged.get("ffmpeg_path", ""))
    proc = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=0xE4EAF4:s=1080x1920:d=1",
         "-vf", title_filter, "-frames:v", "1", "-q:v", "2", str(output_path)],
        capture_output=True,
        timeout=30,
        **background_process_kwargs(),
    )
    if proc.returncode != 0 or not output_path.exists():
        raise ValueError("标题预览渲染失败")
    return {"url": media_url(str(output_path)) + f"&v={output_path.stat().st_mtime_ns}"}


def bootstrap():
    profiles = rows("SELECT * FROM account_profiles ORDER BY updated_at DESC,id DESC")
    categories = rows("SELECT * FROM material_categories ORDER BY sort_order,id")
    materials = rows(
        """SELECT m.*,c.name category_name FROM materials m
        LEFT JOIN material_categories c ON c.id=m.category_id
        WHERE m.status='active' ORDER BY m.updated_at DESC,m.id DESC"""
    )
    for m in materials:
        m["thumbnail_url"] = media_url(m.get("thumbnail_path", ""))
        m["file_url"] = media_url(m.get("file_path", ""))
    templates = rows(
        """SELECT t.*,COUNT(s.id) shot_count,COALESCE(SUM(s.voice_duration),0) total_duration
        FROM edit_templates t LEFT JOIN template_shots s ON s.template_id=t.id
        WHERE COALESCE(t.is_hidden,0)=0
        GROUP BY t.id ORDER BY t.updated_at DESC,t.id DESC"""
    )
    shots = rows(
        """SELECT s.*,c.name category_name,v.name tts_voice_name,v.voice_code,tvc.name tts_service_name,
        m.id matched_material_id,m.file_name matched_file_name,
        m.file_path matched_file_path,m.thumbnail_path matched_thumbnail_path,m.duration matched_material_duration,
        gsm.material_start_time,gsm.material_end_time
        FROM template_shots s
        JOIN edit_templates et ON et.id=s.template_id AND COALESCE(et.is_hidden,0)=0
        LEFT JOIN material_categories c ON c.id=s.material_category_id
        LEFT JOIN tts_voices v ON v.id=s.tts_voice_id
        LEFT JOIN tts_service_configs tvc ON tvc.id=v.service_id
        LEFT JOIN generation_shot_materials gsm ON gsm.id=(SELECT id FROM generation_shot_materials x WHERE x.shot_id=s.id ORDER BY x.id DESC LIMIT 1)
        LEFT JOIN materials m ON m.id=gsm.material_id
        ORDER BY s.template_id,s.sort_order,s.id"""
    )
    for s in shots:
        s["matched_thumbnail_url"] = media_url(s.get("matched_thumbnail_path", ""))
        s["voice_url"] = media_url(s.get("voice_file_path", ""))
    tasks = rows(
        """SELECT g.*,COALESCE(src.name,t.name) template_name,
        COALESCE((SELECT SUM(s2.voice_duration) FROM template_shots s2 WHERE s2.template_id=g.template_id),0) output_duration,
        bi.batch_id,bi.index_no,b.status batch_status FROM generation_tasks g
        LEFT JOIN edit_templates t ON t.id=g.template_id
        LEFT JOIN edit_templates src ON src.id=t.source_template_id
        LEFT JOIN batch_task_items bi ON bi.task_id=g.id
        LEFT JOIN batch_tasks b ON b.id=bi.batch_id ORDER BY g.id DESC"""
    )
    for task in tasks:
        task["output_url"] = media_url(task.get("output_path", ""))
        task["output_exists"] = bool(task.get("output_path") and Path(task["output_path"]).is_file())
    batch_tasks = rows(
        """SELECT b.*,t.name template_name FROM batch_tasks b
        LEFT JOIN edit_templates t ON t.id=b.template_id ORDER BY b.id DESC"""
    )
    batch_items = rows(
        """SELECT i.*,g.task_name,g.progress,g.current_shot,g.total_shots,g.output_path task_output_path,
        g.error_message task_error_message FROM batch_task_items i
        JOIN generation_tasks g ON g.id=i.task_id ORDER BY i.batch_id DESC,i.index_no"""
    )
    for item in batch_items:
        item["output_url"] = media_url(item.get("output_path") or item.get("task_output_path") or "")
    for batch in batch_tasks:
        try:
            batch["category_ids"] = json.loads(batch.get("category_ids_json") or "[]")
        except (TypeError, ValueError):
            batch["category_ids"] = []
        current = next((i for i in batch_items if i["batch_id"] == batch["id"] and i["status"] == "running"), None)
        done = int(batch.get("completed_count") or 0) + int(batch.get("failed_count") or 0)
        partial = float(current.get("progress") or 0) / 100 if current else 0
        batch["progress"] = round((done + partial) / max(1, int(batch.get("total_count") or 0)) * 100)
    bgms = rows("SELECT * FROM bgm_library ORDER BY created_at DESC,id DESC")
    transitions = rows("SELECT * FROM shot_transitions ORDER BY from_shot_id,to_shot_id")
    sound_effects = rows("SELECT * FROM sound_effect_library ORDER BY category,name,id")
    ai_services = rows("SELECT * FROM ai_service_configs ORDER BY is_default DESC,id")
    tts_services = rows("SELECT * FROM tts_service_configs ORDER BY is_default DESC,id")
    tts_voices = rows("SELECT v.*,s.name service_name,s.provider FROM tts_voices v JOIN tts_service_configs s ON s.id=v.service_id ORDER BY s.is_default DESC,v.is_default DESC,v.id")
    platforms = rows("SELECT * FROM platforms ORDER BY CASE region WHEN 'domestic' THEN 0 ELSE 1 END,id")
    for platform in platforms:
        platform["field_support"] = platform_support(platform["code"])
    publish_accounts = publishing_accounts()
    platform_test_runs = list_test_runs() if is_admin_mode() else []
    publish_tasks = rows(
        """SELECT pt.*,g.task_name source_task_name FROM publish_tasks pt
           LEFT JOIN generation_tasks g ON g.id=pt.generation_task_id ORDER BY pt.id DESC LIMIT 100"""
    )
    publish_channels = rows(
        """SELECT pc.*,a.account_name,p.name platform_name,p.code platform_code,p.integration_type,p.capability_status
           FROM publish_channels pc JOIN publish_accounts a ON a.id=pc.account_id
           JOIN platforms p ON p.id=pc.platform_id ORDER BY pc.publish_task_id DESC,pc.id"""
    )
    for task in publish_tasks:
        try:
            task["tags"] = json.loads(task.get("tags_json") or "[]")
        except (TypeError, ValueError):
            task["tags"] = []
        try:
            task["ai_hashtags_list"] = json.loads(task.get("ai_hashtags") or "[]")
        except (TypeError, ValueError):
            task["ai_hashtags_list"] = []
        task["video_url"] = media_url(task.get("video_path", ""))
        task["cover_url"] = media_url(task.get("cover_path", ""))
        task["channels"] = [channel for channel in publish_channels if channel["publish_task_id"] == task["id"]]
    return {"profiles": profiles, "categories": categories, "materials": materials,
            "templates": templates, "shots": shots, "tasks": tasks, "batch_tasks": batch_tasks,
            "batch_items": batch_items, "bgms": bgms, "transitions": transitions,
            "sound_effects": sound_effects,
            "ai_services": ai_services, "tts_services": tts_services, "tts_voices": tts_voices,
            "platforms": platforms, "publish_accounts": publish_accounts,
            "publish_tasks": publish_tasks, "publish_channels": publish_channels,
            "platform_test_runs": platform_test_runs, "admin_mode": is_admin_mode(),
            "settings": settings(), "db_path": str(DB_PATH)}


def dashboard_task_snapshot():
    """Small task-only payload used by the workbench's 3-second refresh."""
    summary = row(
        """SELECT
        SUM(CASE WHEN status IN ('running','processing_shot','merging') THEN 1 ELSE 0 END) running_count,
        SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) queued_count,
        SUM(CASE WHEN status='completed' AND substr(updated_at,1,10)=date('now','localtime') THEN 1 ELSE 0 END) today_completed_count,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_count
        FROM generation_tasks"""
    ) or {}
    active = rows(
        """SELECT g.id,g.task_name,g.status,g.progress,g.current_shot,g.total_shots,g.updated_at,
        COALESCE(src.name,t.name) template_name
        FROM generation_tasks g
        LEFT JOIN edit_templates t ON t.id=g.template_id
        LEFT JOIN edit_templates src ON src.id=t.source_template_id
        WHERE g.status IN ('pending','running','processing_shot','merging')
        ORDER BY CASE WHEN g.status IN ('running','processing_shot','merging') THEN 0 ELSE 1 END,
                 g.updated_at DESC,g.id DESC LIMIT 12"""
    )
    current = next((item for item in active if item["status"] != "pending"), None)
    queued = [item for item in active if item["status"] == "pending"][:2]
    return {
        "running_count": int(summary.get("running_count") or 0),
        "queued_count": int(summary.get("queued_count") or 0),
        "today_completed_count": int(summary.get("today_completed_count") or 0),
        "failed_count": int(summary.get("failed_count") or 0),
        "current": current,
        "queued": queued,
    }


def dashboard_task_thumbnail(task_id: int) -> Path:
    task = row("SELECT output_path FROM generation_tasks WHERE id=? AND status='completed'", (task_id,))
    source = Path(str((task or {}).get("output_path") or ""))
    if not source.is_file():
        raise FileNotFoundError("成片文件不存在")
    stamp = source.stat().st_mtime_ns
    digest = hashlib.sha256(f"dashboard:{task_id}:{stamp}:{source}".encode("utf-8")).hexdigest()
    cached = THUMB_DIR / f"{digest[:16]}.jpg"
    if cached.is_file():
        return cached
    return Path(make_thumbnail(source, digest, settings().get("ffmpeg_path", "")))


def open_task_output(task_id: int):
    task = row("SELECT output_path FROM generation_tasks WHERE id=?", (task_id,))
    output = Path(str((task or {}).get("output_path") or ""))
    if not output.is_file():
        raise FileNotFoundError("成片文件不存在")
    subprocess.Popen(["explorer.exe", "/select,", str(output)])
    return True


def select_folder() -> str:
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    p = subprocess.run(["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
                       capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
                       **background_process_kwargs())
    return (p.stdout or "").strip()


def select_cover_file() -> str:
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Filter = "Image files|*.jpg;*.jpeg;*.jfif;*.png;*.webp;*.bmp;*.tif;*.tiff|All files|*.*"
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.FileName) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    process = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(),
    )
    selected = (process.stdout or "").lstrip("\ufeff").strip()
    return normalize_cover_path(selected) if selected else ""


def select_google_oauth_client() -> dict:
    """Import a Google Desktop OAuth JSON without retaining the source file."""
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Filter = "Google OAuth JSON|*.json|All files|*.*"
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.FileName) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    process = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(),
    )
    selected = (process.stdout or "").lstrip("\ufeff").strip()
    if not selected:
        return {}
    path = Path(selected)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError("Google OAuth JSON 无法读取或格式错误") from exc
    config = payload.get("installed") or payload.get("web") or {}
    client_id = str(config.get("client_id") or "").strip()
    client_secret = str(config.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("OAuth JSON 中缺少 client_id 或 client_secret")
    redirect_uris = config.get("redirect_uris") or []
    redirect_uri = next((str(item) for item in redirect_uris if str(item).startswith("http://127.0.0.1")),
                        "http://127.0.0.1:17866/api/oauth/callback")
    return {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
            "source_name": path.name}


def select_voice_sample() -> str:
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Filter = "Voice sample|*.mp3;*.m4a;*.wav|All files|*.*"
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.FileName) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    process = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(),
    )
    return (process.stdout or "").lstrip("\ufeff").strip()


def clone_tts_voice(service_id: int, data: dict):
    config = row("SELECT * FROM tts_service_configs WHERE id=?", (service_id,))
    if not config or config.get("provider") != "minimax":
        raise ValueError("请选择 MiniMax 克隆语音服务")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("请输入克隆音色名称")
    result = clone_minimax_voice(
        config, str(data.get("sample_path") or ""), str(data.get("voice_id") or ""),
        str(data.get("preview_text") or ""),
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO tts_voices(service_id,name,voice_code,is_default,created_at,updated_at) VALUES(?,?,?,0,?,?)",
            (service_id, name, result["voice_code"], now(), now()),
        )
        result["id"] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return result


def scan_materials(folder: str):
    root = Path(folder)
    if not root.is_dir():
        raise ValueError("素材目录不存在")
    cfg = settings()
    ffprobe = cfg.get("ffprobe_path", "")
    ffmpeg = cfg.get("ffmpeg_path", "")
    files = scan_video_files(folder)
    folder_categories = [item.name for item in root.iterdir() if item.is_dir()]
    if any(path.parent == root for path in files):
        folder_categories.append("未分类")
    # The first-level folders are the category source of truth.
    with connect() as conn:
        for order, category_name in enumerate(dict.fromkeys(folder_categories), 1):
            conn.execute(
                """INSERT INTO material_categories(name,description,sort_order,created_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET sort_order=excluded.sort_order,updated_at=excluded.updated_at""",
                (category_name, "扫描目录自动创建", order, now(), now()),
            )
    added = updated = 0
    with connect() as conn:
        conn.execute("INSERT INTO scan_tasks(folder_path,status,total_files,created_at) VALUES(?,?,?,?)",
                     (folder, "running", len(files), now()))
        scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for index, path in enumerate(files, 1):
        rel = path.relative_to(root)
        category_name = rel.parts[0] if len(rel.parts) > 1 else "未分类"
        with connect() as conn:
            cat = conn.execute("SELECT id FROM material_categories WHERE name=?", (category_name,)).fetchone()
            if not cat:
                max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM material_categories").fetchone()[0]
                conn.execute("INSERT INTO material_categories(name,description,sort_order,created_at,updated_at) VALUES(?,?,?,?,?)",
                             (category_name, "扫描目录自动创建", max_order, now(), now()))
                category_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                category_id = cat[0]
        digest = quick_hash(path)
        info = probe_video(path, ffprobe)
        thumb = make_thumbnail(path, digest, ffmpeg)
        with connect() as conn:
            exists = conn.execute("SELECT id FROM materials WHERE file_path=?", (str(path),)).fetchone()
            if exists:
                conn.execute("""UPDATE materials SET category_id=?,file_name=?,file_hash=?,duration=?,width=?,height=?,fps=?,size=?,thumbnail_path=?,status='active',updated_at=? WHERE id=?""",
                             (category_id, path.name, digest, info["duration"], info["width"], info["height"], info["fps"], info["size"], thumb, now(), exists[0]))
                updated += 1
            else:
                conn.execute("""INSERT INTO materials(category_id,file_path,file_name,file_hash,duration,width,height,fps,size,thumbnail_path,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
                    (category_id, str(path), path.name, digest, info["duration"], info["width"], info["height"], info["fps"], info["size"], thumb, now(), now()))
                added += 1
            conn.execute("UPDATE scan_tasks SET scanned_files=?,added_count=? WHERE id=?", (index, added, scan_id))
    with connect() as conn:
        conn.execute("UPDATE scan_tasks SET status='completed',finished_at=?,skipped_count=? WHERE id=?", (now(), updated, scan_id))
        allowed_names = list(dict.fromkeys(folder_categories))
        root_resolved = root.resolve()
        outside_ids = []
        for material in conn.execute("SELECT id,file_path FROM materials").fetchall():
            try:
                Path(material["file_path"]).resolve().relative_to(root_resolved)
            except (ValueError, OSError):
                outside_ids.append(material["id"])
        if outside_ids:
            marks = ",".join("?" for _ in outside_ids)
            conn.execute(f"DELETE FROM generation_shot_materials WHERE material_id IN ({marks})", outside_ids)
            conn.execute(f"DELETE FROM materials WHERE id IN ({marks})", outside_ids)
        if allowed_names:
            marks = ",".join("?" for _ in allowed_names)
            obsolete = [r["id"] for r in conn.execute(
                f"SELECT id FROM material_categories WHERE name NOT IN ({marks})", allowed_names
            ).fetchall()]
        else:
            obsolete = [r["id"] for r in conn.execute("SELECT id FROM material_categories").fetchall()]
        if obsolete:
            marks = ",".join("?" for _ in obsolete)
            conn.execute(f"UPDATE template_shots SET material_category_id=NULL WHERE material_category_id IN ({marks})", obsolete)
            conn.execute(f"DELETE FROM material_categories WHERE id IN ({marks})", obsolete)
        conn.execute(
            """INSERT INTO app_settings(key,value,updated_at) VALUES('material_root',?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (str(root_resolved), now()),
        )
    return {"total": len(files), "added": added, "updated": updated, "folder": folder}


def save_profile(data):
    values = [str(data.get(k, "")).strip() for k in ("company_name", "industry", "product_info", "brand_intro", "persona_info")]
    if not values[0]:
        raise ValueError("公司名称不能为空")
    with connect() as conn:
        if data.get("id"):
            conn.execute("UPDATE account_profiles SET company_name=?,industry=?,product_info=?,brand_intro=?,persona_info=?,updated_at=? WHERE id=?",
                         (*values, now(), int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO account_profiles(company_name,industry,product_info,brand_intro,persona_info,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (*values, now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def generate_profile_suggestions(data):
    """Generate editable knowledge-base suggestions without writing profile data."""
    brand_name = str(data.get("brand_name") or "").strip()
    industry = str(data.get("industry") or "").strip()
    product = str(data.get("product_intro") or data.get("main_business") or "").strip()
    if not brand_name or not industry or not product:
        raise ValueError("请先填写品牌名称 行业和产品介绍")
    config = row("SELECT * FROM ai_service_configs WHERE is_enabled=1 ORDER BY is_default DESC,id LIMIT 1")
    if not config:
        raise ValueError("请先在系统设置中配置并启用 AI 服务")
    context = {
        "brand_name": brand_name,
        "industry": industry,
        "product": product,
        "main_business": str(data.get("main_business") or "").strip(),
        "region": str(data.get("region") or "").strip(),
        "role": str(data.get("role") or "").strip(),
        "years_experience": str(data.get("years_experience") or "").strip(),
    }
    system = (
        "你是短视频账号定位与品牌知识库顾问 根据用户已填写的真实资料补全创作建议 "
        "不得编造具体金额 年限 资质 客户数量或认证 若资料不足请使用稳妥概括 "
        "只返回严格 JSON 对象 不要 Markdown"
    )
    user = f"""已知资料：{json.dumps(context, ensure_ascii=False)}

请返回以下 JSON 字段，所有值使用简体中文：
{{
  "investment_story":"投入与起步建议",
  "setback_solution":"挫折与解决建议",
  "trust_reason":"信任理由",
  "selling_points":"核心卖点 多项用换行分隔",
  "differentiation":"与同行区别",
  "target_customer":"目标客户",
  "usage_scenario":"使用场景",
  "customer_needs":"用户需求",
  "goal":"目标定位",
  "current_positioning":"一句话AI定位",
  "content_directions":["内容方向1","内容方向2","内容方向3","内容方向4"]
}}"""
    content = chat_completion(
        config,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.65,
    )
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 未返回有效的填写建议")
    try:
        payload = json.loads(content[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("AI 填写建议格式错误 请重新生成") from exc
    allowed = (
        "investment_story", "setback_solution", "trust_reason", "selling_points",
        "differentiation", "target_customer", "usage_scenario", "customer_needs",
        "goal", "current_positioning",
    )
    result = {key: str(payload.get(key) or "").strip() for key in allowed}
    directions = payload.get("content_directions") or []
    result["content_directions"] = [str(item).strip() for item in directions if str(item).strip()][:6]
    return result


def save_template(data):
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("模板名称不能为空")
    with connect() as conn:
        if data.get("id"):
            conn.execute("UPDATE edit_templates SET name=?,description=?,updated_at=? WHERE id=?",
                         (name, str(data.get("description", "")), now(), int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO edit_templates(name,description,created_at,updated_at) VALUES(?,?,?,?)",
                     (name, str(data.get("description", "")), now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def copy_template(template_id: int):
    template = row("SELECT * FROM edit_templates WHERE id=?", (template_id,))
    if not template:
        raise ValueError("模板不存在")
    with connect() as conn:
        conn.execute("INSERT INTO edit_templates(name,description,output_width,output_height,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (template["name"] + " - 副本", template.get("description", ""), template.get("output_width", 1080), template.get("output_height", 1920), now(), now()))
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        source = conn.execute("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (template_id,)).fetchall()
        shot_id_map = {}
        for shot in source:
            conn.execute("""INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,material_category_id,duration,estimated_duration,mute_original_audio,enable_subtitle,enable_tts,tts_voice_id,sort_order,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (new_id, shot["shot_name"], shot["original_text"], "", shot["material_category_id"], shot["duration"], shot["estimated_duration"], shot["mute_original_audio"], shot["enable_subtitle"], shot["enable_tts"], shot["tts_voice_id"], shot["sort_order"], now(), now()))
            shot_id_map[int(shot["id"])] = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if shot_id_map:
            marks = ",".join("?" for _ in shot_id_map)
            transitions = conn.execute(
                f"SELECT * FROM shot_transitions WHERE from_shot_id IN ({marks}) AND to_shot_id IN ({marks})",
                tuple(shot_id_map) + tuple(shot_id_map),
            ).fetchall()
            for transition in transitions:
                conn.execute(
                    """INSERT INTO shot_transitions
                       (from_shot_id,to_shot_id,effect,duration,enabled,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (shot_id_map[int(transition["from_shot_id"])], shot_id_map[int(transition["to_shot_id"])],
                     transition["effect"], transition["duration"], transition["enabled"], now(), now()),
                )
        return new_id


def move_shot(shot_id: int, direction: int):
    shot = row("SELECT * FROM template_shots WHERE id=?", (shot_id,))
    if not shot:
        raise ValueError("镜头不存在")
    ordered = rows("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (shot["template_id"],))
    ids = [x["id"] for x in ordered]
    pos = ids.index(shot_id)
    target = pos + direction
    if 0 <= target < len(ids):
        ids[pos], ids[target] = ids[target], ids[pos]
        with connect() as conn:
            for index, value in enumerate(ids, 1):
                conn.execute("UPDATE template_shots SET sort_order=?,updated_at=? WHERE id=?", (index, now(), value))
    return ids


def save_shot(data):
    template_id = int(data.get("template_id") or 0)
    if not template_id:
        raise ValueError("请先选择模板")
    shot_id = int(data.get("id") or 0)
    existing = row("SELECT * FROM template_shots WHERE id=?", (shot_id,)) if shot_id else None
    original = str(data.get("original_text", ""))
    ai_text = str(data.get("ai_text", ""))
    # An AI rewrite belongs to the original text it was generated from.  If the
    # user edits only the original, discard the stale rewrite so generation uses
    # the newly edited copy instead of silently speaking the old rewrite.
    if existing and original != str(existing.get("original_text") or "") and ai_text == str(existing.get("ai_text") or ""):
        ai_text = ""
    old_script = shot_script_text(existing)
    new_script = shot_script_text({"original_text": original, "ai_text": ai_text})
    script_changed = bool(existing) and old_script != new_script
    fields = (str(data.get("shot_name", "镜头")), original, ai_text,
              int(data.get("material_category_id") or 0) or None,
              1 if data.get("mute_original_audio", True) else 0,
              1 if data.get("enable_subtitle", True) else 0,
              1 if data.get("enable_tts", True) else 0,
              int(data.get("tts_voice_id") or 0) or None)
    with connect() as conn:
        if data.get("id"):
            conn.execute("""UPDATE template_shots SET shot_name=?,original_text=?,ai_text=?,material_category_id=?,mute_original_audio=?,enable_subtitle=?,enable_tts=?,tts_voice_id=?,updated_at=? WHERE id=?""",
                         (*fields, now(), int(data["id"])))
            shot_id = int(data["id"])
        else:
            order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM template_shots WHERE template_id=?", (template_id,)).fetchone()[0]
            conn.execute("""INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,material_category_id,mute_original_audio,enable_subtitle,enable_tts,tts_voice_id,sort_order,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (template_id, *fields, order, now(), now()))
            shot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if script_changed:
            conn.execute("""UPDATE template_shots
                SET voice_file_path='',voice_duration=0,estimated_duration=0,
                    subtitle_file_path='',subtitle_render_hash='',voice_text_hash='',updated_at=?
                WHERE id=?""", (now(), shot_id))
            conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL", (shot_id,))
        conn.execute("UPDATE edit_templates SET updated_at=? WHERE id=?", (now(), template_id))
    return shot_id


def match_shot(
    shot_id: int,
    exclude_ids=None,
    task_id: int | None = None,
    avoid_material_ids=None,
    previous_match=None,
):
    shot = row("SELECT * FROM template_shots WHERE id=?", (shot_id,))
    if not shot:
        raise ValueError("镜头不存在")
    duration = float(shot.get("voice_duration") or 0)
    if duration <= 0:
        raise ValueError("请先生成或导入镜头语音")
    candidates = rows("""SELECT * FROM materials WHERE status='active' AND category_id=? AND duration>=?
        ORDER BY (width*height) DESC,duration DESC""", (shot.get("material_category_id"), duration))
    excluded = set(exclude_ids or [])
    avoided = set(avoid_material_ids or [])
    previous_material_id = int((previous_match or {}).get("material_id") or 0)

    # New tasks first avoid every material used by the previous task.  If the
    # category pool is small, at least avoid the material used by this same shot.
    # Only fall back to reuse when the library genuinely has no alternative.
    pools = [
        [m for m in candidates if m["id"] not in excluded and m["id"] not in avoided],
        [m for m in candidates if m["id"] not in excluded and m["id"] != previous_material_id],
        [m for m in candidates if m["id"] not in excluded],
        [m for m in candidates if m["id"] != previous_material_id],
        candidates,
    ]
    available = next((pool for pool in pools if pool), [])
    if not available:
        category = row("SELECT name FROM material_categories WHERE id=?", (shot.get("material_category_id"),))
        short = row("SELECT MAX(duration) duration FROM materials WHERE status='active' AND category_id=?", (shot.get("material_category_id"),))
        max_duration = float((short or {}).get("duration") or 0)
        raise ValueError(f"素材不足：需要 {duration:.1f} 秒{(category or {}).get('name','')}素材，当前最长只有 {max_duration:.1f} 秒")
    top_pixels = int(available[0].get("width", 0)) * int(available[0].get("height", 0))
    top = [m for m in available if int(m.get("width", 0)) * int(m.get("height", 0)) == top_pixels]
    material = random.choice(top)
    max_start = max(0.0, float(material["duration"]) - duration)
    if task_id is not None and material["id"] == previous_material_id and max_start <= 0.5:
        raise ValueError(
            f"素材不足：镜头只能复用上一任务的 {material['file_name']}，"
            "且没有足够长度切换到不同画面，请补充该分类素材"
        )
    start = random.uniform(0.0, max_start) if max_start > 0.05 else 0.0
    # With only one usable source video, still choose a different section when
    # the source has enough spare duration.
    if material["id"] == previous_material_id and max_start > 0.5:
        old_start = float((previous_match or {}).get("material_start_time") or 0)
        min_gap = min(max_start / 2, max(0.5, duration * 0.25))
        for _ in range(12):
            candidate_start = random.uniform(0.0, max_start)
            if abs(candidate_start - old_start) >= min_gap:
                start = candidate_start
                break
    start = round(start, 3)
    end = start + duration
    with connect() as conn:
        if task_id is None:
            conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL", (shot_id,))
        else:
            conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id=?", (shot_id, task_id))
        conn.execute("""INSERT INTO generation_shot_materials(task_id,shot_id,material_id,voice_duration,material_start_time,material_end_time,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""", (task_id, shot_id, material["id"], duration, start, end, now(), now()))
    return {"material_id": material["id"], "file_name": material["file_name"], "duration": material["duration"], "start": start, "end": end}


def generate_tts(shot_id: int):
    shot = row("SELECT * FROM template_shots WHERE id=?", (shot_id,))
    if not shot:
        raise ValueError("镜头不存在")
    text = normalize_rewrite_text(shot_script_text(shot))
    if not text:
        raise ValueError("镜头文案不能为空")
    cfg = settings()
    voice, config = resolve_tts_voice(cfg, text)
    speed = float(cfg.get("tts_speed") or 1.0)
    segment_gap_ms = int(float(cfg.get("tts_segment_gap_ms") or 60))
    # Spaces are explicit cuts in Chinese scripts, but normal word separators
    # in English, Spanish and Vietnamese. Never synthesize Latin text word by word.
    text_parts = split_text(text) if contains_cjk(text) else [text]
    exact_subtitle_segments = []
    part_paths = []
    if len(text_parts) > 1:
        for index, part in enumerate(text_parts, 1):
            part_path, part_duration = synthesize_tts(
                config, voice, part, f"{shot_id}_part_{index:03d}", cfg, speed
            )
            part_paths.append(part_path)
        voice_path, duration, timings = concatenate_tts_wavs(
            part_paths, str(VOICE_DIR / f"shot_{shot_id}.wav"), cfg, return_timings=True
        )
        exact_subtitle_segments = [
            {"start": timing["start"], "end": timing["end"], "text": part}
            for part, timing in zip(text_parts, timings)
        ]
        for generated in VOICE_DIR.glob(f"shot_{shot_id}_part_*.*"):
            generated.unlink(missing_ok=True)
    else:
        voice_path, duration = synthesize_tts(config, voice, text, shot_id, cfg, speed)
    subtitle_path = ""
    if shot.get("enable_subtitle") and str(cfg.get("subtitle_enabled") or "1") != "0":
        subtitle_path = (
            generate_ass_from_segments(shot_id, exact_subtitle_segments)
            if exact_subtitle_segments
            else generate_ass_for_shot(shot_id, voice_path, text, duration)[0]
        )
    with connect() as conn:
        conn.execute("UPDATE template_shots SET voice_file_path=?,voice_duration=?,estimated_duration=?,subtitle_file_path=?,subtitle_render_hash=?,voice_text_hash=?,updated_at=? WHERE id=?",
                     (voice_path, duration, duration, subtitle_path, subtitle_signature(text) if subtitle_path else "", voice_render_hash(text, config, voice, speed, segment_gap_ms), now(), shot_id))
        conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL", (shot_id,))
    return {"voice_file_path": voice_path, "voice_duration": duration, "subtitle_file_path": subtitle_path}


def save_ai_service(data):
    name = str(data.get("name") or "").strip()
    if not name: raise ValueError("AI 服务名称不能为空")
    values = (name, str(data.get("provider") or "openai_compatible"), str(data.get("base_url") or ""), str(data.get("api_key") or ""), str(data.get("model") or ""), int(data.get("timeout_seconds") or 60), 1 if data.get("is_enabled", True) else 0, 1 if data.get("is_default") else 0, str(data.get("system_prompt") or ""))
    with connect() as conn:
        if values[7]: conn.execute("UPDATE ai_service_configs SET is_default=0")
        if data.get("id"):
            conn.execute("UPDATE ai_service_configs SET name=?,provider=?,base_url=?,api_key=?,model=?,timeout_seconds=?,is_enabled=?,is_default=?,system_prompt=?,updated_at=? WHERE id=?", (*values, now(), int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO ai_service_configs(name,provider,base_url,api_key,model,timeout_seconds,is_enabled,is_default,system_prompt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def save_tts_service(data):
    name = str(data.get("name") or "").strip()
    if not name: raise ValueError("TTS 服务名称不能为空")
    values = (name, str(data.get("provider") or "edge_tts"), str(data.get("base_url") or ""), str(data.get("api_key") or ""), str(data.get("model") or ""), str(data.get("response_format") or "mp3"), int(data.get("timeout_seconds") or 120), 1 if data.get("is_enabled", True) else 0, 1 if data.get("is_default") else 0)
    with connect() as conn:
        if values[8]: conn.execute("UPDATE tts_service_configs SET is_default=0")
        if data.get("id"):
            conn.execute("UPDATE tts_service_configs SET name=?,provider=?,base_url=?,api_key=?,model=?,response_format=?,timeout_seconds=?,is_enabled=?,is_default=?,updated_at=? WHERE id=?", (*values, now(), int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO tts_service_configs(name,provider,base_url,api_key,model,response_format,timeout_seconds,is_enabled,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def save_tts_voice(data):
    service_id = int(data.get("service_id") or 0); name = str(data.get("name") or "").strip(); code = str(data.get("voice_code") or "").strip()
    if not service_id or not name or not code: raise ValueError("服务、音色名称和音色代码均不能为空")
    default = 1 if data.get("is_default") else 0
    with connect() as conn:
        if default: conn.execute("UPDATE tts_voices SET is_default=0 WHERE service_id=?", (service_id,))
        if data.get("id"):
            conn.execute("UPDATE tts_voices SET service_id=?,name=?,voice_code=?,is_default=?,updated_at=? WHERE id=?", (service_id,name,code,default,now(),int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO tts_voices(service_id,name,voice_code,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?)", (service_id,name,code,default,now(),now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def rewrite_shot(
    shot_id: int,
    profile_id: int | None = None,
    variation_context: str = "",
    temperature: float = 0.45,
):
    shot = row("SELECT * FROM template_shots WHERE id=?", (shot_id,))
    if not shot: raise ValueError("镜头不存在")
    config = row("SELECT * FROM ai_service_configs WHERE is_enabled=1 ORDER BY is_default DESC,id LIMIT 1")
    if not config: raise ValueError("请先在设置中配置并启用 AI 服务")
    profile = row("SELECT * FROM account_profiles WHERE id=?", (profile_id,)) if profile_id else row("SELECT * FROM account_profiles ORDER BY updated_at DESC,id DESC LIMIT 1")
    profile = profile or {}
    source_language = detect_text_language(shot.get("original_text") or "")
    system = config.get("system_prompt") or "你是短视频口播文案编辑 保持原句含义和事实准确 只输出改写文案"
    system += f" 原文语言为 {source_language} 改写结果必须保持相同语言 禁止翻译成其他语言"
    if source_language != "zh-CN":
        system += " 拉丁文字中的普通单词空格必须保留 不要把每个单词当成独立配音段"
    system += " 输出中禁止任何中英文标点符号 特殊符号或Emoji 只允许文字数字和空格 使用单个空格表示配音与字幕换段"
    system += "\n" + AI_SEGMENTATION_INSTRUCTION
    user = f"""账号资料：\n公司：{profile.get('company_name','')}\n行业：{profile.get('industry','')}\n产品：{profile.get('product_info','')}\n品牌：{profile.get('brand_intro','')}\n人设：{profile.get('persona_info','')}\n\n镜头名称：{shot.get('shot_name','')}\n原始文案：{shot.get('original_text','')}\n\n请改写成适合该账号自然口播的一句话。原文空格表示字幕切换位置，输出时保留对应的空格，不要删除。"""
    if variation_context:
        user += "\n\n批量差异要求：" + variation_context
    content = chat_completion(
        config,
        [{"role":"system","content":system},{"role":"user","content":user}],
        temperature=temperature,
    )
    content = normalize_rewrite_text(content.strip().strip('"“”'))
    with connect() as conn:
        conn.execute("""UPDATE template_shots SET ai_text=?,voice_file_path='',voice_duration=0,
            estimated_duration=0,subtitle_file_path='',subtitle_render_hash='',voice_text_hash='',updated_at=? WHERE id=?""",
            (content, now(), shot_id))
        conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL", (shot_id,))
    return {"shot_id":shot_id,"ai_text":content}


def parse_rewrite_batch(content: str, expected_ids) -> dict[int, str]:
    text = str(content or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("批量改写未返回 JSON 数组")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, list):
        raise ValueError("批量改写结果格式错误")
    expected = {int(value) for value in expected_ids}
    result = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        shot_id = int(item.get("id") or 0)
        ai_text = normalize_rewrite_text(item.get("ai_text") or "")
        if shot_id in expected and ai_text:
            result[shot_id] = ai_text
    if set(result) != expected:
        raise ValueError("批量改写返回的镜头数量不完整")
    return result


def rewrite_template_shots(
    template_id: int,
    profile_id: int | None = None,
    variation_context: str = "",
    temperature: float = 0.45,
):
    shots = rows("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (template_id,))
    if not shots:
        return []
    config = row("SELECT * FROM ai_service_configs WHERE is_enabled=1 ORDER BY is_default DESC,id LIMIT 1")
    if not config:
        raise ValueError("请先在设置中配置并启用 AI 服务")
    profile = row("SELECT * FROM account_profiles WHERE id=?", (profile_id,)) if profile_id else row(
        "SELECT * FROM account_profiles ORDER BY updated_at DESC,id DESC LIMIT 1"
    )
    profile = profile or {}
    source = [
        {
            "id": s["id"],
            "shot_name": s.get("shot_name") or "",
            "original_text": s.get("original_text") or "",
            "language": detect_text_language(s.get("original_text") or ""),
        }
        for s in shots
    ]
    system = (
        "你是短视频口播文案编辑。根据账号资料一次性改写全部镜头，保持镜头顺序和事实准确。"
        "输出禁止使用任何中英文标点符号 特殊符号或Emoji 只允许文字数字和空格。"
        "使用单个空格作为字幕与配音的唯一硬分段标记。"
        "只返回严格 JSON 数组，不要 Markdown；每项格式为 {\"id\":镜头ID,\"ai_text\":\"改写文案\"}。"
        "每个 ai_text 必须保持对应 original_text 标注的 language 禁止翻译成其他语言。"
        "英文 西班牙语 越南语中的单词空格是正常拼写 必须保留。"
    )
    system += "\n" + AI_SEGMENTATION_INSTRUCTION
    if variation_context:
        system += (
            "这是批量生产中的独立文案版本 必须更换开场句式 表达顺序和措辞 "
            "不能复制此前版本 同时保持产品事实不变"
        )
    user = (
        f"账号资料：\n公司：{profile.get('company_name','')}\n行业：{profile.get('industry','')}\n"
        f"产品：{profile.get('product_info','')}\n品牌：{profile.get('brand_intro','')}\n"
        f"人设：{profile.get('persona_info','')}\n\n待改写镜头：\n"
        + json.dumps(source, ensure_ascii=False)
        + (f"\n\n批量差异要求：\n{variation_context}" if variation_context else "")
    )
    content = chat_completion(
        config,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
    )
    try:
        rewritten = parse_rewrite_batch(content, [s["id"] for s in shots])
    except (ValueError, json.JSONDecodeError):
        # Compatibility fallback: still much faster than the old serial loop.
        workers = min(4, len(shots))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(
                pool.map(
                    lambda shot: rewrite_shot(
                        shot["id"], profile_id, variation_context, temperature
                    ),
                    shots,
                )
            )
        return results
    with connect() as conn:
        for shot in shots:
            conn.execute(
                """UPDATE template_shots SET ai_text=?,voice_file_path='',voice_duration=0,
                   estimated_duration=0,subtitle_file_path='',subtitle_render_hash='',voice_text_hash='',updated_at=?
                   WHERE id=?""",
                (rewritten[shot["id"]], now(), shot["id"]),
            )
            conn.execute(
                "DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL",
                (shot["id"],),
            )
    return [{"shot_id": shot["id"], "ai_text": rewritten[shot["id"]]} for shot in shots]


def create_task(data):
    template_id = int(data.get("template_id") or 0)
    template = row("SELECT * FROM edit_templates WHERE id=?", (template_id,))
    shots = rows("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (template_id,))
    if not template or not shots:
        raise ValueError("模板不存在或没有镜头")
    name = str(data.get("task_name") or f"{template['name']} - {now()}")
    with connect() as conn:
        conn.execute("INSERT INTO generation_tasks(template_id,task_name,status,progress,total_shots,created_at,updated_at) VALUES(?,?, 'pending',0,?,?,?)",
                     (template_id, name, len(shots), now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def start_generate(task_id: int):
    task = row("SELECT * FROM generation_tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    cfg = settings()
    output_dir = cfg.get("output_dir", "")
    if not output_dir or not Path(output_dir).parent.exists():
        output_dir = str(APP_DIR / "outputs")
    def worker():
        try:
            task_shots = rows("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (task["template_id"],))
            selected_speed = float(cfg.get("tts_speed") or 1.0)
            selected_segment_gap_ms = int(float(cfg.get("tts_segment_gap_ms") or 60))
            used_materials = []
            batch_context = row(
                """SELECT b.id batch_id,b.material_strategy,i.index_no
                   FROM batch_task_items i JOIN batch_tasks b ON b.id=i.batch_id
                   WHERE i.task_id=?""",
                (task_id,),
            )
            previous_task = row(
                """SELECT t.id FROM generation_tasks t
                   WHERE t.template_id=? AND t.id<?
                     AND EXISTS(SELECT 1 FROM generation_shot_materials gsm WHERE gsm.task_id=t.id)
                   ORDER BY t.id DESC LIMIT 1""",
                (task["template_id"], task_id),
            )
            previous_matches = {}
            if previous_task:
                previous_matches = {
                    item["shot_id"]: item
                    for item in rows(
                        """SELECT shot_id,material_id,material_start_time,material_end_time
                           FROM generation_shot_materials WHERE task_id=?""",
                        (previous_task["id"],),
                    )
                }
            previous_materials = {item["material_id"] for item in previous_matches.values()}
            if batch_context and batch_context["material_strategy"] in ("prefer_unused", "avoid_repeat"):
                previous_materials.update(
                    item["material_id"]
                    for item in rows(
                        """SELECT DISTINCT gsm.material_id FROM batch_task_items i
                           JOIN generation_shot_materials gsm ON gsm.task_id=i.task_id
                           WHERE i.batch_id=? AND i.index_no<?""",
                        (batch_context["batch_id"], batch_context["index_no"]),
                    )
                )
            elif batch_context and batch_context["material_strategy"] == "random":
                previous_matches = {}
                previous_materials = set()
            for index, shot in enumerate(task_shots, 1):
                with connect() as conn:
                    conn.execute("UPDATE generation_tasks SET status='processing_shot',current_shot=?,progress=?,updated_at=? WHERE id=?", (f"准备镜头 {index}/{len(task_shots)}", max(1, int(index / max(1, len(task_shots)) * 8)), now(), task_id))
                current_text = shot_script_text(shot)
                selected_voice, selected_tts_config = resolve_tts_voice(cfg, current_text)
                voice_path = str(shot.get("voice_file_path") or "")
                voice_stale = (
                    float(shot.get("voice_duration") or 0) <= 0
                    or not voice_path
                    or not Path(voice_path).is_file()
                    or str(shot.get("voice_text_hash") or "") != voice_render_hash(
                        current_text, selected_tts_config, selected_voice, selected_speed, selected_segment_gap_ms
                    )
                )
                if shot.get("enable_tts") and voice_stale:
                    generate_tts(shot["id"])
                    # A regenerated voice can have a different real duration, so
                    # its previous material crop is no longer valid.
                    with connect() as conn:
                        conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL", (shot["id"],))
                shot = row("SELECT * FROM template_shots WHERE id=?", (shot["id"],))
                # Always trust ffprobe of the final audio file over cached DB or
                # provider estimates before matching material and rendering.
                previous_voice_duration = float(shot.get("voice_duration") or 0)
                actual_voice_duration = audio_duration(
                    str(shot.get("voice_file_path") or ""), cfg.get("ffprobe_path", "")
                )
                if actual_voice_duration <= 0:
                    raise ValueError(f"镜头 {shot.get('shot_name') or shot['id']} 无法读取真实语音时长")
                duration_changed = abs(actual_voice_duration - previous_voice_duration) > 0.001
                if duration_changed:
                    with connect() as conn:
                        conn.execute(
                            "UPDATE template_shots SET voice_duration=?,estimated_duration=?,updated_at=? WHERE id=?",
                            (actual_voice_duration, actual_voice_duration, now(), shot["id"]),
                        )
                        conn.execute(
                            "DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL",
                            (shot["id"],),
                        )
                    shot = row("SELECT * FROM template_shots WHERE id=?", (shot["id"],))
                if shot.get("enable_subtitle") and str(cfg.get("subtitle_enabled") or "1") != "0":
                    text = shot_script_text(shot)
                    expected_subtitle_hash = subtitle_signature(text)
                    subtitle_path = str(shot.get("subtitle_file_path") or "")
                    subtitle_stale = (
                        duration_changed
                        or
                        not subtitle_path
                        or not Path(subtitle_path).is_file()
                        or str(shot.get("subtitle_render_hash") or "") != expected_subtitle_hash
                    )
                    if subtitle_stale:
                        subtitle_path, _ = generate_ass_for_shot(
                            shot["id"], shot["voice_file_path"], text, float(shot.get("voice_duration") or 0)
                        )
                        with connect() as conn:
                            conn.execute(
                                "UPDATE template_shots SET subtitle_file_path=?,subtitle_render_hash=?,updated_at=? WHERE id=?",
                                (subtitle_path, expected_subtitle_hash, now(), shot["id"]),
                            )
                # Every generation task rematches.  The relation is task-scoped,
                # so creating a new task never silently reuses the old preview.
                relation = match_shot(
                    shot["id"],
                    exclude_ids=used_materials,
                    task_id=task_id,
                    avoid_material_ids=previous_materials,
                    previous_match=previous_matches.get(shot["id"]),
                )
                if (
                    batch_context
                    and batch_context["material_strategy"] == "avoid_repeat"
                    and relation["material_id"] in previous_materials
                ):
                    raise ValueError("避免重复策略下可用素材不足，请补充对应分类素材")
                used_materials.append(relation["material_id"])
            generate_video(task_id, task["template_id"], cfg.get("ffmpeg_path", ""), output_dir, cfg.get("work_dir", ""))
        except Exception as exc:
            with connect() as conn:
                conn.execute("UPDATE generation_tasks SET status='failed',error_message=?,updated_at=? WHERE id=?", (str(exc), now(), task_id))
    with connect() as conn:
        conn.execute("UPDATE generation_tasks SET status='running',current_shot=?,progress=1,error_message='',updated_at=? WHERE id=?", ("等待准备", now(), task_id))
    threading.Thread(target=worker, daemon=True).start()
    return {"task_id": task_id, "status": "running"}


def template_script_snapshot(template_id: int) -> tuple[str, list[str]]:
    texts = [
        normalize_rewrite_text(item.get("ai_text") or item.get("original_text") or "")
        for item in rows(
            "SELECT original_text,ai_text FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
            (template_id,),
        )
    ]
    return "\x1f".join(texts), texts


def start_batch_generation(task_id: int):
    """Create a unique script version before calling the unchanged single-video flow."""
    context = row(
        """SELECT b.id batch_id,b.total_count,i.index_no,g.template_id
           FROM batch_task_items i
           JOIN batch_tasks b ON b.id=i.batch_id
           JOIN generation_tasks g ON g.id=i.task_id
           WHERE i.task_id=?""",
        (task_id,),
    )
    if not context:
        return start_generate(task_id)

    with connect() as conn:
        conn.execute(
            "UPDATE generation_tasks SET status='running',current_shot=?,progress=1,error_message='',updated_at=? WHERE id=?",
            (f"正在生成第 {context['index_no']} 条独立文案", now(), task_id),
        )

    previous = rows(
        """SELECT i.index_no,g.template_id FROM batch_task_items i
           JOIN generation_tasks g ON g.id=i.task_id
           WHERE i.batch_id=? AND i.index_no<? ORDER BY i.index_no""",
        (context["batch_id"], context["index_no"]),
    )
    previous_signatures = set()
    previous_examples = []
    for item in previous:
        signature, texts = template_script_snapshot(item["template_id"])
        if signature:
            previous_signatures.add(signature)
            previous_examples.append(f"版本{item['index_no']} " + " | ".join(texts))

    current_signature = ""
    for attempt in range(1, 4):
        variation = (
            f"当前需要生成第 {context['index_no']} 条 共 {context['total_count']} 条 "
            f"这是第 {attempt} 次差异化尝试 必须与其他版本明显不同"
        )
        if previous_examples:
            variation += "\n禁止复用以下已生成版本的完整句式：\n" + "\n".join(previous_examples[-8:])[:5000]
        rewrite_template_shots(
            context["template_id"],
            variation_context=variation,
            temperature=min(0.95, 0.72 + attempt * 0.08),
        )
        current_signature, _ = template_script_snapshot(context["template_id"])
        if current_signature and current_signature not in previous_signatures:
            break
    if not current_signature or current_signature in previous_signatures:
        raise ValueError("AI 连续生成了重复文案，请重试或调整原始文案")
    return start_generate(task_id)


class CommercialHandler(SimpleHTTPRequestHandler):
    dist_dir: Path = RESOURCE_DIR / "ui-commercial" / "dist"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.dist_dir), **kwargs)

    def log_message(self, _format, *_args):
        return

    def json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/bootstrap":
                return self.json_response({"ok": True, "data": bootstrap()})
            if parsed.path == "/api/dashboard/tasks":
                return self.json_response({"ok": True, "data": dashboard_task_snapshot()})
            thumbnail_match = re.fullmatch(r"/api/dashboard/tasks/(\d+)/thumbnail", parsed.path)
            if thumbnail_match:
                file_path = dashboard_task_thumbnail(int(thumbnail_match.group(1)))
                size = file_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                with file_path.open("rb") as file:
                    while chunk := file.read(256 * 1024):
                        self.wfile.write(chunk)
                return
            if parsed.path == "/api/health":
                return self.json_response({"ok": True, "db": str(DB_PATH)})
            if parsed.path == "/api/update/status":
                return self.json_response({"ok": True, "data": update_status()})
            if parsed.path == "/api/publish/capabilities":
                return self.json_response({"ok": True, "data": {**publishing_capabilities(), "adapters": adapter_catalog(), "publishers": publisher_catalog()}})
            if parsed.path == "/api/oauth/callback":
                query = parse_qs(parsed.query)
                if query.get("error"):
                    state = (query.get("state") or [""])[0]
                    code = query["error"][0]
                    description = (query.get("error_description") or [""])[0]
                    raise ValueError(cancel_authorization(state, code + ((": " + description) if description else "")))
                result = complete_authorization((query.get("state") or [""])[0], (query.get("code") or [""])[0])
                return self.json_response({"ok": True, "data": result})
            if parsed.path == "/media":
                file_path = Path(unquote((parse_qs(parsed.query).get("path") or [""])[0]))
                if not file_path.is_file() or file_path.suffix.lower() not in MEDIA_EXTS:
                    return self.send_error(404)
                mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                size = file_path.stat().st_size
                self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(size)); self.send_header("Accept-Ranges", "bytes"); self.end_headers()
                with file_path.open("rb") as f:
                    while chunk := f.read(1024 * 1024): self.wfile.write(chunk)
                return
            return super().do_GET()
        except Exception as exc:
            return self.json_response({"ok": False, "error": str(exc), "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        data = self.read_json()
        try:
            result = None
            if parsed.path == "/api/select-folder": result = {"path": select_folder()}
            elif parsed.path == "/api/select-cover": result = {"path": select_cover_file()}
            elif parsed.path == "/api/select-google-oauth-client": result = select_google_oauth_client()
            elif parsed.path == "/api/select-voice-sample": result = {"path": select_voice_sample()}
            elif parsed.path == "/api/update/check": result = check_configured_update()
            elif parsed.path == "/api/update/apply": result = apply_configured_update()
            elif parsed.path == "/api/materials/scan": result = scan_materials(str(data.get("folder_path", "")))
            elif parsed.path == "/api/materials/category":
                with connect() as conn: conn.execute("UPDATE materials SET category_id=?,updated_at=? WHERE id=?", (int(data.get("category_id") or 0) or None, now(), int(data["material_id"])))
                result = True
            elif parsed.path == "/api/bgm/scan":
                folder = str(data.get("folder_path", "")); result = scan_bgm_folder(folder, settings().get("ffprobe_path", ""))
                with connect() as conn: conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('bgm_dir',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (folder, now()))
            elif parsed.path == "/api/sfx/scan":
                folder = str(data.get("folder_path", ""))
                result = scan_sfx_folder(folder, settings().get("ffprobe_path", ""))
                with connect() as conn:
                    conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('sfx_dir',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (folder, now()))
            elif parsed.path == "/api/transitions":
                from_id, to_id = int(data.get("from_shot_id") or 0), int(data.get("to_shot_id") or 0)
                effect = str(data.get("effect") or "fade")
                if effect not in {"fade","slide_left","slide_right","zoom","flash","blur"}: raise ValueError("不支持的转场效果")
                duration = max(0.05, min(2.0, float(data.get("duration") or 0.35)))
                with connect() as conn:
                    conn.execute("""INSERT INTO shot_transitions(from_shot_id,to_shot_id,effect,duration,enabled,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(from_shot_id,to_shot_id) DO UPDATE SET effect=excluded.effect,duration=excluded.duration,enabled=excluded.enabled,updated_at=excluded.updated_at""",
                    (from_id,to_id,effect,duration,1 if data.get("enabled",True) else 0,now(),now()))
                result = True
            elif parsed.path == "/api/profiles": result = {"id": save_profile(data)}
            elif parsed.path == "/api/publish/accounts": result = {"id": save_publishing_account(data)}
            elif parsed.path == "/api/publish/tasks": result = {"id": create_publish_task(data), "status": "pending"}
            elif parsed.path == "/api/publish/sync-output": result = sync_missing_publish_videos()
            elif parsed.path == "/api/platform-tests": result = {"id": create_platform_test(data), "status": "pending"}
            elif re.fullmatch(r"/api/platform-tests/\d+/retry", parsed.path):
                result = {"id": retry_platform_test(int(parsed.path.split("/")[3])), "status": "pending"}
            elif parsed.path == "/api/publish/ai-content":
                result = generate_publish_content(
                    int(data.get("video_id") or 0), str(data.get("platform") or "douyin"),
                    int(data.get("profile_id") or 0) or None, str(data.get("current_title") or ""),
                )
            elif re.fullmatch(r"/api/publish/tasks/\d+/enqueue", parsed.path):
                result = enqueue_publish_task(int(parsed.path.split("/")[4]))
                PUBLISH_WORKER.wake()
            elif re.fullmatch(r"/api/publish/accounts/\d+/login", parsed.path):
                result = start_account_login(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/publish/accounts/\d+/authorize", parsed.path):
                result = start_account_login(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/publish/accounts/\d+/check", parsed.path):
                result = check_account_login(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/publish/accounts/\d+/open-profile", parsed.path):
                result = open_account_profile(int(parsed.path.split("/")[4]))
            elif parsed.path == "/api/profiles/ai-suggestions": result = generate_profile_suggestions(data)
            elif parsed.path == "/api/ai-services": result = {"id": save_ai_service(data)}
            elif re.fullmatch(r"/api/ai-services/\d+/test", parsed.path):
                config = row("SELECT * FROM ai_service_configs WHERE id=?", (int(parsed.path.split("/")[3]),)); result = {"message": test_ai_service(config or {})}
            elif parsed.path == "/api/tts-services": result = {"id": save_tts_service(data)}
            elif re.fullmatch(r"/api/tts-services/\d+/test", parsed.path):
                service_id = int(parsed.path.split("/")[3]); config = row("SELECT * FROM tts_service_configs WHERE id=?", (service_id,)); voice = row("SELECT * FROM tts_voices WHERE service_id=? ORDER BY is_default DESC,id LIMIT 1", (service_id,))
                if not config or not voice: raise ValueError("TTS 服务不存在或尚未配置音色")
                path, duration = test_tts_service(config, voice, settings()); result = {"voice_file_path": path, "duration": duration}
            elif re.fullmatch(r"/api/tts-services/\d+/clone", parsed.path):
                result = clone_tts_voice(int(parsed.path.split("/")[3]), data)
            elif parsed.path == "/api/tts-voices": result = {"id": save_tts_voice(data)}
            elif parsed.path == "/api/templates": result = {"id": save_template(data)}
            elif re.fullmatch(r"/api/templates/\d+/copy", parsed.path): result = {"id": copy_template(int(parsed.path.split("/")[3]))}
            elif parsed.path == "/api/shots": result = {"id": save_shot(data)}
            elif re.fullmatch(r"/api/shots/\d+/move", parsed.path): result = move_shot(int(parsed.path.split("/")[3]), int(data.get("direction") or 0))
            elif re.fullmatch(r"/api/shots/\d+/tts", parsed.path): result = generate_tts(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/shots/\d+/match", parsed.path): result = match_shot(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/shots/\d+/rewrite", parsed.path): result = rewrite_shot(int(parsed.path.split("/")[3]), int(data.get("profile_id") or 0) or None)
            elif re.fullmatch(r"/api/templates/\d+/rewrite-all", parsed.path):
                template_id = int(parsed.path.split("/")[3])
                result = rewrite_template_shots(template_id, int(data.get("profile_id") or 0) or None)
            elif parsed.path == "/api/tasks": result = {"id": create_task(data)}
            elif re.fullmatch(r"/api/dashboard/tasks/\d+/open-output", parsed.path):
                result = open_task_output(int(parsed.path.split("/")[4]))
            elif parsed.path == "/api/batches":
                batch_id = create_batch_task(data)
                BATCH_QUEUE.enqueue(batch_id, start_batch_generation)
                result = {"id": batch_id, "status": "pending"}
            elif parsed.path == "/api/tasks/clear-completed":
                with connect() as conn:
                    completed_ids = [r["id"] for r in conn.execute("SELECT id FROM generation_tasks WHERE status='completed'").fetchall()]
                    if completed_ids:
                        marks = ",".join("?" for _ in completed_ids)
                        conn.execute(f"DELETE FROM generation_shot_materials WHERE task_id IN ({marks})", completed_ids)
                    result = {"deleted": conn.execute("DELETE FROM generation_tasks WHERE status='completed'").rowcount}
            elif re.fullmatch(r"/api/tasks/\d+/generate", parsed.path): result = start_generate(int(parsed.path.split("/")[3]))
            elif parsed.path == "/api/settings":
                old_settings = settings()
                voice_changed = any(
                    key in data and str(data.get(key)) != str(old_settings.get(key, ""))
                    for key in ("default_tts_voice_id", "tts_speed", "tts_segment_gap_ms")
                )
                with connect() as conn:
                    for key, value in data.items():
                        conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), now()))
                    if voice_changed:
                        conn.execute(
                            """UPDATE template_shots SET voice_file_path='',voice_duration=0,estimated_duration=0,
                               subtitle_file_path='',subtitle_render_hash='',voice_text_hash='',updated_at=?""",
                            (now(),),
                        )
                        conn.execute("DELETE FROM generation_shot_materials WHERE task_id IS NULL")
                result = settings()
            elif parsed.path == "/api/title-preview":
                result = generate_title_preview(data)
            else: return self.json_response({"ok": False, "error": "接口不存在"}, 404)
            return self.json_response({"ok": True, "data": result})
        except Exception as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        try:
            batch_match = re.fullmatch(r"/api/batches/(\d+)", parsed.path)
            if batch_match:
                batch_id = int(batch_match.group(1))
                batch = row("SELECT status FROM batch_tasks WHERE id=?", (batch_id,))
                if batch and batch["status"] == "running":
                    raise ValueError("批量任务生成中，不能删除")
                with connect() as conn:
                    conn.execute("DELETE FROM batch_tasks WHERE id=?", (batch_id,))
                return self.json_response({"ok": True})
            publish_match = re.fullmatch(r"/api/publish/accounts/(\d+)", parsed.path)
            if publish_match:
                with connect() as conn:
                    conn.execute("UPDATE publish_accounts SET status='deleted',updated_at=? WHERE id=?", (now(), int(publish_match.group(1))))
                return self.json_response({"ok": True})
            publish_task_match = re.fullmatch(r"/api/publish/tasks/(\d+)", parsed.path)
            if publish_task_match:
                delete_publish_task(int(publish_task_match.group(1)))
                return self.json_response({"ok": True})
            patterns = [(r"/api/profiles/(\d+)", "account_profiles"), (r"/api/ai-services/(\d+)", "ai_service_configs"), (r"/api/tts-services/(\d+)", "tts_service_configs"), (r"/api/tts-voices/(\d+)", "tts_voices"), (r"/api/templates/(\d+)", "edit_templates"), (r"/api/shots/(\d+)", "template_shots"), (r"/api/tasks/(\d+)", "generation_tasks")]
            for pattern, table in patterns:
                match = re.fullmatch(pattern, parsed.path)
                if match:
                    record_id = int(match.group(1))
                    with connect() as conn:
                        if table == "generation_tasks":
                            conn.execute("DELETE FROM generation_shot_materials WHERE task_id=?", (record_id,))
                        conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
                    return self.json_response({"ok": True})
            return self.json_response({"ok": False, "error": "接口不存在"}, 404)
        except Exception as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)


def prepare():
    init_db()
    cfg = settings()
    output = cfg.get("output_dir", "")
    if not output or not Path(output).parent.exists():
        with connect() as conn:
            conn.execute("UPDATE app_settings SET value=?,updated_at=? WHERE key='output_dir'", (str(APP_DIR / "outputs"), now()))
    recover_and_enqueue(start_batch_generation)
    start_publish_worker()
