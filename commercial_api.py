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
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from db import (
    APP_DIR, DB_PATH, THUMB_DIR, VOICE_DIR, connect, init_db, now,
    generation_queue_seq_supported, reserve_generation_queue_seqs,
)
from bgm_service import scan_bgm_folder
from sfx_service import scan_sfx_folder
from subtitle_service import generate_ass_for_shot, generate_ass_from_segments, split_text, subtitle_signature
from tts_service import audio_duration, synthesize_windows_tts
from video_engine import find_ffmpeg, generate_video, title_config_from_settings, video_acceleration_status
from timeline_engine import ffmpeg_filter_path, render_title_ass
from video_scan import VIDEO_EXTS, make_thumbnail, make_thumbnail_with_status, probe_video, quick_hash, scan_video_files
from ai_services import (
    AIRequestError, ServiceError, concatenate_tts_wavs, chat_completion,
    synthesize_tts, test_ai_service, test_tts_service,
)
from runtime_paths import RESOURCE_DIR, get_media_runtime_status
from subprocess_utils import background_process_kwargs
from batch_service import BATCH_QUEUE, create_batch_task, recover_and_enqueue, sync_batch
from shared.update_manifest import UpdateManifest
from update_service import UpdateError, check_for_update, configured_manifest_url, current_version, stage_and_launch
from browser_profile_service import (
    detect_login_status,
    ensure_profile,
    open_profile_folder,
    profile_path,
    publishing_capabilities,
)
from tiktok_login_service import (
    cancel_tiktok_login,
    launch_tiktok_login,
    probe_tiktok_login,
    tiktok_login_status,
)
from domestic_login_service import (
    SUPPORTED_PLATFORMS as DOMESTIC_LOGIN_PLATFORMS,
    cancel_domestic_login,
    domestic_login_status,
    domestic_profile_path,
    launch_domestic_login,
    probe_domestic_login,
)
from oauth_service import (
    authorization_status, cancel_authorization, complete_authorization,
    import_google_oauth_payload, provider_preflight, reopen_authorization,
    save_provider_config, select_oauth_account, session_status, start_authorization,
)
from oauth_token_store import protect
from config_secret_store import encrypt_secret, public_service_config, runtime_service_config
from aliyun_bailian_tts import list_voices as list_bailian_voices
from platforms import adapter_catalog, get_adapter
from publishers import publisher_catalog
from publishers.youtube import verify_youtube_account
from publish_queue import (
    PUBLISH_WORKER, create_publish_task, delete_publish_task, enqueue_publish_task,
    recalculate_group_batch, retry_group_batch_failed, start_publish_worker,
    sync_missing_publish_videos,
)
from ai_publish_service import PublishAIError, generate_publish_content
from publish_metadata_service import attach_and_schedule, save_draft_field
from generation_success_service import repair_missing_publish_records
from cover_service import normalize_cover_path
from script_segmentation import DEFAULT_REWRITE_PROMPT, AI_SEGMENTATION_INSTRUCTION, smart_segment_text
from language_service import LANGUAGE_VOICE_PREFIXES, contains_cjk, detect_text_language, voice_matches_language
from voice_clone_service import clone_minimax_voice
from smart_split_api import smart_split_get, smart_split_post
from smart_split_service import recover_smart_split_jobs
from platform_test_service import create_platform_test, is_admin_mode, list_test_runs, platform_support, retry_platform_test
from task_runtime import (
    TaskCancelled, cancel_task, check_cancelled, heartbeat, heartbeat_state,
    task_context, terminate_process, worker_is_alive, initialize_task_log, append_task_log,
    fail_task, task_log_path, canonical_task_status, task_health_snapshot,
    ACTIVE_TASK_STATUSES, GENERATION_QUEUE, MAX_GENERATION_CONCURRENCY,
    generation_worker_snapshot, append_generation_worker_log,
)
from material_matcher import load_material_candidates, material_pool_diagnostics, select_material
from startup_diagnostics import startup_log


MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".wmv", ".mpeg", ".mpg", ".wav", ".mp3", ".m4a", ".ass", ".srt"}
GROUP_BATCH_LOCK = threading.Lock()
BAILIAN_API_KEY_URL = "https://bailian.console.aliyun.com/?tab=model#/api-key"
GENERATION_START_LOCK = threading.RLock()
MATERIAL_SCAN_LOCK = threading.Lock()
RECENT_COMPLETED_LIMIT = 10
TASK_CENTER_PAGE_SIZE = 20
TASK_CENTER_ACTIVE_STATUSES = {
    "queued", "preparing", "rendering", "verifying", "paused", "failed", "stalled",
    "needs_action", "needs_manual_action", "interrupted", "running", "partially_blocked",
}
GENERATION_WATCHDOG_INTERVAL_SECONDS = 5.0
_GENERATION_WATCHDOG_LOCK = threading.Lock()
_GENERATION_WATCHDOG_THREAD: threading.Thread | None = None
_GENERATION_WATCHDOG_STOP = threading.Event()
FRONTEND_CONNECTED_EVENT = threading.Event()
UPDATE_SHUTDOWN_EVENT = threading.Event()


def rows(sql: str, args=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(sql: str, args=()):
    with connect() as conn:
        value = conn.execute(sql, args).fetchone()
        return dict(value) if value else None


def runtime_config(table: str, record_id: int) -> dict | None:
    if table not in {"ai_service_configs", "tts_service_configs"}:
        raise ValueError("不支持的配置类型")
    value = row(f"SELECT * FROM {table} WHERE id=?", (int(record_id),))
    return runtime_service_config(value) if value else None


def migrate_provider_secrets() -> None:
    """Encrypt legacy plaintext provider keys in place with Windows DPAPI."""
    with connect() as conn:
        for table in ("ai_service_configs", "tts_service_configs"):
            values = conn.execute(f"SELECT id,api_key FROM {table} WHERE COALESCE(api_key,'')<>''").fetchall()
            for item in values:
                stored = str(item["api_key"] or "")
                if not stored.startswith("dpapi:"):
                    conn.execute(
                        f"UPDATE {table} SET api_key=?,updated_at=? WHERE id=?",
                        (encrypt_secret(stored), now(), int(item["id"])),
                    )


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
    config = runtime_service_config(config) if config else None
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
                config = runtime_service_config(config) if config else None
                break
    return voice, config


def settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in rows("SELECT key,value FROM app_settings")}


def public_settings() -> dict[str, str]:
    """Settings safe for the ordinary UI; runtime binary paths are internal."""
    values = settings()
    values.pop("ffmpeg_path", None)
    values.pop("ffprobe_path", None)
    return values

def style_presets() -> dict:
    from style_presets import TITLE_STYLE_PRESETS, SUBTITLE_STYLE_PRESETS
    return {'title': TITLE_STYLE_PRESETS, 'subtitle': SUBTITLE_STYLE_PRESETS}


TITLE_TEMPLATE_SCOPES = {"domestic", "overseas"}
TITLE_TEMPLATE_KEYS = (
    "title_main", "title_sub", "title_font_dir", "title_font", "title_font_size",
    "title_color", "title_auto_fit", "title_background_enabled",
    "title_background_color", "title_background_opacity",
    "title_background_padding_y", "title_center", "title_outline_width",
    "title_outline_color", "title_shadow_color", "title_shadow_x",
    "title_shadow_y", "title_main_x", "title_main_y", "title_sub_x",
    "title_sub_y", "subtitle_enabled", "subtitle_font", "subtitle_font_size",
    "subtitle_color", "subtitle_outline_width", "subtitle_outline_color",
    "subtitle_margin_bottom", "subtitle_max_chars_per_line", "subtitle_effect",
    "subtitle_effect_duration",
)


def normalize_title_template_scope(value: str | None) -> str:
    scope = str(value or "domestic").strip().lower()
    if scope == "international":  # compatibility with v1.3.7 clients/data
        scope = "overseas"
    if scope not in TITLE_TEMPLATE_SCOPES:
        raise ValueError("标题字幕模板类型无效")
    return scope


def title_template_config(scope: str, values: dict[str, str] | None = None) -> dict[str, str]:
    scope = normalize_title_template_scope(scope)
    values = values or settings()
    legacy_prefix = "international_" if scope == "overseas" else ""
    result = {}
    for key in TITLE_TEMPLATE_KEYS:
        scoped = values.get(f"{scope}_{key}")
        if scoped is None and legacy_prefix:
            scoped = values.get(f"{legacy_prefix}{key}")
        result[key] = str(scoped if scoped is not None else values.get(key, ""))
    font_dir = str(result.get("title_font_dir") or "").strip()
    result["font_dir_available"] = "1" if not font_dir or Path(font_dir).is_dir() else "0"
    return result


def activate_title_template(scope: str) -> dict:
    """Activate one saved scope without changing the other scope."""
    scope = normalize_title_template_scope(scope)
    current = settings()
    config = title_template_config(scope, current)
    stamp = now()
    with connect() as conn:
        for key in TITLE_TEMPLATE_KEYS:
            conn.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, str(config.get(key, "")), stamp),
            )
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES('title_style_preset',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (scope, stamp),
        )
    return {"scope": scope, "config": title_template_config(scope), "settings": settings()}


def save_title_template(data: dict) -> dict:
    scope = normalize_title_template_scope(data.get("template_scope"))
    incoming = data.get("config")
    if not isinstance(incoming, dict):
        raise ValueError("标题字幕模板配置格式无效")
    current = settings()
    saved = title_template_config(scope, current)
    for key in TITLE_TEMPLATE_KEYS:
        if key in incoming:
            saved[key] = str(incoming.get(key, ""))
    stamp = now()
    with connect() as conn:
        for key in TITLE_TEMPLATE_KEYS:
            value = saved[key]
            conn.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (f"{scope}_{key}", value, stamp),
            )
            # The rendering pipeline continues to consume the established flat
            # keys; only the explicitly active scope is projected onto them.
            conn.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, stamp),
            )
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES('title_style_preset',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (scope, stamp),
        )
    return {"scope": scope, "config": title_template_config(scope), "settings": settings()}


def reset_title_template(scope: str) -> dict:
    scope = normalize_title_template_scope(scope)
    defaults = {
        "title_main": "", "title_sub": "", "title_font_dir": "",
        "title_font": "Microsoft YaHei", "title_font_size": "118",
        "title_color": "#FFE67A", "title_auto_fit": "0",
        "title_background_enabled": "0", "title_background_color": "#16B8B8",
        "title_background_opacity": "72", "title_background_padding_y": "22",
        "title_center": "1", "title_outline_width": "1",
        "title_outline_color": "#000000", "title_shadow_color": "#F15A24",
        "title_shadow_x": "2", "title_shadow_y": "2", "title_main_x": "50",
        "title_main_y": "13", "title_sub_x": "50", "title_sub_y": "21",
        "subtitle_enabled": "1", "subtitle_font": "Microsoft YaHei",
        "subtitle_font_size": "78", "subtitle_color": "#FFFFFF",
        "subtitle_outline_width": "1", "subtitle_outline_color": "#000000",
        "subtitle_margin_bottom": "446", "subtitle_max_chars_per_line": "14",
        "subtitle_effect": "none", "subtitle_effect_duration": "160",
    }
    if scope == "overseas":
        defaults.update({
            "title_font": "Arial", "title_font_size": "92", "title_auto_fit": "1",
            "subtitle_font": "Arial", "subtitle_font_size": "58",
            "subtitle_max_chars_per_line": "24", "subtitle_outline_width": "2",
        })
    return save_title_template({"template_scope": scope, "config": defaults})


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
            # Never expose the stored API secret or encrypted config blob to the UI.
            # The account screen only needs a configuration-state flag and the
            # read-only callback address.
            try:
                api_config = json.loads(str(account.get("api_config_json") or "{}"))
            except Exception:
                api_config = {}
            preflight = provider_preflight(str(account.get("platform_code") or ""), int(account["id"]))
            account["api_configured"] = bool(preflight["developer_configured"])
            account["oauth_redirect_uri"] = preflight["callback_uri"]
            account["oauth_state"] = preflight["status"]
            account.pop("api_config_json", None)
            account["authorization_status"] = authorization_status(int(account["id"]))["status"]
            account["login_status"] = "not_applicable"
    return accounts


def publishing_account_groups() -> list[dict]:
    """Return group summaries plus account rows for the account management UI."""
    groups = rows("SELECT * FROM publish_account_groups WHERE enabled=1 ORDER BY updated_at DESC,id DESC")
    members = rows(
        """SELECT m.group_id,m.account_id,m.enabled,m.sort_order,
                  a.account_name,a.login_status,a.authorization_status,a.last_checked_at,
                  p.code platform_code,p.name platform_name,p.capability_status,p.integration_type
           FROM publish_account_group_members m
           JOIN publish_accounts a ON a.id=m.account_id AND a.status='active'
           JOIN platforms p ON p.id=a.platform_id AND p.enabled=1
           WHERE m.enabled=1 ORDER BY m.group_id,m.sort_order,m.id"""
    )
    for group in groups:
        group_members = [item for item in members if int(item["group_id"]) == int(group["id"])]
        for item in group_members:
            allowed, error_code, reason = _group_member_gate(item)
            item.update({"publishable": allowed, "error_code": error_code, "blocked_reason": reason})
        group["members"] = group_members
        group["account_count"] = len(group_members)
        group["publishable_count"] = sum(1 for item in group_members if item.get("publishable"))
        group["needs_login_count"] = sum(1 for item in group_members if item.get("integration_type") == "browser" and item.get("login_status") != "logged_in")
        group["needs_auth_count"] = sum(1 for item in group_members if item.get("integration_type") == "api" and item.get("authorization_status") != "authorized")
        # 保留旧字段供旧客户端读取，但开发验收状态不再作为用户门禁。
        group["untested_count"] = 0
        group["platforms"] = list(dict.fromkeys(item.get("platform_name") or item.get("platform_code") for item in group_members))
    return groups


def save_account_group(data: dict, group_id: int | None = None) -> int:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("分组名称不能为空")
    account_ids = sorted({int(value) for value in data.get("account_ids", []) if int(value) > 0})
    stamp = now()
    with connect() as conn:
        duplicate = conn.execute(
            "SELECT id FROM publish_account_groups WHERE name=? AND id<>COALESCE(?,0)",
            (name, group_id or 0),
        ).fetchone()
        if duplicate:
            raise ValueError("已存在同名分组，请换一个名称")
        if group_id:
            if not conn.execute("SELECT 1 FROM publish_account_groups WHERE id=? AND enabled=1", (group_id,)).fetchone():
                raise ValueError("发布分组不存在")
            conn.execute("UPDATE publish_account_groups SET name=?,description=?,updated_at=? WHERE id=?", (name, str(data.get("description") or "").strip(), stamp, group_id))
        else:
            cursor = conn.execute("INSERT INTO publish_account_groups(name,description,enabled,created_at,updated_at) VALUES(?,?,1,?,?)", (name, str(data.get("description") or "").strip(), stamp, stamp))
            group_id = int(cursor.lastrowid)
        if account_ids:
            marks = ",".join("?" for _ in account_ids)
            existing = conn.execute(f"SELECT id FROM publish_accounts WHERE status='active' AND id IN ({marks})", tuple(account_ids)).fetchall()
            if len(existing) != len(account_ids):
                raise ValueError("部分账号不存在或已删除")
        conn.execute("DELETE FROM publish_account_group_members WHERE group_id=?", (group_id,))
        for order, account_id in enumerate(account_ids):
            conn.execute("INSERT INTO publish_account_group_members(group_id,account_id,enabled,sort_order,created_at) VALUES(?,?,1,?,?)", (group_id, account_id, order, stamp))
    return int(group_id)


def copy_account_group(group_id: int) -> int:
    source = row("SELECT * FROM publish_account_groups WHERE id=? AND enabled=1", (group_id,))
    if not source:
        raise ValueError("发布分组不存在")
    members = rows("SELECT account_id FROM publish_account_group_members WHERE group_id=? AND enabled=1 ORDER BY sort_order,id", (group_id,))
    base = f"{source['name']} 副本"
    name = base
    index = 2
    while row("SELECT id FROM publish_account_groups WHERE name=?", (name,)):
        name = f"{base}{index}"
        index += 1
    return save_account_group({"name": name, "description": source.get("description") or "", "account_ids": [item["account_id"] for item in members]})


def delete_account_group(group_id: int) -> None:
    with connect() as conn:
        if conn.execute(
            "UPDATE publish_account_groups SET enabled=0,updated_at=? WHERE id=? AND enabled=1",
            (now(), group_id),
        ).rowcount != 1:
            raise ValueError("账号分组不存在")
        conn.execute("DELETE FROM publish_account_group_members WHERE group_id=?", (group_id,))


def _group_member_gate(account: dict) -> tuple[bool, str, str]:
    if str(account.get("integration_type") or "browser") == "api":
        if str(account.get("authorization_status") or "") != "authorized":
            return False, "OAUTH_EXPIRED", "请先完成平台授权"
    elif str(account.get("login_status") or "") != "logged_in":
        return False, "AUTH_EXPIRED", "账号尚未登录"
    return True, "", ""


def _group_members(group_id: int, selected_account_ids=None) -> list[dict]:
    members = rows(
        """SELECT a.id account_id,a.account_name,a.platform_id,p.code platform_code,
                  p.name platform_name,p.capability_status,p.integration_type,
                  a.login_status,a.authorization_status
           FROM publish_account_group_members m
           JOIN publish_accounts a ON a.id=m.account_id AND a.status='active'
           JOIN platforms p ON p.id=a.platform_id AND p.enabled=1
           WHERE m.group_id=? AND m.enabled=1 ORDER BY m.sort_order,m.id""",
        (group_id,),
    )
    selected = {int(value) for value in (selected_account_ids or []) if int(value) > 0}
    if selected:
        members = [item for item in members if int(item["account_id"]) in selected]
    for item in members:
        allowed, error_code, reason = _group_member_gate(item)
        item.update({"publishable": allowed, "error_code": error_code, "blocked_reason": reason})
    return members


def group_publish_preflight(data: dict) -> dict:
    group_id = int(data.get("group_id") or 0)
    group = row("SELECT * FROM publish_account_groups WHERE id=? AND enabled=1", (group_id,))
    if not group:
        raise ValueError("账号分组不存在")
    members = _group_members(group_id, data.get("selected_account_ids"))
    if not members:
        raise ValueError("分组内没有可用账号")
    publishable = [item for item in members if item["publishable"]]
    blocked = [item for item in members if not item["publishable"]]
    return {
        "group": group, "members": members, "publishable": publishable, "blocked": blocked,
        "total_count": len(members), "publishable_count": len(publishable), "blocked_count": len(blocked),
    }


def _group_batch_key(data: dict, account_ids: list[int]) -> str:
    identity = {
        "group_id": int(data.get("group_id") or 0),
        "generation_task_id": int(data.get("generation_task_id") or 0),
        "account_ids": sorted(account_ids),
        "title": str(data.get("title") or "").strip(),
        "scheduled_at": str(data.get("scheduled_at") or "").strip().replace("T", " "),
    }
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _group_batch_result(batch_id: int, *, duplicate: bool = False) -> dict:
    batch = row("SELECT * FROM publish_group_batches WHERE id=?", (batch_id,)) or {}
    items = rows("SELECT * FROM publish_group_batch_items WHERE batch_id=? ORDER BY id", (batch_id,))
    task_ids = [int(item["task_id"]) for item in items if item.get("task_id")]
    return {
        "batch_id": batch_id, "batch": batch, "items": items, "task_ids": task_ids,
        "publishable": [item for item in items if item.get("task_id")],
        "blocked": [item for item in items if item.get("status") in {"skipped", "blocked"}],
        "duplicate": duplicate,
    }


def _create_group_publish_batch(data: dict) -> dict:
    preview = group_publish_preflight(data)
    group_id = int(preview["group"]["id"])
    member_rows = preview["members"]
    key = _group_batch_key(data, [int(item["account_id"]) for item in member_rows])
    duplicate = row("SELECT id FROM publish_group_batches WHERE idempotency_key=? ORDER BY id DESC LIMIT 1", (key,))
    if duplicate:
        return _group_batch_result(int(duplicate["id"]), duplicate=True)
    stamp = now()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO publish_group_batches(name,group_id,total_count,idempotency_key,status,created_at,updated_at)
               VALUES(?,?,?,?, 'pending',?,?)""",
            (preview["group"]["name"], group_id, len(member_rows), key, stamp, stamp),
        )
        batch_id = int(cursor.lastrowid)
    task_ids = []
    try:
        for account in member_rows:
            if not account["publishable"]:
                with connect() as conn:
                    conn.execute(
                        """INSERT INTO publish_group_batch_items(
                           batch_id,task_id,account_id,group_id,group_batch_id,
                           account_name_snapshot,platform_name_snapshot,platform_code_snapshot,
                           status,blocked_reason,error_code,created_at,updated_at)
                           VALUES(?,NULL,?,?,?,?,?,?, 'skipped',?,?,?,?)""",
                        (batch_id, account["account_id"], group_id, batch_id,
                         account["account_name"], account["platform_name"], account["platform_code"],
                         account["blocked_reason"], account["error_code"], stamp, stamp),
                    )
                continue
            payload = dict(data)
            payload["account_ids"] = [int(account["account_id"])]
            payload["platform"] = str(account["platform_code"] or "")
            payload["group_id"] = group_id
            payload["group_batch_id"] = batch_id
            task_id = create_publish_task(payload)
            task_ids.append(task_id)
            with connect() as conn:
                conn.execute("UPDATE publish_tasks SET group_id=?,group_batch_id=? WHERE id=?", (group_id, batch_id, task_id))
                task_state = conn.execute("SELECT status FROM publish_tasks WHERE id=?", (task_id,)).fetchone()["status"]
                conn.execute(
                    """INSERT INTO publish_group_batch_items(
                       batch_id,task_id,account_id,group_id,group_batch_id,
                       account_name_snapshot,platform_name_snapshot,platform_code_snapshot,
                       status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, task_id, account["account_id"], group_id, batch_id,
                     account["account_name"], account["platform_name"], account["platform_code"],
                     task_state, stamp, stamp),
                )
    except Exception:
        with connect() as conn:
            conn.execute("DELETE FROM publish_group_batches WHERE id=?", (batch_id,))
            for task_id in task_ids:
                conn.execute("DELETE FROM publish_tasks WHERE id=?", (task_id,))
        raise
    with connect() as conn:
        conn.execute("UPDATE publish_account_groups SET last_used_at=?,updated_at=? WHERE id=?", (stamp, stamp, group_id))
        recalculate_group_batch(conn, batch_id)
    return _group_batch_result(batch_id)


def create_group_publish_batch(data: dict) -> dict:
    with GROUP_BATCH_LOCK:
        return _create_group_publish_batch(data)

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
    # API developer credentials are provider-level configuration and are never
    # accepted through ordinary account creation.
    api_config = {}
    existing = row(
        "SELECT * FROM publish_accounts WHERE platform_id=? AND account_name=?",
        (platform_id, account_name),
    )
    if existing and existing.get("status") == "active":
        raise ValueError("该平台下已存在同名账号")
    if existing:
        account_id = int(existing["id"])
        path = (
            domestic_profile_path(platform["code"], account_id)
            if integration_type == "browser" and platform["code"] in DOMESTIC_LOGIN_PLATFORMS
            else profile_path(platform["code"], account_name, account_id) if integration_type == "browser" else ""
        )
        if path:
            ensure_profile(path)
        with connect() as conn:
            conn.execute(
                """UPDATE publish_accounts SET status='active',login_status='not_logged_in',
                    browser_profile_path=?,publishing_mode=?,authorization_status='not_authorized',
                    last_checked_at='',last_error='',updated_at=? WHERE id=?""",
                (str(path), integration_type, stamp, account_id),
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
            path = (
                domestic_profile_path(platform["code"], account_id)
                if integration_type == "browser" and platform["code"] in DOMESTIC_LOGIN_PLATFORMS
                else profile_path(platform["code"], account_name, account_id) if integration_type == "browser" else ""
            )
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


def start_account_login(account_id: int, login_mode: str = "qr") -> dict:
    platform, account = publishing_account_context(account_id)
    if platform.get("integration_type") == "api":
        return start_authorization(platform, account)
    platform_code = str(platform.get("code") or "")
    if platform_code in DOMESTIC_LOGIN_PLATFORMS:
        result = launch_domestic_login(
            account_id,
            platform_code,
            account["browser_profile_path"],
            initial_login_status=str(account.get("login_status") or "not_logged_in"),
        )
        return {
            **result,
            "status": str(result.get("status") or "starting"),
            "network_requirement": platform.get("network_requirement", ""),
        }
    with connect() as conn:
        conn.execute(
            "UPDATE publish_accounts SET login_status='login_pending',last_error='',updated_at=? WHERE id=?",
            (now(), account_id),
    )
    try:
        if platform_code == "tiktok":
            result = launch_tiktok_login(account_id, account["browser_profile_path"], login_mode)
        else:
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
    return {
        **result,
        "status": str(result.get("status") or "login_pending"),
        "network_requirement": platform.get("network_requirement", ""),
    }


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
        platform_code = str(platform.get("code") or "")
        if platform_code == "tiktok":
            result = probe_tiktok_login(account_id, account["browser_profile_path"])
        elif platform_code in DOMESTIC_LOGIN_PLATFORMS:
            result = probe_domestic_login(account_id, platform_code, account["browser_profile_path"])
        else:
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


_UPDATE_STATE_LOCK = threading.Lock()
_UPDATE_STATE = {"state": "idle", "message": "", "error": "", "version": ""}


def _set_update_state(state: str, message: str, *, error: str = "", version: str = "") -> None:
    with _UPDATE_STATE_LOCK:
        _UPDATE_STATE.update(state=state, message=message, error=error, version=version or _UPDATE_STATE.get("version", ""))


def update_status() -> dict:
    config = settings()
    with _UPDATE_STATE_LOCK:
        update_process = dict(_UPDATE_STATE)
    return {
        "current_version": current_version(),
        "auto_check_update": str(config.get("auto_check_update", "1")).lower() in ("1", "true", "on", "yes"),
        "configured": bool(configured_manifest_url(config)),
        "update_process": update_process,
    }


def check_configured_update() -> dict:
    url = configured_manifest_url(settings())
    if not url:
        raise UpdateError("尚未配置 update.json 的 HTTPS 地址")
    return check_for_update(url)


def apply_configured_update() -> dict:
    with _UPDATE_STATE_LOCK:
        if _UPDATE_STATE["state"] in {"preparing", "downloading", "backing_up", "launching"}:
            return dict(_UPDATE_STATE)
    result = check_configured_update()
    if not result.get("available"):
        raise UpdateError("当前已经是最新版本")
    manifest = UpdateManifest.from_dict(result["manifest"])
    UPDATE_SHUTDOWN_EVENT.clear()
    _set_update_state("preparing", "准备更新...", version=manifest.version)

    def run_update() -> None:
        stages = {
            "准备下载更新组件...": ("preparing", "准备更新组件..."),
            "正在下载新版程序...": ("downloading", "正在下载新版程序..."),
            "正在备份用户数据...": ("backing_up", "正在备份用户数据..."),
            "正在通知软件退出...": ("launching", "准备关闭程序..."),
        }

        def report_stage(message: str) -> None:
            state, text = stages.get(message, ("preparing", message))
            _set_update_state(state, text, version=manifest.version)

        try:
            stage_and_launch(manifest, status_callback=report_stage)
            _set_update_state("launching", "更新器已启动，正在关闭程序...", version=manifest.version)
            threading.Timer(0.5, UPDATE_SHUTDOWN_EVENT.set).start()
        except Exception as exc:
            _set_update_state("failed", "更新失败", error=str(exc), version=manifest.version)

    threading.Thread(target=run_update, name="mixcut-update-worker", daemon=True).start()
    return dict(_UPDATE_STATE)


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
    ffmpeg = find_ffmpeg()
    command = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=0xE4EAF4:s=1080x1920:d=1",
               "-vf", title_filter, "-frames:v", "1", "-q:v", "2", str(output_path)]
    proc = subprocess.run(
        command,
        capture_output=True,
        timeout=30,
        **background_process_kwargs(command, "title_preview_render"),
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
        task["heartbeat_state"] = heartbeat_state(task.get("last_heartbeat_at", ""))
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
        own_items = [i for i in batch_items if i["batch_id"] == batch["id"]]
        batch["progress"] = round(
            sum(float(i.get("progress") or 0) for i in own_items)
            / max(1, int(batch.get("total_count") or len(own_items) or 1))
        )
    bgms = rows("SELECT * FROM bgm_library ORDER BY created_at DESC,id DESC")
    transitions = rows("SELECT * FROM shot_transitions ORDER BY from_shot_id,to_shot_id")
    sound_effects = rows("SELECT * FROM sound_effect_library ORDER BY category,name,id")
    ai_services = [public_service_config(item) for item in rows("SELECT * FROM ai_service_configs ORDER BY is_default DESC,id")]
    tts_services = [public_service_config(item) for item in rows("SELECT * FROM tts_service_configs ORDER BY is_default DESC,id")]
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
    publish_account_groups = publishing_account_groups()
    publish_group_batches = rows("SELECT b.*,COALESCE(g.name,b.name) group_name FROM publish_group_batches b LEFT JOIN publish_account_groups g ON g.id=b.group_id ORDER BY b.id DESC LIMIT 100")
    publish_group_batch_items = rows(
        """SELECT i.*,
                  COALESCE(NULLIF(i.account_name_snapshot,''),a.account_name,'已删除账号') account_name,
                  COALESCE(NULLIF(i.platform_name_snapshot,''),p.name,'未知平台') platform_name,
                  COALESCE(NULLIF(i.platform_code_snapshot,''),p.code,'') platform_code
           FROM publish_group_batch_items i
           LEFT JOIN publish_accounts a ON a.id=i.account_id
           LEFT JOIN platforms p ON p.id=a.platform_id
           ORDER BY i.batch_id DESC,i.id"""
    )
    attach_and_schedule(
        tasks,
        {int(item["generation_task_id"]) for item in publish_tasks if item.get("generation_task_id")},
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
            "publish_account_groups": publish_account_groups, "publish_group_batches": publish_group_batches,
            "publish_group_batch_items": publish_group_batch_items,
            "platform_test_runs": platform_test_runs, "admin_mode": is_admin_mode(),
            "media_runtime": get_media_runtime_status(),
            "video_acceleration": video_acceleration_status(),
            "settings": public_settings(), "app_version": current_version(), "db_path": str(DB_PATH)}


def dashboard_task_snapshot():
    """Small task-only payload used by the workbench's 3-second refresh."""
    summary = row(
        """SELECT
        SUM(CASE WHEN status IN ('preparing','rendering','verifying') THEN 1 ELSE 0 END) running_count,
        SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) queued_count,
        SUM(CASE WHEN status='succeeded' AND substr(updated_at,1,10)=date('now','localtime') THEN 1 ELSE 0 END) today_completed_count,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_count,
        SUM(CASE WHEN status IN ('stalled','needs_manual_action','interrupted') THEN 1 ELSE 0 END) needs_action_count
        FROM generation_tasks"""
    ) or {}
    active = rows(
        """SELECT g.id,g.task_name,g.status,g.progress,g.current_shot,g.total_shots,g.updated_at,
        COALESCE(src.name,t.name) template_name
        FROM generation_tasks g
        LEFT JOIN edit_templates t ON t.id=g.template_id
        LEFT JOIN edit_templates src ON src.id=t.source_template_id
        WHERE g.status IN ('queued','preparing','rendering','verifying','stalled','needs_manual_action','interrupted')
        ORDER BY CASE WHEN g.status IN ('stalled','needs_manual_action','interrupted') THEN 0
                      WHEN g.status IN ('preparing','rendering','verifying') THEN 1 ELSE 2 END,
                 g.updated_at DESC,g.id DESC LIMIT 12"""
    )
    current = next((item for item in active if item["status"] in {"preparing", "rendering", "verifying"}), None)
    queued = [item for item in active if item["status"] == "queued"][:2]
    return {
        "running_count": int(summary.get("running_count") or 0),
        "queued_count": int(summary.get("queued_count") or 0),
        "today_completed_count": int(summary.get("today_completed_count") or 0),
        "failed_count": int(summary.get("failed_count") or 0),
        "needs_action_count": int(summary.get("needs_action_count") or 0),
        "current": current,
        "queued": queued,
    }


def _task_center_union_sql() -> str:
    """Return authoritative batch rows without eagerly loading child details."""
    return """
        SELECT 'batch' record_type,b.id record_id,b.template_id,
               COALESCE(NULLIF(b.name,''),COALESCE(src.name,t.name,'\u672a\u547d\u540d\u9879\u76ee') || ' \u00b7 \u6279\u91cf\u751f\u6210' || b.total_count || '\u6761') display_name,
               COALESCE(src.name,t.name,'\u672a\u547d\u540d\u9879\u76ee') project_name,
               b.total_count,
               COALESCE((SELECT SUM(CASE WHEN bi.status='succeeded' THEN 1 ELSE 0 END)
                         FROM batch_task_items bi WHERE bi.batch_id=b.id),0) completed_count,
               COALESCE((SELECT SUM(CASE WHEN bi.status='failed' THEN 1 ELSE 0 END)
                         FROM batch_task_items bi WHERE bi.batch_id=b.id),0) failed_count,
               COALESCE((SELECT SUM(CASE WHEN bi.status='queued' THEN 1 ELSE 0 END)
                         FROM batch_task_items bi WHERE bi.batch_id=b.id),0) queued_count,
               COALESCE((SELECT SUM(CASE WHEN bi.status IN ('preparing','rendering','verifying') THEN 1 ELSE 0 END)
                         FROM batch_task_items bi WHERE bi.batch_id=b.id),0) running_count,
               COALESCE((SELECT SUM(CASE WHEN bi.status IN ('stalled','paused') THEN 1 ELSE 0 END)
                         FROM batch_task_items bi WHERE bi.batch_id=b.id),0) needs_action_count,
               b.status,b.created_at,b.updated_at,b.finished_at,
               COALESCE((SELECT ROUND(AVG(CASE WHEN g.status IN ('succeeded','completed') THEN 100 ELSE COALESCE(g.progress,0) END),0)
                         FROM batch_task_items bi JOIN generation_tasks g ON g.id=bi.task_id
                         WHERE bi.batch_id=b.id),0) progress_percent
        FROM batch_tasks b
        LEFT JOIN edit_templates t ON t.id=b.template_id
        LEFT JOIN edit_templates src ON src.id=t.source_template_id
        UNION ALL
        SELECT 'single' record_type,g.id record_id,g.template_id,g.task_name display_name,
               COALESCE(src.name,t.name,g.task_name,'\u672a\u547d\u540d\u9879\u76ee') project_name,
               1 total_count,CASE WHEN g.status IN ('succeeded','completed') THEN 1 ELSE 0 END completed_count,
               CASE WHEN g.status='failed' THEN 1 ELSE 0 END failed_count,
               CASE WHEN g.status IN ('queued','pending') THEN 1 ELSE 0 END queued_count,
               CASE WHEN g.status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN 1 ELSE 0 END running_count,
               CASE WHEN g.status IN ('stalled','paused','needs_manual_action','interrupted') THEN 1 ELSE 0 END needs_action_count,
               g.status,g.created_at,g.updated_at,
               CASE WHEN g.status IN ('succeeded','completed') THEN g.updated_at ELSE g.finished_at END finished_at,
               CASE WHEN g.status IN ('succeeded','completed') THEN 100 ELSE COALESCE(g.progress,0) END progress_percent
        FROM generation_tasks g
        LEFT JOIN edit_templates t ON t.id=g.template_id
        LEFT JOIN edit_templates src ON src.id=t.source_template_id
        WHERE NOT EXISTS(SELECT 1 FROM batch_task_items bi WHERE bi.task_id=g.id)
          AND NOT EXISTS(
              SELECT 1 FROM generation_tasks newer
              JOIN batch_task_items current_item ON current_item.task_id=newer.id
              WHERE newer.retry_of_task_id=g.id
          )
    """


def task_center_list(view: str = "current", page: int = 1, page_size: int = TASK_CENTER_PAGE_SIZE,
                     status: str = "", project_id: int = 0, date_from: str = "", date_to: str = "",
                     keyword: str = "") -> dict:
    view = view if view in {"current", "recent", "history"} else "current"
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or TASK_CENTER_PAGE_SIZE)))
    filters, args = [], []
    active_marks = ",".join("?" for _ in TASK_CENTER_ACTIVE_STATUSES)
    if view == "current":
        filters.append(f"status IN ({active_marks})")
        args.extend(sorted(TASK_CENTER_ACTIVE_STATUSES))
    elif view == "recent":
        filters.extend(["status IN ('succeeded','partially_succeeded','completed')", "datetime(COALESCE(NULLIF(finished_at,''),updated_at)) >= datetime('now','localtime','-7 days')"])
    if project_id:
        filters.append("template_id=?")
        args.append(project_id)
    if status:
        filters.append("status=?")
        args.append(status)
    if date_from:
        filters.append("date(created_at)>=date(?)")
        args.append(date_from)
    if date_to:
        filters.append("date(created_at)<=date(?)")
        args.append(date_to)
    if keyword:
        filters.append("(display_name LIKE ? OR project_name LIKE ?)")
        value = f"%{keyword.strip()}%"
        args.extend([value, value])
    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    base = f"SELECT * FROM ({_task_center_union_sql()}) task_center{where}"
    if view == "current":
        ordering = " ORDER BY CASE WHEN status IN ('needs_action','failed','stalled','needs_manual_action','interrupted') THEN 0 WHEN status IN ('running','preparing','rendering','verifying','partially_blocked') THEN 1 WHEN status IN ('queued','pending') THEN 2 WHEN status='paused' THEN 3 ELSE 4 END,datetime(created_at) DESC,record_id DESC"
    else:
        ordering = " ORDER BY datetime(COALESCE(NULLIF(finished_at,''),updated_at)) DESC,record_id DESC"
    with connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) count FROM ({base})", args).fetchone()["count"])
        if view == "recent":
            limit, offset = RECENT_COMPLETED_LIMIT, 0
        else:
            limit, offset = page_size, (page - 1) * page_size
        result_rows = [dict(item) for item in conn.execute(base + ordering + " LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()]
        summary_row = conn.execute(
            """SELECT
               SUM(CASE WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN 1 ELSE 0 END) running_count,
               SUM(CASE WHEN status IN ('queued','pending') THEN 1 ELSE 0 END) queued_count,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_count,
               SUM(CASE WHEN status IN ('stalled','paused','needs_manual_action','interrupted') THEN 1 ELSE 0 END) needs_action_count
               FROM generation_tasks g
               WHERE EXISTS(SELECT 1 FROM batch_task_items current_item WHERE current_item.task_id=g.id)
                  OR (
                    NOT EXISTS(SELECT 1 FROM batch_task_items any_item WHERE any_item.task_id=g.id)
                    AND NOT EXISTS(
                      SELECT 1 FROM generation_tasks newer
                      JOIN batch_task_items newer_item ON newer_item.task_id=newer.id
                      WHERE newer.retry_of_task_id=g.id
                    )
                  )"""
        ).fetchone()
    return {
        "view": view, "items": result_rows, "page": page, "page_size": limit,
        "total": total, "total_pages": max(1, (total + page_size - 1) // page_size),
        "recent_limit": RECENT_COMPLETED_LIMIT,
        "summary": {key: int(summary_row[key] or 0) for key in ("running_count", "queued_count", "failed_count", "needs_action_count")},
    }


def task_center_items(record_type: str, record_id: int) -> list[dict]:
    if record_type not in {"batch", "single"}:
        raise ValueError("任务批次类型无效")
    condition = "bi.batch_id=?" if record_type == "batch" else "g.id=?"
    join = "JOIN batch_task_items bi ON bi.task_id=g.id" if record_type == "batch" else "LEFT JOIN batch_task_items bi ON bi.task_id=g.id"
    items = rows(
        f"""SELECT g.*,bi.batch_id,bi.index_no,COALESCE(src.name,t.name,g.task_name) template_name,
                    COALESCE((SELECT SUM(s.voice_duration) FROM template_shots s WHERE s.template_id=g.template_id),0) output_duration
             FROM generation_tasks g {join}
             LEFT JOIN edit_templates t ON t.id=g.template_id
             LEFT JOIN edit_templates src ON src.id=t.source_template_id
             WHERE {condition} ORDER BY COALESCE(bi.index_no,1),g.id""", (record_id,)
    )
    for item in items:
        item["status"] = canonical_task_status(item.get("status"))
        item["output_url"] = media_url(item.get("output_path", ""))
        item["output_exists"] = bool(item.get("output_path") and Path(item["output_path"]).is_file())
        item["heartbeat_state"] = heartbeat_state(item.get("last_heartbeat_at", ""))
        item.update(task_health_snapshot(int(item["id"])))
    return items


def _flat_task_base_sql() -> str:
    return """
        SELECT g.id,g.template_id,g.task_name,g.status,g.progress,g.output_path,g.error_message,
               g.created_at,g.updated_at,g.started_at,g.last_heartbeat_at,g.current_stage,
               g.current_step,g.finished_at,g.failed_stage,g.error_code,
               bi.batch_id,bi.index_no,b.name batch_name,b.total_count batch_total,
               b.status batch_status,COALESCE(src.id,t.id,g.template_id) project_id,
               COALESCE(src.name,t.name,g.task_name,'未命名项目') project_name
        FROM generation_tasks g
        LEFT JOIN batch_task_items bi ON bi.task_id=g.id
        LEFT JOIN batch_tasks b ON b.id=bi.batch_id
        LEFT JOIN edit_templates t ON t.id=g.template_id
        LEFT JOIN edit_templates src ON src.id=t.source_template_id
        WHERE NOT EXISTS(SELECT 1 FROM generation_tasks newer WHERE newer.retry_of_task_id=g.id)
    """


def task_center_flat_list(view: str = "current", page: int = 1, page_size: int = 20,
                          status: str = "", project_id: int = 0, batch_id: int = 0,
                          date_from: str = "", date_to: str = "", keyword: str = "",
                          focus_task_id: int = 0) -> dict:
    """Return one real MP4 generation job per row; batches are metadata only."""
    view = view if view in {"current", "recent", "history"} else "current"
    page, page_size = max(1, int(page or 1)), max(1, min(100, int(page_size or 20)))
    filters, args = [], []
    if view == "current":
        filters.append("status IN ('queued','pending','preparing','rendering','verifying','running','processing','processing_shot','merging','failed','stalled','paused','needs_action','needs_manual_action','interrupted')")
    elif view == "recent":
        filters.append("status IN ('succeeded','completed')")
    if status:
        aliases = {
            "running": ("preparing", "rendering", "verifying", "running", "processing", "processing_shot", "merging"),
            "queued": ("queued", "pending"), "needs_action": ("stalled", "paused", "needs_action", "needs_manual_action", "interrupted"),
            "succeeded": ("succeeded", "completed"),
        }
        values = aliases.get(status, (status,))
        marks = ",".join("?" for _ in values)
        filters.append(f"status IN ({marks})")
        args.extend(values)
    if project_id:
        filters.append("project_id=?")
        args.append(int(project_id))
    if batch_id:
        filters.append("batch_id=?")
        args.append(int(batch_id))
    if date_from:
        filters.append("date(created_at)>=date(?)")
        args.append(date_from)
    if date_to:
        filters.append("date(created_at)<=date(?)")
        args.append(date_to)
    if keyword:
        value = f"%{keyword.strip()}%"
        filters.append("(task_name LIKE ? OR project_name LIKE ? OR COALESCE(batch_name,'') LIKE ?)")
        args.extend((value, value, value))
    base = f"SELECT * FROM ({_flat_task_base_sql()}) flat_tasks"
    if filters:
        base += " WHERE " + " AND ".join(filters)
    focus_order = "CASE WHEN id=? THEN -1 ELSE 0 END," if focus_task_id else ""
    order = f""" ORDER BY {focus_order}
        CASE WHEN status IN ('stalled','paused','needs_action','needs_manual_action','interrupted') THEN 0
             WHEN status='failed' THEN 1
             WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN 2
             WHEN status IN ('queued','pending') THEN 3
             WHEN status IN ('succeeded','completed') THEN 4 ELSE 5 END,
        CASE WHEN batch_id IS NULL THEN created_at ELSE COALESCE((SELECT created_at FROM batch_tasks bx WHERE bx.id=batch_id),created_at) END,
        COALESCE(batch_id,999999999),COALESCE(index_no,1),id"""
    if view in {"recent", "history"}:
        order = f" ORDER BY {focus_order} datetime(COALESCE(NULLIF(finished_at,''),updated_at)) DESC,id DESC"
    order_args = (int(focus_task_id),) if focus_task_id else ()
    with connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) count FROM ({base})", args).fetchone()["count"])
        summary_row = conn.execute(
            f"""SELECT
                SUM(CASE WHEN status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging') THEN 1 ELSE 0 END) running_count,
                SUM(CASE WHEN status IN ('queued','pending') THEN 1 ELSE 0 END) queued_count,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_count,
                SUM(CASE WHEN status IN ('stalled','paused','needs_action','needs_manual_action','interrupted') THEN 1 ELSE 0 END) needs_action_count
                FROM ({base})""",
            args,
        ).fetchone()
        limit = RECENT_COMPLETED_LIMIT if view == "recent" else page_size
        offset = 0 if view == "recent" else (page - 1) * page_size
        result = [dict(item) for item in conn.execute(
            base + order + " LIMIT ? OFFSET ?", (*args, *order_args, limit, offset)
        ).fetchall()]
    queue = get_generation_queue_status()
    blocking_id = int((queue.get("blocking_task") or {}).get("id") or 0)
    running_id = int((queue.get("running_task") or {}).get("id") or 0)
    next_id = int((queue.get("next_task") or {}).get("id") or 0)
    queue_positions = {
        int(item["id"]): index
        for index, item in enumerate(queue.get("queued_tasks") or [], start=1)
    }
    for item in result:
        item["status"] = canonical_task_status(item.get("status"))
        item["error_summary"] = str(item.get("error_message") or "")
        item["is_blocking"] = int(item["id"]) == blocking_id
        item["is_running"] = int(item["id"]) == running_id
        item["is_next"] = int(item["id"]) == next_id
        item["output_exists"] = bool(item.get("output_path") and Path(str(item["output_path"])).is_file())
        item["output_url"] = media_url(str(item.get("output_path") or ""))
        if item["status"] == "queued":
            item["queue_position"] = queue_positions.get(int(item["id"]), 0)
            item["display_step"] = (
                f"队列第 {item['queue_position']} 位 · 等待前一条完成"
                if item["queue_position"] else "等待生成队列"
            )
        else:
            item["display_step"] = item.get("current_step") or item.get("current_shot") or item.get("current_stage") or "—"
    return {
        "view": view, "items": result, "page": page, "page_size": limit, "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size), "queue": queue,
        "summary": {
            key: int(summary_row[key] or 0)
            for key in ("running_count", "queued_count", "failed_count", "needs_action_count")
        },
    }


def _queue_task_row(record) -> dict | None:
    if not record:
        return None
    item = dict(record)
    item["status"] = canonical_task_status(item.get("status"))
    item["error_summary"] = str(item.get("error_message") or "")
    return item


def get_generation_queue_status() -> dict:
    """Describe the single global generation slot and the task blocking it."""
    runtime = generation_worker_snapshot()
    active_marks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
    with connect() as conn:
        queue_order = (
            "CASE WHEN COALESCE(g.queue_seq,0)>0 THEN g.queue_seq ELSE g.id END,g.id"
            if generation_queue_seq_supported(conn) else "g.id"
        )
        running = conn.execute(
            f"""SELECT g.id,g.task_name,g.status,g.progress,g.current_step,g.current_stage,g.created_at,
                       bi.batch_id,bi.index_no,b.name batch_name,b.total_count batch_total,g.error_message
                FROM generation_tasks g LEFT JOIN batch_task_items bi ON bi.task_id=g.id
                LEFT JOIN batch_tasks b ON b.id=bi.batch_id
                WHERE g.status IN ({active_marks})
                ORDER BY {queue_order} LIMIT 1""",
            tuple(sorted(ACTIVE_TASK_STATUSES))
        ).fetchone()
        queued_count = int(conn.execute(
            "SELECT COUNT(*) count FROM generation_tasks WHERE status IN ('queued','pending')"
        ).fetchone()["count"])
        queued_tasks = conn.execute(
            f"""SELECT g.id,g.task_name,g.status,g.progress,g.current_step,g.current_stage,g.created_at,
                      bi.batch_id,bi.index_no,b.name batch_name,b.total_count batch_total,g.error_message
               FROM generation_tasks g LEFT JOIN batch_task_items bi ON bi.task_id=g.id
               LEFT JOIN batch_tasks b ON b.id=bi.batch_id
               WHERE g.status IN ('queued','pending')
               ORDER BY {queue_order}"""
        ).fetchall()
        blocking = conn.execute(
            """SELECT g.id,g.task_name,g.status,g.progress,g.current_step,g.current_stage,g.created_at,
                      bi.batch_id,bi.index_no,b.name batch_name,b.total_count batch_total,g.error_message,g.error_code
               FROM generation_tasks g JOIN batch_task_items bi ON bi.task_id=g.id
               JOIN batch_tasks b ON b.id=bi.batch_id
               WHERE b.status IN ('needs_action','partially_blocked')
                 AND g.status IN ('failed','stalled','paused','needs_action','needs_manual_action','interrupted')
                 AND EXISTS(SELECT 1 FROM batch_task_items later JOIN generation_tasks q ON q.id=later.task_id
                            WHERE later.batch_id=bi.batch_id AND later.index_no>bi.index_no AND q.status IN ('queued','pending'))
               ORDER BY b.created_at,b.id,bi.index_no LIMIT 1"""
        ).fetchone()
    running_item = _queue_task_row(running)
    queued_items = [_queue_task_row(item) for item in queued_tasks]
    next_item = queued_items[0] if queued_items else None
    blocking_item = _queue_task_row(blocking)
    reason = ""
    if blocking_item:
        reason = blocking_item.get("error_summary") or "前一条任务失败，需要处理后才能继续"
    elif queued_count and not running_item:
        reason = "当前存在排队任务，但生成Worker没有运行"
    state = "running" if running_item else "blocked" if blocking_item else "waiting" if queued_count else "idle"
    return {
        "state": state, "running_task": running_item, "next_task": next_item,
        "queued_count": queued_count, "blocking_task": blocking_item,
        "queued_tasks": queued_items,
        "blocking_reason": reason, "worker_status": runtime,
        "batch_worker_status": BATCH_QUEUE.snapshot(),
        "max_concurrency": MAX_GENERATION_CONCURRENCY,
    }


def ensure_generation_worker_running() -> dict:
    """Keep both queue coordinators alive; safe to call repeatedly."""
    generation_restarted = GENERATION_QUEUE.ensure_started()
    batch_restarted = BATCH_QUEUE.ensure_started()
    GENERATION_QUEUE.signal()
    BATCH_QUEUE.signal()
    if generation_restarted or batch_restarted:
        append_generation_worker_log(
            "Watchdog restarted workers | generation="
            f"{int(generation_restarted)} | batch={int(batch_restarted)}"
        )
    return {
        "generation": GENERATION_QUEUE.snapshot(),
        "batch": BATCH_QUEUE.snapshot(),
    }


def _wake_earliest_durable_task() -> int:
    """Wake the owner of the oldest queued DB row without bypassing batch policy."""
    with connect() as conn:
        queue_seq_enabled = generation_queue_seq_supported(conn)
        order_expr = (
            "CASE WHEN COALESCE(queue_seq,0)>0 THEN queue_seq ELSE id END,id"
            if queue_seq_enabled else "id"
        )
        joined_order_expr = (
            "CASE WHEN COALESCE(g.queue_seq,0)>0 THEN g.queue_seq ELSE g.id END,g.id"
            if queue_seq_enabled else "g.id"
        )
        active = conn.execute(
            f"""SELECT id FROM generation_tasks
               WHERE status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging')
               ORDER BY {order_expr} LIMIT 1"""
        ).fetchone()
        queued = conn.execute(
            f"""SELECT g.id,i.batch_id
               FROM generation_tasks g
               LEFT JOIN batch_task_items i ON i.task_id=g.id
               WHERE g.status IN ('queued','pending')
               ORDER BY {joined_order_expr} LIMIT 1"""
        ).fetchone()
    if active or not queued:
        return 0
    task_id = int(queued["id"])
    batch_id = int(queued["batch_id"] or 0)
    if batch_id:
        BATCH_QUEUE.enqueue(batch_id, start_batch_generation)
    else:
        start_generate(task_id)
    append_generation_worker_log(
        f"Watchdog wake durable task #{task_id}" + (f" | batch #{batch_id}" if batch_id else "")
    )
    return task_id


def _generation_watchdog_loop() -> None:
    append_generation_worker_log("Generation watchdog started")
    while not _GENERATION_WATCHDOG_STOP.wait(GENERATION_WATCHDOG_INTERVAL_SECONDS):
        try:
            ensure_generation_worker_running()
            _wake_earliest_durable_task()
        except BaseException as exc:
            append_generation_worker_log(
                f"Watchdog error · {type(exc).__name__}: {exc}"
            )
    append_generation_worker_log("Generation watchdog stopped")


def start_generation_watchdog() -> bool:
    """Start one process-wide self-healing watchdog."""
    global _GENERATION_WATCHDOG_THREAD
    with _GENERATION_WATCHDOG_LOCK:
        if _GENERATION_WATCHDOG_THREAD and _GENERATION_WATCHDOG_THREAD.is_alive():
            return False
        _GENERATION_WATCHDOG_STOP.clear()
        _GENERATION_WATCHDOG_THREAD = threading.Thread(
            target=_generation_watchdog_loop,
            name="mixcut-generation-watchdog",
            daemon=True,
        )
        _GENERATION_WATCHDOG_THREAD.start()
    return True


def scan_invalid_generation_tasks(apply: bool = False) -> dict:
    """Audit stale runtime state without deleting output files or user records."""
    findings = []
    with connect() as conn:
        candidates = conn.execute(
            """SELECT id,task_name,status,template_id,last_heartbeat_at,worker_id,process_id,
                      ffmpeg_pid,ffprobe_pid,output_path FROM generation_tasks
               WHERE status IN ('running','processing','processing_shot','merging','preparing','rendering','verifying','queued','pending','cancelled','succeeded','completed')"""
        ).fetchall()
        for record in candidates:
            status = canonical_task_status(record["status"])
            health = task_health_snapshot(int(record["id"]))
            if status in {"preparing", "rendering", "verifying"}:
                age = health.get("heartbeat_age_seconds")
                if age is not None and age > 180 and not any(health.get(key) for key in ("worker_alive", "process_alive", "ffmpeg_alive", "ffprobe_alive")):
                    findings.append({"task_id": int(record["id"]), "type": "worker_missing", "message": "任务已超过180秒没有心跳，且Worker与FFmpeg均不存在", "fix": "stalled"})
            if status == "queued" and record["template_id"] and not conn.execute("SELECT 1 FROM edit_templates WHERE id=?", (int(record["template_id"]),)).fetchone():
                findings.append({"task_id": int(record["id"]), "type": "project_missing", "message": "排队任务引用的项目已经不存在", "fix": "cancelled"})
            if status == "succeeded" and record["output_path"] and not Path(str(record["output_path"])).is_file():
                findings.append({"task_id": int(record["id"]), "type": "output_missing", "message": "成片记录存在，但输出文件已经不存在", "fix": "keep_record"})
        batches = conn.execute(
            """SELECT b.id,b.name,b.total_count,b.status,COUNT(i.id) item_count
               FROM batch_tasks b LEFT JOIN batch_task_items i ON i.batch_id=b.id
               WHERE b.status IN ('queued','running','needs_action','partially_blocked')
               GROUP BY b.id HAVING COUNT(i.id)=0 OR COUNT(i.id)<>b.total_count"""
        ).fetchall()
        for batch in batches:
            expected = int(batch["total_count"] or 0)
            actual = int(batch["item_count"] or 0)
            message = (
                f"批次应有{expected}条任务，当前只有{actual}条"
                if expected else "历史批次只有父记录，没有任何视频子任务"
            )
            findings.append({"batch_id": int(batch["id"]), "type": "batch_item_missing", "message": message, "fix": "needs_action"})
        fixed = 0
        if apply:
            for item in findings:
                if item.get("task_id") and item["fix"] == "stalled":
                    conn.execute(
                        """UPDATE generation_tasks SET status='stalled',current_stage='stalled',current_step='任务已失去生成Worker',
                           current_shot='任务已失去生成Worker',worker_id='',process_id=0,process_name='',ffmpeg_pid=0,ffprobe_pid=0,
                           error_code='WORKER_STALLED',error_message=?,updated_at=? WHERE id=?""",
                        (item["message"], now(), int(item["task_id"])),
                    ); fixed += 1
                elif item.get("task_id") and item["fix"] == "cancelled":
                    conn.execute(
                        "UPDATE generation_tasks SET status='cancelled',current_stage='cancelled',current_step='项目已不存在',error_code='PROJECT_MISSING',error_message=?,updated_at=? WHERE id=?",
                        (item["message"], now(), int(item["task_id"])),
                    ); fixed += 1
                elif item.get("batch_id") and item["fix"] == "needs_action":
                    conn.execute("UPDATE batch_tasks SET status='needs_action',updated_at=? WHERE id=?", (now(), int(item["batch_id"]))); fixed += 1
            valid_batches = [int(r["id"]) for r in conn.execute(
                "SELECT b.id FROM batch_tasks b WHERE (SELECT COUNT(*) FROM batch_task_items i WHERE i.batch_id=b.id)=b.total_count"
            ).fetchall()]
        else:
            fixed, valid_batches = 0, []
    if apply:
        for batch_id_value in valid_batches:
            sync_batch(batch_id_value)
    summary = {}
    for item in findings:
        summary[item["type"]] = summary.get(item["type"], 0) + 1
    return {"count": len(findings), "fixed": fixed, "summary": summary, "items": findings}


def repair_generation_queue() -> dict:
    audit = scan_invalid_generation_tasks(apply=True)
    queue = get_generation_queue_status()
    if queue.get("running_task"):
        return {"repaired": True, "started": False, "message": "当前生成任务运行正常", "audit": audit, "queue": queue}
    if queue.get("blocking_task"):
        return {"repaired": False, "started": False, "message": "队列仍被失败任务阻塞，请重试、跳过或删除并继续", "audit": audit, "queue": queue}
    next_task = queue.get("next_task")
    if not next_task:
        return {"repaired": True, "started": False, "message": "当前没有等待生成的任务", "audit": audit, "queue": queue}
    batch_id_value = int(next_task.get("batch_id") or 0)
    if batch_id_value:
        BATCH_QUEUE.enqueue(batch_id_value, start_batch_generation)
    else:
        start_generate(int(next_task["id"]))
    return {"repaired": True, "started": True, "task_id": int(next_task["id"]), "message": "已恢复队列，并只启动第一条任务", "audit": audit, "queue": get_generation_queue_status()}


def delete_generation_task_records(task_ids: list[int], delete_outputs: bool = False) -> dict:
    deleted, skipped = [], []
    with connect() as conn:
        affected_batches = set()
        for task_id in sorted({int(value) for value in task_ids if int(value) > 0}):
            task = conn.execute("SELECT status,output_path FROM generation_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                continue
            status = canonical_task_status(task["status"])
            if status in {"preparing", "rendering", "verifying"}:
                skipped.append({"task_id": task_id, "reason": "生成中的任务必须先停止"})
                continue
            batch = conn.execute("SELECT batch_id FROM batch_task_items WHERE task_id=?", (task_id,)).fetchone()
            if batch:
                affected_batches.add(int(batch["batch_id"]))
            if delete_outputs and task["output_path"]:
                path = Path(str(task["output_path"]))
                if path.is_file():
                    path.unlink()
            conn.execute("DELETE FROM generation_shot_materials WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM generation_tasks WHERE id=?", (task_id,))
            deleted.append(task_id)
        for batch_id_value in affected_batches:
            count = int(conn.execute("SELECT COUNT(*) count FROM batch_task_items WHERE batch_id=?", (batch_id_value,)).fetchone()["count"])
            conn.execute("UPDATE batch_tasks SET total_count=?,updated_at=? WHERE id=?", (count, now(), batch_id_value))
    for batch_id_value in affected_batches:
        sync_batch(batch_id_value)
    return {"deleted": deleted, "skipped": skipped, "deleted_outputs": bool(delete_outputs)}


def cleanup_generation_task_records(statuses: list[str], age: str = "all", delete_outputs: bool = False) -> dict:
    allowed = {"succeeded", "completed", "failed", "cancelled", "stalled", "paused", "needs_action", "needs_manual_action", "interrupted"}
    selected = [value for value in statuses if value in allowed]
    if not selected:
        raise ValueError("请选择需要清理的任务状态")
    marks = ",".join("?" for _ in selected)
    sql, args = f"SELECT id FROM generation_tasks WHERE status IN ({marks})", list(selected)
    if age in {"7", "30"}:
        sql += " AND datetime(updated_at)<datetime('now','localtime',?)"
        args.append(f"-{int(age)} days")
    task_ids = [int(item["id"]) for item in rows(sql, args)]
    result = delete_generation_task_records(task_ids, delete_outputs)
    result["matched"] = len(task_ids)
    return result


def delete_generation_task_and_continue(task_id: int) -> dict:
    result = skip_generation_task_and_continue(int(task_id))
    with connect() as conn:
        conn.execute(
            "UPDATE generation_tasks SET error_code='ARCHIVED_AND_CONTINUED',error_message='用户已删除此失败任务并继续队列',updated_at=? WHERE id=?",
            (now(), int(task_id)),
        )
    append_task_log(int(task_id), "用户选择删除并继续；失败记录已归档，队列继续")
    result["archived"] = True
    return result


def delete_task_center_records(records: list[dict], delete_outputs: bool = False) -> dict:
    deleted = 0
    with connect() as conn:
        for record in records:
            record_type, record_id = str(record.get("record_type") or ""), int(record.get("record_id") or 0)
            if record_type == "batch":
                batch_state = conn.execute("SELECT status FROM batch_tasks WHERE id=?", (record_id,)).fetchone()
                if not batch_state: continue
                if batch_state["status"] in TASK_CENTER_ACTIVE_STATUSES - {"failed", "completed_with_errors", "needs_action", "needs_manual_action", "paused"}:
                    raise ValueError("运行中或排队中的批次不能删除")
                task_rows = conn.execute("SELECT g.id,g.status,g.output_path FROM generation_tasks g JOIN batch_task_items bi ON bi.task_id=g.id WHERE bi.batch_id=?", (record_id,)).fetchall()
                if any(item["status"] in TASK_CENTER_ACTIVE_STATUSES - {"failed", "needs_action", "paused"} for item in task_rows):
                    raise ValueError("运行中或排队中的批次不能删除")
                task_ids = [int(item["id"]) for item in task_rows]
                if delete_outputs:
                    for item in task_rows:
                        path = Path(str(item["output_path"] or ""))
                        if path.is_file(): path.unlink()
                if task_ids:
                    marks = ",".join("?" for _ in task_ids)
                    conn.execute(f"DELETE FROM generation_shot_materials WHERE task_id IN ({marks})", task_ids)
                    conn.execute(f"DELETE FROM generation_tasks WHERE id IN ({marks})", task_ids)
                conn.execute("DELETE FROM batch_tasks WHERE id=?", (record_id,))
                deleted += 1
            elif record_type in {"single", "task"}:
                task = conn.execute("SELECT status,output_path FROM generation_tasks WHERE id=?", (record_id,)).fetchone()
                if not task: continue
                batch = conn.execute("SELECT batch_id FROM batch_task_items WHERE task_id=?", (record_id,)).fetchone()
                if task["status"] in TASK_CENTER_ACTIVE_STATUSES - {"failed", "needs_action", "paused"}:
                    raise ValueError("运行中或排队中的任务不能删除")
                if delete_outputs:
                    path = Path(str(task["output_path"] or ""))
                    if path.is_file(): path.unlink()
                conn.execute("DELETE FROM generation_shot_materials WHERE task_id=?", (record_id,))
                conn.execute("DELETE FROM generation_tasks WHERE id=?", (record_id,))
                if batch:
                    counts = conn.execute(
                        """SELECT COUNT(*) total,
                                  SUM(CASE WHEN g.status IN ('succeeded','completed') THEN 1 ELSE 0 END) completed,
                                  SUM(CASE WHEN g.status='failed' THEN 1 ELSE 0 END) failed
                           FROM batch_task_items bi JOIN generation_tasks g ON g.id=bi.task_id
                           WHERE bi.batch_id=?""", (int(batch["batch_id"]),)
                    ).fetchone()
                    conn.execute(
                        "UPDATE batch_tasks SET total_count=?,completed_count=?,failed_count=?,updated_at=? WHERE id=?",
                        (int(counts["total"] or 0), int(counts["completed"] or 0), int(counts["failed"] or 0), now(), int(batch["batch_id"])),
                    )
                deleted += 1
    return {"deleted": deleted, "deleted_outputs": bool(delete_outputs)}


def delete_old_completed_records(delete_outputs: bool = False) -> dict:
    cutoff = rows(f"SELECT record_type,record_id FROM ({_task_center_union_sql()}) WHERE status IN ('completed','succeeded','partially_succeeded') AND datetime(COALESCE(NULLIF(finished_at,''),updated_at)) < datetime('now','localtime','-30 days')")
    result = delete_task_center_records(cutoff, delete_outputs)
    result["matched"] = len(cutoff)
    return result


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
    return Path(make_thumbnail(source, digest))


def open_task_output(task_id: int):
    task = row("SELECT output_path FROM generation_tasks WHERE id=?", (task_id,))
    output = Path(str((task or {}).get("output_path") or ""))
    if not output.is_file():
        raise FileNotFoundError("成片文件不存在")
    command = ["explorer.exe", "/select,", str(output)]
    subprocess.Popen(command, **background_process_kwargs(command, "open_task_output"))
    return True


def task_log_details(task_id: int) -> dict:
    task = row(
        """SELECT g.id,g.task_name,g.status,g.current_stage,g.failed_stage,g.current_step,g.current_item,g.current_shot,
                    g.error_code,g.error_message,g.technical_log_id,g.log_path,g.diagnostic_log,g.progress,
                    g.last_heartbeat_at,g.worker_id,g.process_id,g.process_name,g.ffmpeg_pid,g.ffprobe_pid,
                   g.created_at,g.finished_at,g.updated_at,g.output_path,
                   COALESCE((SELECT SUM(s.voice_duration) FROM template_shots s WHERE s.template_id=g.template_id),0) output_duration,
                   bi.index_no,b.total_count,bi.batch_id
           FROM generation_tasks g
           LEFT JOIN batch_task_items bi ON bi.task_id=g.id
           LEFT JOIN batch_tasks b ON b.id=bi.batch_id
           WHERE g.id=?""", (int(task_id),)
    )
    if not task:
        raise ValueError("任务不存在")
    log_file = Path(str(task.get("log_path") or task_log_path(task_id)))
    file_lines: list[str] = []
    if log_file.is_file():
        try:
            file_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
        except OSError:
            file_lines = []
    if not file_lines:
        file_lines = str(task.get("diagnostic_log") or "").splitlines()[-100:]
    health = task_health_snapshot(int(task_id))
    return {
        "task_id": int(task_id), "task_name": task.get("task_name") or f"任务{task_id}",
        "index_no": int(task.get("index_no") or 1), "total_count": int(task.get("total_count") or 1),
        "status": canonical_task_status(task.get("status")), "current_stage": task.get("current_stage") or "",
        "failed_stage": task.get("failed_stage") or task.get("current_stage") or "",
        "progress": int(task.get("progress") or 0), "error_code": task.get("error_code") or "",
        "error_summary": task.get("error_message") or "",
        "technical_log_id": task.get("technical_log_id") or f"MIX-{int(task_id):06d}",
        "last_heartbeat_at": task.get("last_heartbeat_at") or "",
        "created_at": task.get("created_at") or "", "started_at": task.get("created_at") or "",
        "finished_at": task.get("finished_at") or "", "updated_at": task.get("updated_at") or "",
        "current_step": task.get("current_step") or task.get("current_shot") or "",
        "current_shot": task.get("current_shot") or task.get("current_step") or "",
        "output_path": task.get("output_path") or "", "output_duration": float(task.get("output_duration") or 0),
        "output_exists": bool(task.get("output_path") and Path(str(task.get("output_path"))).is_file()),
        "output_url": media_url(task.get("output_path") or ""),
        "batch_id": int(task.get("batch_id") or 0),
        "worker_id": task.get("worker_id") or "", "process_id": int(task.get("process_id") or 0),
        "process_name": task.get("process_name") or "", "ffmpeg_pid": int(task.get("ffmpeg_pid") or 0),
        "ffprobe_pid": int(task.get("ffprobe_pid") or 0), **health,
        "log_exists": log_file.is_file(), "log_file_name": log_file.name,
        "recent_logs": "\n".join(file_lines) or "该历史任务没有保存完整诊断日志。",
    }


def open_task_log(task_id: int) -> dict:
    details = task_log_details(task_id)
    if not details["log_exists"]:
        raise FileNotFoundError("未找到该任务的日志文件。历史任务可能在日志功能启用前已经结束。")
    task = row("SELECT log_path FROM generation_tasks WHERE id=?", (int(task_id),))
    path = Path(str((task or {}).get("log_path") or ""))
    if os.name == "nt":
        os.startfile(str(path))
    else:
        command = ["xdg-open", str(path)]
        subprocess.Popen(command, **background_process_kwargs(command, "open_task_log"))
    return {"opened": True, "file_name": path.name}


def select_folder() -> str:
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded]
    p = subprocess.run(command,
                       capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
                       **background_process_kwargs(command, "select_folder_dialog"))
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
    command = ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded]
    process = subprocess.run(
        command,
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(command, "select_cover_dialog"),
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
    command = ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded]
    process = subprocess.run(
        command,
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(command, "select_oauth_json_dialog"),
    )
    selected = (process.stdout or "").lstrip("\ufeff").strip()
    if not selected:
        return {}
    path = Path(selected)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError("Google OAuth JSON 无法读取或格式错误") from exc
    return import_google_oauth_payload(payload, path.name)


def select_voice_sample() -> str:
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Filter = "Voice sample|*.mp3;*.m4a;*.wav|All files|*.*"
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.FileName) }
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded]
    process = subprocess.run(
        command,
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore",
        **background_process_kwargs(command, "select_voice_dialog"),
    )
    return (process.stdout or "").lstrip("\ufeff").strip()


def clone_tts_voice(service_id: int, data: dict):
    config = runtime_config("tts_service_configs", service_id)
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


def _inspect_material_directory(folder: str | Path) -> dict:
    """Validate only the directory itself, never its video count.

    ``os.access`` is deliberately not used here: on Windows it can report a
    directory as readable even when the first directory operation is denied.
    Opening the iterator and consuming one entry is a reliable, non-mutating
    read probe and also succeeds for an empty directory.
    """
    configured = str(folder or "").strip()
    if not configured:
        return {
            "exists": False, "readable": False, "has_videos": False,
            "video_count": 0, "error_code": "MATERIAL_ROOT_NOT_CONFIGURED",
            "message": "素材目录不存在，请重新选择",
        }
    root = Path(configured)
    try:
        if not root.exists() or not root.is_dir():
            return {
                "exists": False, "readable": False, "has_videos": False,
                "video_count": 0, "error_code": "MATERIAL_ROOT_NOT_FOUND",
                "message": "素材目录不存在，请重新选择",
            }
        iterator = root.iterdir()
        next(iterator, None)  # Empty directories are valid and remain readable.
        return {
            "exists": True, "readable": True, "has_videos": False,
            "video_count": 0, "error_code": "", "message": "目录可用",
        }
    except PermissionError:
        return {
            "exists": True, "readable": False, "has_videos": False,
            "video_count": 0, "error_code": "MATERIAL_ROOT_PERMISSION_DENIED",
            "message": "当前目录没有读取权限，请更换目录",
        }
    except OSError as exc:
        return {
            "exists": False, "readable": False, "has_videos": False,
            "video_count": 0, "error_code": "MATERIAL_ROOT_UNAVAILABLE",
            "message": f"素材目录无法访问，请重新选择（{exc}）",
        }


def material_root_status() -> dict:
    configured = str(settings().get("material_root") or "").strip()
    inspection = _inspect_material_directory(configured)
    root = Path(configured) if configured else None
    # Video presence is informational only. It must never make a readable
    # directory invalid, especially before the first scan.
    if inspection["readable"] and root:
        try:
            count = sum(1 for path in root.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTS)
            inspection.update(has_videos=count > 0, video_count=count)
            inspection["message"] = "目录可用" if count else "该目录暂无可用视频素材"
        except (PermissionError, OSError):
            pass
    return {"folder": configured, **inspection, "scanning": MATERIAL_SCAN_LOCK.locked()}


def open_material_root() -> dict:
    state = material_root_status()
    root = Path(state["folder"])
    if not state["exists"]:
        raise ValueError(state["message"])
    if not state["readable"]:
        raise PermissionError(state["message"])
    command = ["explorer.exe", str(root)] if os.name == "nt" else ["xdg-open", str(root)]
    subprocess.Popen(command, **background_process_kwargs(command, "open_data_folder"))
    return {"opened": True, "folder": str(root)}


def _path_key(value: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(value)))


def _material_error_text(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "FFprobe处理超时"
    if isinstance(exc, PermissionError):
        return "文件正在被其他程序占用"
    if isinstance(exc, FileNotFoundError):
        return "素材文件不存在"
    return "素材无法读取或视频格式异常"


def scan_materials(folder: str = ""):
    """Incrementally synchronize the configured material root with SQLite."""
    configured = str(folder or settings().get("material_root") or "").strip()
    inspection = _inspect_material_directory(configured)
    root = Path(configured) if configured else None
    if not inspection["exists"]:
        raise ValueError(inspection["message"])
    if not inspection["readable"]:
        raise PermissionError(inspection["message"])
    if not MATERIAL_SCAN_LOCK.acquire(blocking=False):
        raise ValueError("素材正在扫描中")

    scan_id = 0
    root_resolved = root.resolve()
    result = {
        "total": 0, "added": 0, "updated": 0, "missing": 0,
        "errors": 0, "unchanged": 0, "folder": str(root_resolved),
        "scan_id": 0, "error_items": [],
    }
    try:
        ffprobe = ""
        ffmpeg = ""
        files = sorted(scan_video_files(str(root_resolved)), key=lambda item: _path_key(item))
        result["total"] = len(files)
        disk_keys = {_path_key(path) for path in files}

        folder_categories = [item.name for item in root_resolved.iterdir() if item.is_dir()]
        if any(path.parent == root_resolved for path in files):
            folder_categories.append("未分类")
        with connect() as conn:
            # A process-local lock is authoritative. Old running rows only mean the
            # previous executable exited before it could close the audit record.
            conn.execute(
                "UPDATE scan_tasks SET status='interrupted',error_message='上次扫描被软件退出中断',finished_at=? WHERE status='running'",
                (now(),),
            )
            for order, category_name in enumerate(dict.fromkeys(folder_categories), 1):
                conn.execute(
                    """INSERT INTO material_categories(name,description,sort_order,created_at,updated_at)
                       VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET sort_order=excluded.sort_order,updated_at=excluded.updated_at""",
                    (category_name, "扫描目录自动创建", order, now(), now()),
                )
            conn.execute(
                """INSERT INTO scan_tasks(folder_path,status,total_files,created_at)
                   VALUES(?,?,?,?)""", (str(root_resolved), "running", len(files), now())
            )
            scan_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            result["scan_id"] = scan_id
            conn.execute(
                """INSERT INTO app_settings(key,value,updated_at) VALUES('material_root',?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (str(root_resolved), now()),
            )

        for index, path in enumerate(files, 1):
            path_text = str(path.resolve())
            scanned_at = now()
            try:
                stat = path.stat()
                rel = path.resolve().relative_to(root_resolved)
                category_name = rel.parts[0] if len(rel.parts) > 1 else "未分类"
                with connect() as conn:
                    cat = conn.execute("SELECT id FROM material_categories WHERE name=?", (category_name,)).fetchone()
                    if not cat:
                        max_order = int(conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM material_categories").fetchone()[0])
                        conn.execute(
                            "INSERT INTO material_categories(name,description,sort_order,created_at,updated_at) VALUES(?,?,?,?,?)",
                            (category_name, "扫描目录自动创建", max_order, scanned_at, scanned_at),
                        )
                        category_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                    else:
                        category_id = int(cat[0])
                    existing = conn.execute("SELECT * FROM materials WHERE file_path=?", (path_text,)).fetchone()

                digest = ""
                changed = existing is None
                if existing is not None:
                    stored_mtime = int(existing["file_mtime_ns"] or 0)
                    stored_size = int(existing["size"] or 0)
                    changed = stored_mtime != int(stat.st_mtime_ns) or stored_size != int(stat.st_size) or str(existing["status"] or "") != "active"
                    # Old schema rows have no mtime. Verify once by hash, then
                    # populate mtime without unnecessarily regenerating metadata.
                    if stored_mtime == 0 or changed:
                        digest = quick_hash(path)
                        if digest == str(existing["file_hash"] or "") and str(existing["status"] or "") == "active":
                            changed = False

                if existing is not None and not changed:
                    with connect() as conn:
                        conn.execute(
                            """UPDATE materials SET file_name=?,size=?,file_mtime_ns=?,status='active',scan_error='',last_scanned_at=? WHERE id=?""",
                            (path.name, int(stat.st_size), int(stat.st_mtime_ns), scanned_at, int(existing["id"])),
                        )
                    result["unchanged"] += 1
                else:
                    digest = digest or quick_hash(path)
                    # A missing record with the exact existing fingerprint is a
                    # reliable rename/move candidate. Preserve its category/remark.
                    renamed = None
                    if existing is None:
                        with connect() as conn:
                            candidates = conn.execute(
                                "SELECT * FROM materials WHERE file_hash=? AND file_path<>? ORDER BY id", (digest, path_text)
                            ).fetchall()
                        missing_candidates = [item for item in candidates if _path_key(item["file_path"]) not in disk_keys and not Path(item["file_path"]).is_file()]
                        if len(missing_candidates) == 1:
                            renamed = missing_candidates[0]
                            existing = renamed

                    info = probe_video(path, ffprobe)
                    thumb, thumb_warning = make_thumbnail_with_status(path, digest, ffmpeg)
                    probe_error = str(info.get("error") or "")
                    usable = not probe_error
                    scan_error = probe_error or thumb_warning
                    if scan_error:
                        result["errors"] += 1
                        result["error_items"].append({"file": path.name, "path": path_text, "reason": scan_error})
                    material_status = "active" if usable else "error"
                    old_thumb = str(existing["thumbnail_path"] or "") if existing is not None else ""
                    with connect() as conn:
                        if existing is not None:
                            # Preserve user classification and remarks for same-path
                            # replacement and fingerprint-confirmed rename.
                            conn.execute(
                                """UPDATE materials SET file_path=?,file_name=?,file_hash=?,duration=?,width=?,height=?,fps=?,size=?,
                                   file_mtime_ns=?,codec=?,thumbnail_path=?,status=?,scan_error=?,last_scanned_at=?,updated_at=? WHERE id=?""",
                                (path_text, path.name, digest, float(info.get("duration") or 0), int(info.get("width") or 0),
                                 int(info.get("height") or 0), float(info.get("fps") or 0), int(info.get("size") or stat.st_size),
                                 int(stat.st_mtime_ns), str(info.get("codec") or ""), thumb, material_status, scan_error,
                                 scanned_at, scanned_at, int(existing["id"])),
                            )
                            result["updated"] += 1
                        else:
                            conn.execute(
                                """INSERT INTO materials(category_id,file_path,file_name,file_hash,duration,width,height,fps,size,
                                   file_mtime_ns,codec,thumbnail_path,status,scan_error,last_scanned_at,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (category_id, path_text, path.name, digest, float(info.get("duration") or 0), int(info.get("width") or 0),
                                 int(info.get("height") or 0), float(info.get("fps") or 0), int(info.get("size") or stat.st_size),
                                 int(stat.st_mtime_ns), str(info.get("codec") or ""), thumb, material_status, scan_error,
                                 scanned_at, scanned_at, scanned_at),
                            )
                            result["added"] += 1
                    if old_thumb and old_thumb != thumb:
                        old_path = Path(old_thumb)
                        if old_path.is_file() and old_path.parent == THUMB_DIR:
                            try:
                                old_path.unlink()
                            except OSError:
                                pass
            except Exception as exc:
                reason = _material_error_text(exc)
                result["errors"] += 1
                result["error_items"].append({"file": path.name, "path": str(path), "reason": reason})
                with connect() as conn:
                    existing = conn.execute("SELECT id FROM materials WHERE file_path=?", (path_text,)).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE materials SET status='error',scan_error=?,last_scanned_at=?,updated_at=? WHERE id=?",
                            (reason, scanned_at, scanned_at, int(existing["id"])),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO materials(category_id,file_path,file_name,status,scan_error,last_scanned_at,created_at,updated_at)
                               VALUES(NULL,?,?,'error',?,?,?,?)""",
                            (path_text, path.name, reason, scanned_at, scanned_at, scanned_at),
                        )
                        result["added"] += 1
            finally:
                with connect() as conn:
                    conn.execute(
                        """UPDATE scan_tasks SET scanned_files=?,added_count=?,updated_count=?,error_count=?,unchanged_count=? WHERE id=?""",
                        (index, result["added"], result["updated"], result["errors"], result["unchanged"], scan_id),
                    )

        # Any indexed file not present under the newly authoritative root is
        # retained for history but excluded from the normal list and matcher.
        with connect() as conn:
            all_materials = conn.execute("SELECT id,file_path,status FROM materials").fetchall()
            missing_ids = [int(item["id"]) for item in all_materials if _path_key(item["file_path"]) not in disk_keys and str(item["status"] or "") != "missing"]
            if missing_ids:
                marks = ",".join("?" for _ in missing_ids)
                conn.execute(
                    f"UPDATE materials SET status='missing',scan_error='文件已丢失',last_scanned_at=?,updated_at=? WHERE id IN ({marks})",
                    (now(), now(), *missing_ids),
                )
            result["missing"] = len(missing_ids)
            conn.execute(
                """UPDATE scan_tasks SET status='completed',scanned_files=?,added_count=?,updated_count=?,missing_count=?,
                   error_count=?,unchanged_count=?,skipped_count=?,finished_at=? WHERE id=?""",
                (len(files), result["added"], result["updated"], result["missing"], result["errors"],
                 result["unchanged"], result["unchanged"], now(), scan_id),
            )
        return result
    except Exception as exc:
        if scan_id:
            with connect() as conn:
                conn.execute(
                    "UPDATE scan_tasks SET status='failed',error_message=?,finished_at=? WHERE id=?",
                    (_material_error_text(exc), now(), scan_id),
                )
        raise
    finally:
        MATERIAL_SCAN_LOCK.release()


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
    config = runtime_service_config(config) if config else None
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
    material_strategy="random",
):
    shot = row("SELECT * FROM template_shots WHERE id=?", (shot_id,))
    if not shot:
        raise ValueError("镜头不存在")
    duration = float(shot.get("voice_duration") or 0)
    if duration <= 0:
        raise ValueError("请先生成镜头语音，再匹配素材")
    with connect() as conn:
        pool_stats = material_pool_diagnostics(
            conn, int(shot.get("material_category_id") or 0), duration
        )
        candidates = load_material_candidates(conn, int(shot.get("material_category_id") or 0), duration)
    excluded = set(exclude_ids or [])
    avoided = set(avoid_material_ids or [])
    previous_material_id = int((previous_match or {}).get("material_id") or 0)
    try:
        material, diagnostics = select_material(
            candidates,
            strategy=material_strategy,
            exclude_ids=excluded,
            avoid_ids=avoided | ({previous_material_id} if previous_material_id else set()),
        )
        diagnostics.update(pool_stats)
    except ValueError:
        category = row("SELECT name FROM material_categories WHERE id=?", (shot.get("material_category_id"),))
        short = row("SELECT MAX(duration) duration FROM materials WHERE status='active' AND category_id=?", (shot.get("material_category_id"),))
        max_duration = float((short or {}).get("duration") or 0)
        raise ValueError(
            f"素材不足：需要 {duration:.1f} 秒{(category or {}).get('name','')}素材，"
            f"当前最长只有 {max_duration:.1f} 秒"
        )
    max_start = max(0.0, float(material["duration"]) - duration)
    if task_id is not None and material["id"] == previous_material_id and max_start <= 0.5:
        raise ValueError(
            f"当前分类只有素材 {material['file_name']} 可用，且无法选择不同片段。"
            "请补充该分类素材后重试。"
        )
    start = random.uniform(0.0, max_start) if max_start > 0.05 else 0.0
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
    return {
        "material_id": material["id"], "file_name": material["file_name"],
        "duration": material["duration"], "start": start, "end": end,
        "usage_count": int(material.get("usage_count") or 0),
        "last_used_at": str(material.get("last_used_at") or ""),
        "selection_reason": diagnostics["selection_reason"],
        "diagnostics": diagnostics,
    }

def generate_tts(shot_id: int):
    from generation_performance import measure
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
    result = generate_shot_audio(shot_id, text, config, voice, cfg, speed)
    return commit_shot_audio(shot, text, config, voice, cfg, speed, segment_gap_ms, result)


def generate_shot_audio(shot_id, text, config, voice, cfg, speed=1.0):
    """Generate audio artifacts only; caller owns subtitle and database updates."""
    from generation_performance import measure
    # Spaces are explicit cuts in Chinese scripts, but normal word separators
    # in English, Spanish and Vietnamese. Never synthesize Latin text word by word.
    text_parts = split_text(text) if contains_cjk(text) else [text]
    exact_subtitle_segments = []
    part_paths = []
    if len(text_parts) > 1:
        with measure("tts"):
            for index, part in enumerate(text_parts, 1):
                part_path, part_duration = synthesize_tts(
                    config, voice, part, f"{shot_id}_part_{index:03d}", cfg, speed
                )
                part_paths.append(part_path)
        with measure("audio_mix"):
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
        with measure("tts"):
            voice_path, duration = synthesize_tts(config, voice, text, shot_id, cfg, speed)
    return {"shot_id": shot_id, "audio_path": voice_path, "duration": duration,
            "subtitle_segments": exact_subtitle_segments}


def prepare_all_shot_audio(shots, cfg, speed=1.0):
    """Prepare audio with bounded concurrency; callers still commit in order."""
    from concurrent.futures import ThreadPoolExecutor

    def prepare_one(shot):
        shot_id = shot["id"]
        text = normalize_rewrite_text(shot_script_text(shot))
        voice, config = resolve_tts_voice(cfg, text)
        result = generate_shot_audio(shot_id, text, config, voice, cfg, speed)
        return shot, text, config, voice, result

    # executor.map preserves input order even when later shots finish first.
    prepared = []
    seen_paths = set()
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="tts-prepare") as executor:
      for shot, text, config, voice, result in executor.map(prepare_one, shots):
        shot_id = shot["id"]
        path = str(result.get("audio_path") or "")
        duration = float(result.get("duration") or 0)
        if result.get("shot_id") != shot_id or not path or duration <= 0 or path in seen_paths:
            raise ValueError(f"TTS音频结果校验失败：镜头 {shot_id}")
        seen_paths.add(path)
        prepared.append({"shot_id": shot_id, "audio_path": path, "duration": duration,
                         "subtitle_segments": result.get("subtitle_segments") or [],
                         "text": text, "config": config, "voice": voice})
    return prepared


def commit_shot_audio(shot, text, config, voice, cfg, speed, segment_gap_ms, result):
    """Commit serially on the generation caller, never on an audio worker."""
    from generation_performance import measure
    shot_id = shot["id"]
    if result["shot_id"] != shot_id:
        raise ValueError("TTS result does not belong to this shot")
    voice_path, duration = result["audio_path"], result["duration"]
    exact_subtitle_segments = result["subtitle_segments"]
    subtitle_path = ""
    if shot.get("enable_subtitle") and str(cfg.get("subtitle_enabled") or "1") != "0":
        with measure("subtitle"):
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
    existing = row("SELECT api_key FROM ai_service_configs WHERE id=?", (int(data.get("id") or 0),)) if data.get("id") else None
    submitted_key = str(data.get("api_key") or "").strip()
    stored_key = encrypt_secret(submitted_key) if submitted_key else str((existing or {}).get("api_key") or "")
    system_prompt = str(data.get("system_prompt") or "").strip() or DEFAULT_REWRITE_PROMPT
    values = (name, str(data.get("provider") or "openai_compatible"), str(data.get("base_url") or ""), stored_key, str(data.get("model") or ""), int(data.get("timeout_seconds") or 60), 1 if data.get("is_enabled", True) else 0, 1 if data.get("is_default") else 0, system_prompt)
    with connect() as conn:
        if values[7]: conn.execute("UPDATE ai_service_configs SET is_default=0")
        if data.get("id"):
            conn.execute("UPDATE ai_service_configs SET name=?,provider=?,base_url=?,api_key=?,model=?,timeout_seconds=?,is_enabled=?,is_default=?,system_prompt=?,updated_at=? WHERE id=?", (*values, now(), int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO ai_service_configs(name,provider,base_url,api_key,model,timeout_seconds,is_enabled,is_default,system_prompt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def open_bailian_api_key_page() -> dict:
    """Open the official Bailian key page in the user's system browser."""
    if not webbrowser.open(BAILIAN_API_KEY_URL, new=1):
        raise RuntimeError("无法打开系统默认浏览器")
    return {"ok": True, "url": BAILIAN_API_KEY_URL}


_SECRET_PLACEHOLDERS = {
    "已配置，重新输入可覆盖", "已配置", "••••••••••••••••",
    "****************", "********",
}


def resolve_ai_test_config(data: dict) -> dict:
    """Merge current form values with only the stored secret, without saving."""
    form = dict(data.get("config") or data or {})
    record_id = int(form.get("id") or 0)
    submitted_key = str(form.get("api_key") or "").strip()
    if submitted_key in _SECRET_PLACEHOLDERS or set(submitted_key) <= {"*", "•"}:
        submitted_key = ""
    stored = runtime_config("ai_service_configs", record_id) if record_id else {}
    config = dict(stored or {})
    config.update({key: value for key, value in form.items() if key != "api_key"})
    config["api_key"] = submitted_key or str((stored or {}).get("api_key") or "")
    return config


def save_tts_service(data):
    name = str(data.get("name") or "").strip()
    if not name: raise ValueError("配音服务名称不能为空")
    existing = row("SELECT api_key FROM tts_service_configs WHERE id=?", (int(data.get("id") or 0),)) if data.get("id") else None
    submitted_key = str(data.get("api_key") or "").strip()
    stored_key = encrypt_secret(submitted_key) if submitted_key else str((existing or {}).get("api_key") or "")
    values = (name, str(data.get("provider") or "edge_tts"), str(data.get("base_url") or ""), stored_key, str(data.get("model") or ""), str(data.get("response_format") or "mp3"), int(data.get("timeout_seconds") or 120), 1 if data.get("is_enabled", True) else 0, 1 if data.get("is_default") else 0)
    with connect() as conn:
        if values[8]: conn.execute("UPDATE tts_service_configs SET is_default=0")
        if data.get("id"):
            conn.execute("UPDATE tts_service_configs SET name=?,provider=?,base_url=?,api_key=?,model=?,response_format=?,timeout_seconds=?,is_enabled=?,is_default=?,updated_at=? WHERE id=?", (*values, now(), int(data["id"])))
            return int(data["id"])
        conn.execute("INSERT INTO tts_service_configs(name,provider,base_url,api_key,model,response_format,timeout_seconds,is_enabled,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, now(), now()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def sync_tts_voices(service_id: int) -> dict:
    config = runtime_config("tts_service_configs", service_id)
    if not config:
        raise ValueError("配音服务不存在")
    if config.get("provider") != "aliyun_bailian":
        raise ValueError("当前服务不支持同步音色")
    voices = list_bailian_voices(config)
    with connect() as conn:
        for index, voice in enumerate(voices):
            existing = conn.execute(
                "SELECT id FROM tts_voices WHERE service_id=? AND voice_code=?",
                (service_id, voice["voice_code"]),
            ).fetchone()
            if existing:
                conn.execute("UPDATE tts_voices SET name=?,updated_at=? WHERE id=?", (voice["name"], now(), existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO tts_voices(service_id,name,voice_code,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (service_id, voice["name"], voice["voice_code"], 1 if index == 0 else 0, now(), now()),
                )
    return {"total": len(voices)}


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
    retry_callback=None,
):
    shot = row("SELECT * FROM template_shots WHERE id=?", (shot_id,))
    if not shot: raise ValueError("镜头不存在")
    config = row("SELECT * FROM ai_service_configs WHERE is_enabled=1 ORDER BY is_default DESC,id LIMIT 1")
    config = runtime_service_config(config) if config else None
    if not config: raise ValueError("请先在设置中配置并启用 AI 服务")
    profile = row("SELECT * FROM account_profiles WHERE id=?", (profile_id,)) if profile_id else row("SELECT * FROM account_profiles ORDER BY updated_at DESC,id DESC LIMIT 1")
    profile = profile or {}
    source_language = detect_text_language(shot.get("original_text") or "")
    system = config.get("system_prompt") or DEFAULT_REWRITE_PROMPT
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
        attempts=3,
        retry_callback=retry_callback,
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
    retry_callback=None,
):
    shots = rows("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (template_id,))
    if not shots:
        return []
    config = row("SELECT * FROM ai_service_configs WHERE is_enabled=1 ORDER BY is_default DESC,id LIMIT 1")
    config = runtime_service_config(config) if config else None
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
        DEFAULT_REWRITE_PROMPT + "\n\n根据账号资料一次性改写全部镜头，保持镜头顺序和事实准确。"
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
        attempts=3,
        retry_callback=retry_callback,
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
                        shot["id"], profile_id, variation_context, temperature, retry_callback
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
        conn.execute("BEGIN IMMEDIATE")
        queue_seq = reserve_generation_queue_seqs(conn, 1)[0]
        stamp = now()
        if generation_queue_seq_supported(conn):
            conn.execute("INSERT INTO generation_tasks(template_id,task_name,status,queue_seq,progress,total_shots,created_at,updated_at) VALUES(?,?, 'queued',?,0,?,?,?)",
                         (template_id, name, queue_seq, len(shots), stamp, stamp))
        else:
            conn.execute("INSERT INTO generation_tasks(template_id,task_name,status,progress,total_shots,created_at,updated_at) VALUES(?,?, 'queued',0,?,?,?)",
                         (template_id, name, len(shots), stamp, stamp))
        task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    initialize_task_log(task_id, name)
    return task_id


def single_generation_preflight(template_id: int) -> dict:
    template = row("SELECT * FROM edit_templates WHERE id=?", (int(template_id),))
    shots = rows("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (int(template_id),))
    if not template:
        raise ValueError("模板不存在")
    if not shots:
        raise ValueError("请先添加镜头")
    missing_text, missing_category, missing_material = [], [], []
    missing_required_voice = []
    voice_ready = subtitle_ready = material_ready = 0
    cfg = settings()
    subtitles_enabled = str(cfg.get("subtitle_enabled") or "1") != "0"
    for index, shot in enumerate(shots, 1):
        label = shot.get("shot_name") or f"镜头{index}"
        text = shot_script_text(shot).strip()
        if not text:
            missing_text.append(label)
        category_id = int(shot.get("material_category_id") or 0)
        if not category_id:
            missing_category.append(label)
        else:
            available = next((item for item in rows(
                "SELECT id,file_path FROM materials WHERE status='active' AND category_id=? AND file_path<>''",
                (category_id,),
            ) if Path(str(item.get("file_path") or "")).is_file()), None)
            if not available:
                missing_material.append(label)
            else:
                material_ready += 1
        voice_path = Path(str(shot.get("voice_file_path") or ""))
        if float(shot.get("voice_duration") or 0) > 0 and voice_path.is_file():
            voice_ready += 1
        elif str(cfg.get("tts_enabled") or "1") == "0" or not int(shot.get("enable_tts") or 0):
            missing_required_voice.append(label)
        subtitle_path = Path(str(shot.get("subtitle_file_path") or ""))
        if not subtitles_enabled or not int(shot.get("enable_subtitle") or 0) or subtitle_path.is_file():
            subtitle_ready += 1
    if missing_text:
        raise ValueError("以下镜头缺少文案：" + "、".join(missing_text))
    if missing_category:
        raise ValueError("以下镜头未选择素材分类：" + "、".join(missing_category))
    if missing_material:
        raise ValueError("素材不足，请补充：" + "、".join(missing_material))
    if missing_required_voice:
        raise ValueError("请先生成配音：" + "、".join(missing_required_voice))
    return {
        "template_id": int(template_id), "template_name": template["name"], "shot_count": len(shots),
        "voice_ready_count": voice_ready, "subtitle_ready_count": subtitle_ready,
        "material_ready_count": material_ready,
        "estimated_duration": round(sum(float(item.get("voice_duration") or 0) for item in shots), 2),
    }


def create_and_start_single_task(data: dict) -> dict:
    template_id = int(data.get("template_id") or 0)
    summary = single_generation_preflight(template_id)
    with GENERATION_START_LOCK:
        active = row(
            """SELECT * FROM generation_tasks WHERE template_id=?
               AND NOT EXISTS(SELECT 1 FROM batch_task_items bi WHERE bi.task_id=generation_tasks.id)
               AND status IN ('queued','preparing','rendering','verifying')
               ORDER BY id DESC LIMIT 1""",
            (template_id,),
        )
        if active and canonical_task_status(active["status"]) != "queued" and worker_is_alive(int(active["id"])):
            return {"task_id": int(active["id"]), "status": active["status"], "duplicate": True, **summary}
        if active:
            task_id = int(active["id"])
            reused = True
        else:
            task_id = int(create_task({
                "template_id": template_id,
                "task_name": str(data.get("task_name") or f"{summary['template_name']}-{now()}"),
            }))
            reused = False
        started = start_generate(task_id)
        return {**started, **summary, "duplicate": reused, "batch_size": 1}


def start_generate(task_id: int, before_generation=None, *, queue_priority: int | None = None):
    task_id = int(task_id)
    with GENERATION_START_LOCK:
        task = row("SELECT * FROM generation_tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("任务不存在")
        status = canonical_task_status(task["status"])
        if status in {"preparing", "rendering", "verifying"}:
            health = task_health_snapshot(task_id)
            if health.get("worker_alive") or health.get("process_alive"):
                return {"task_id": task_id, "status": status, "duplicate": True}
            raise ValueError("任务处于异常执行状态，请先在任务中心诊断或终止并重试")
        if status != "queued":
            raise ValueError(f"当前任务状态 {status} 不能开始生成")
    cfg = settings()
    output_dir = cfg.get("output_dir", "")
    if not output_dir or not Path(output_dir).parent.exists():
        output_dir = str(APP_DIR / "outputs")
    def worker():
        from generation_performance import start as start_performance, finish as finish_performance, measure
        start_performance(task_id)
        try:
          with task_context(task_id):
            if before_generation is not None:
                before_generation()
            heartbeat(task_id, status="preparing", stage="preparing", step="读取镜头配置", progress=2,
                      log="生成线程已启动")
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
                           JOIN generation_tasks g ON g.id=i.task_id
                           WHERE i.batch_id=? AND i.index_no<?
                             AND g.status IN ('completed','succeeded')""",
                        (batch_context["batch_id"], batch_context["index_no"]),
                    )
                )
            elif batch_context and batch_context["material_strategy"] == "random":
                previous_matches = {}
                previous_materials = set()
            prepared_audio = {}
            stale_audio_shots = []
            for candidate in task_shots:
                candidate_text = shot_script_text(candidate)
                candidate_voice, candidate_config = resolve_tts_voice(cfg, candidate_text)
                candidate_path = str(candidate.get("voice_file_path") or "")
                candidate_stale = (
                    float(candidate.get("voice_duration") or 0) <= 0
                    or not candidate_path
                    or not Path(candidate_path).is_file()
                    or str(candidate.get("voice_text_hash") or "") != voice_render_hash(
                        candidate_text, candidate_config, candidate_voice, selected_speed, selected_segment_gap_ms
                    )
                )
                if candidate.get("enable_tts") and candidate_stale:
                    stale_audio_shots.append(candidate)
            if stale_audio_shots:
                heartbeat(task_id, stage="tts", step=f"准备配音 0/{len(stale_audio_shots)}", progress=4)
                for prepared in prepare_all_shot_audio(stale_audio_shots, cfg, selected_speed):
                    prepared_audio[prepared["shot_id"]] = prepared

            for index, shot in enumerate(task_shots, 1):
                check_cancelled(task_id)
                base_progress = 3 + int((index - 1) / max(1, len(task_shots)) * 37)
                heartbeat(task_id, status="preparing", stage="preparing_shot",
                          step=f"准备镜头 {index}/{len(task_shots)}", item=str(shot.get("shot_name") or index),
                          progress=base_progress)
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
                    heartbeat(task_id, stage="tts", step=f"生成配音 {index}/{len(task_shots)}",
                              progress=base_progress + 1)
                    prepared = prepared_audio.pop(shot["id"], None)
                    if prepared is None:
                        raise ValueError(f"镜头 {shot['id']} 的配音准备结果不存在")
                    commit_shot_audio(
                        shot,
                        prepared["text"],
                        prepared["config"],
                        prepared["voice"],
                        cfg,
                        selected_speed,
                        selected_segment_gap_ms,
                        {
                            "shot_id": prepared["shot_id"],
                            "audio_path": prepared["audio_path"],
                            "duration": prepared["duration"],
                            "subtitle_segments": prepared["subtitle_segments"],
                        },
                    )
                    # A regenerated voice can have a different real duration, so
                    # its previous material crop is no longer valid.
                    with connect() as conn:
                        conn.execute("DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL", (shot["id"],))
                shot = row("SELECT * FROM template_shots WHERE id=?", (shot["id"],))
                # Always trust ffprobe of the final audio file over cached DB or
                # provider estimates before matching material and rendering.
                previous_voice_duration = float(shot.get("voice_duration") or 0)
                heartbeat(task_id, stage="ffprobe", step=f"检测语音时长 {index}/{len(task_shots)}",
                          progress=base_progress + 2)
                actual_voice_duration = audio_duration(str(shot.get("voice_file_path") or ""))
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
                        heartbeat(task_id, stage="subtitle", step=f"生成字幕 {index}/{len(task_shots)}",
                                  progress=base_progress + 3)
                        with measure("subtitle"):
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
                heartbeat(task_id, stage="matching", step=f"匹配素材 {index}/{len(task_shots)}",
                          progress=base_progress + 4)
                with measure("material_match"):
                    relation = match_shot(
                        shot["id"], exclude_ids=used_materials, task_id=task_id,
                        avoid_material_ids=previous_materials,
                        previous_match=previous_matches.get(shot["id"]),
                        material_strategy=(batch_context or {}).get("material_strategy") or "random",
                    )
                diagnostic = relation.get("diagnostics") or {}
                append_task_log(
                    task_id,
                    "素材匹配诊断 · "
                    f"镜头={shot.get('shot_name') or shot['id']} · "
                    f"素材库={diagnostic.get('registry_count', 0)} · "
                    f"分类匹配={diagnostic.get('category_count', 0)} · "
                    f"时长合格={diagnostic.get('duration_count', 0)} · "
                    f"质量合格={diagnostic.get('valid_count', 0)} · "
                    f"未使用={diagnostic.get('unused_candidates', 0)} · "
                    f"本批可选={diagnostic.get('after_batch_exclusion', 0)} · "
                    f"选择={relation.get('material_id')} {relation.get('file_name')} · "
                    f"历史使用={relation.get('usage_count', 0)} · "
                    f"策略={relation.get('selection_reason', '')}",
                )
                if (
                    batch_context
                    and batch_context["material_strategy"] == "avoid_repeat"
                    and relation["material_id"] in previous_materials
                ):
                    raise ValueError("避免重复策略下可用素材不足，请补充对应分类素材")
                used_materials.append(relation["material_id"])
                heartbeat(task_id, stage="preparing_shot", step=f"镜头 {index}/{len(task_shots)} 已准备",
                          progress=min(40, base_progress + 5))
            heartbeat(task_id, status="rendering", stage="rendering", step="开始渲染成片", progress=42)
            generate_video(task_id, task["template_id"], "", output_dir, cfg.get("work_dir", ""))
        except TaskCancelled:
            with connect() as conn:
                conn.execute(
                    "UPDATE generation_tasks SET status='cancelled',current_stage='cancelled',current_step='已取消',"
                    "current_shot='已取消',process_id=0,error_message='',finished_at=?,updated_at=? WHERE id=?",
                    (now(), now(), task_id),
                )
            finish_performance("cancelled")
        except BaseException as exc:
            wrapped = exc if isinstance(exc, Exception) else RuntimeError(
                f"生成线程被异常中断：{type(exc).__name__}: {exc}"
            )
            fail_task(task_id, wrapped)
            finish_performance(str(wrapped))
        else:
            finish_performance()
    with GENERATION_START_LOCK:
        if GENERATION_QUEUE.is_pending(task_id):
            current = row("SELECT status FROM generation_tasks WHERE id=?", (task_id,)) or {}
            return {"task_id": task_id, "status": canonical_task_status(current.get("status")), "duplicate": True}
        if queue_priority is None:
            # A single durable sequence is shared by standalone and batch tasks.
            # Batch membership must never jump a newer task ahead of an older one.
            queue_priority = int(task.get("queue_seq") or task_id)
        queued = GENERATION_QUEUE.enqueue(task_id, worker, priority=queue_priority)
    current = row("SELECT status FROM generation_tasks WHERE id=?", (task_id,)) or {}
    return {
        "task_id": task_id,
        "status": canonical_task_status(current.get("status")),
        "duplicate": not queued,
        "max_generation_concurrency": MAX_GENERATION_CONCURRENCY,
    }


def retry_generation_task(task_id: int) -> dict:
    original_id = int(task_id)
    terminate_process(original_id)
    with GENERATION_START_LOCK:
        task = row("SELECT * FROM generation_tasks WHERE id=?", (original_id,))
        if not task:
            raise ValueError("任务不存在")
        if canonical_task_status(task["status"]) not in {"failed", "stalled", "cancelled", "queued", "paused"}:
            raise ValueError("当前任务状态不能重试")
        existing = row(
            """SELECT id,status FROM generation_tasks WHERE retry_of_task_id=?
               AND status IN ('queued','preparing','rendering','verifying')
               ORDER BY id DESC LIMIT 1""", (original_id,)
        )
        if existing:
            return {"task_id": int(existing["id"]), "status": existing["status"], "duplicate": True}
        single_generation_preflight(int(task["template_id"] or 0))
        cfg = settings()
        find_ffmpeg()
        output = Path(str(cfg.get("output_dir") or APP_DIR / "outputs"))
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".mixcut_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise ValueError("输出目录不可写，请更换目录或检查磁盘权限") from exc
        batch_item = row("SELECT id,batch_id,index_no FROM batch_task_items WHERE task_id=?", (original_id,))
        retry_no = int(task.get("retry_count") or 0) + 1
        retry_name = f"{task['task_name']} · 重试{retry_no}"
        stamp = now()
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            allocated_queue_seq = reserve_generation_queue_seqs(conn, 1)[0]
            queue_seq = allocated_queue_seq
            # A failed batch child must be retried in its original position.
            # Appending it behind its still-queued siblings creates a cycle:
            # the global queue waits for the older sibling while the batch
            # queue refuses to start that sibling until this retry completes.
            if (
                batch_item
                and generation_queue_seq_supported(conn)
                and int(task.get("queue_seq") or 0) > 0
            ):
                queue_seq = int(task["queue_seq"])
                conn.execute(
                    "UPDATE generation_tasks SET queue_seq=? WHERE id=?",
                    (allocated_queue_seq, original_id),
                )
            if generation_queue_seq_supported(conn):
                conn.execute(
                    """INSERT INTO generation_tasks(template_id,task_name,status,queue_seq,progress,current_progress,total_shots,
                           retry_count,retry_of_task_id,attempt_no,created_at,updated_at)
                       VALUES(?,?,'queued',?,0,0,?,?,?,?,?,?)""",
                    (task["template_id"], retry_name, queue_seq, task.get("total_shots") or 0, retry_no,
                      original_id, retry_no + 1, stamp, stamp),
                )
            else:
                conn.execute(
                    """INSERT INTO generation_tasks(template_id,task_name,status,progress,current_progress,total_shots,
                           retry_count,retry_of_task_id,attempt_no,created_at,updated_at)
                       VALUES(?,?,'queued',0,0,?,?,?,?,?,?)""",
                    (task["template_id"], retry_name, task.get("total_shots") or 0, retry_no,
                      original_id, retry_no + 1, stamp, stamp),
                )
            new_task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            if batch_item:
                conn.execute(
                    "UPDATE batch_task_items SET task_id=?,status='queued',output_path='',error_message='',updated_at=? WHERE id=?",
                    (new_task_id, stamp, int(batch_item["id"])),
                )
                conn.execute(
                    "UPDATE batch_tasks SET status='queued',finished_at='',updated_at=? WHERE id=?",
                    (stamp, int(batch_item["batch_id"])),
                )
            conn.execute("UPDATE generation_tasks SET retry_count=? WHERE id=?", (retry_no, original_id))
            conn.commit()
        initialize_task_log(new_task_id, retry_name)
    if batch_item:
        BATCH_QUEUE.enqueue(int(batch_item["batch_id"]), start_batch_generation)
        return {"task_id": new_task_id, "status": "queued", "batch_id": int(batch_item["batch_id"]), "duplicate": False}
    return {**start_generate(new_task_id), "retry_of_task_id": original_id}


def mark_generation_failed(task_id: int) -> dict:
    task_id = int(task_id)
    task = row("SELECT status FROM generation_tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("\u4efb\u52a1\u4e0d\u5b58\u5728")
    terminate_process(task_id)
    reason = "\u7528\u6237\u5df2\u5c06\u505c\u6ede\u4efb\u52a1\u6807\u8bb0\u4e3a\u5931\u8d25"
    item = None
    with connect() as conn:
        conn.execute(
            """UPDATE generation_tasks SET status='failed',current_stage='failed',
               current_step='\u5df2\u6807\u8bb0\u5931\u8d25',current_shot='\u5df2\u6807\u8bb0\u5931\u8d25',
               worker_id='',process_id=0,process_name='',ffmpeg_pid=0,ffprobe_pid=0,
               error_code=CASE WHEN error_code='' THEN 'MANUALLY_FAILED' ELSE error_code END,
               error_message=?,finished_at=?,updated_at=? WHERE id=?""",
            (reason, now(), now(), task_id),
        )
        item = conn.execute(
            "SELECT id,batch_id FROM batch_task_items WHERE task_id=?", (task_id,)
        ).fetchone()
        if item:
            conn.execute(
                "UPDATE batch_task_items SET status='failed',error_message=?,updated_at=? WHERE id=?",
                (reason, now(), int(item["id"])),
            )
    if item:
        sync_batch(int(item["batch_id"]))
    return {"task_id": task_id, "status": "failed"}


def skip_generation_task_and_continue(task_id: int) -> dict:
    """Skip one blocked batch child without treating it as an execution failure."""
    task_id = int(task_id)
    terminate_process(task_id)
    stamp = now()
    with connect() as conn:
        task = conn.execute(
            "SELECT status,task_name FROM generation_tasks WHERE id=?", (task_id,)
        ).fetchone()
        item = conn.execute(
            "SELECT id,batch_id,index_no FROM batch_task_items WHERE task_id=?", (task_id,)
        ).fetchone()
        if not task or not item:
            raise ValueError("只有批量任务中的异常子任务可以跳过")
        status = canonical_task_status(task["status"])
        if status not in {"failed", "stalled", "paused"}:
            raise ValueError("当前任务状态不能跳过")
        reason = "用户已跳过此条，批次将继续执行下一条"
        conn.execute(
            """UPDATE generation_tasks SET status='cancelled',current_stage='cancelled',
               current_step='已跳过',current_shot='已跳过',worker_id='',process_id=0,
               process_name='',ffmpeg_pid=0,ffprobe_pid=0,error_code='SKIPPED_BY_USER',
               error_message=?,finished_at=?,updated_at=? WHERE id=?""",
            (reason, stamp, stamp, task_id),
        )
        conn.execute(
            "UPDATE batch_task_items SET status='cancelled',error_message=?,updated_at=? WHERE id=?",
            (reason, stamp, int(item["id"])),
        )
        conn.execute(
            "UPDATE batch_tasks SET status='queued',finished_at='',updated_at=? WHERE id=?",
            (stamp, int(item["batch_id"])),
        )
    append_task_log(task_id, reason)
    sync_batch(int(item["batch_id"]))
    BATCH_QUEUE.enqueue(int(item["batch_id"]), start_batch_generation)
    return {
        "task_id": task_id,
        "batch_id": int(item["batch_id"]),
        "status": "cancelled",
        "continued": True,
    }


def recover_standalone_generation_queue() -> list[int]:
    """Restore queued non-batch jobs after the durable batch queue is restored."""
    task_ids = [
        int(item["id"])
        for item in rows(
            """SELECT g.id FROM generation_tasks g
               WHERE g.status IN ('queued','pending')
                 AND NOT EXISTS(SELECT 1 FROM batch_task_items bi WHERE bi.task_id=g.id)
               ORDER BY CASE WHEN COALESCE(g.queue_seq,0)>0 THEN g.queue_seq ELSE g.id END,g.id"""
        )
    ]
    for task_id in task_ids:
        start_generate(task_id)
    return task_ids


def template_script_snapshot(template_id: int) -> tuple[str, list[str]]:
    texts = [
        normalize_rewrite_text(item.get("ai_text") or item.get("original_text") or "")
        for item in rows(
            "SELECT original_text,ai_text FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
            (template_id,),
        )
    ]
    return "\x1f".join(texts), texts


def fallback_template_to_original(template_id: int) -> tuple[str, list[str]]:
    """Clear stale AI output and return the exact original-script snapshot."""
    shots = rows(
        "SELECT id,original_text FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
        (int(template_id),),
    )
    texts = [normalize_rewrite_text(item.get("original_text") or "") for item in shots]
    if not shots or any(not text for text in texts):
        raise AIRequestError(
            "AI_PROVIDER_ERROR",
            "AI\u6587\u6848\u751f\u6210\u5931\u8d25\uff0c\u4e14\u5b58\u5728\u6ca1\u6709\u539f\u59cb\u6587\u6848\u7684\u955c\u5934\u3002",
        )
    with connect() as conn:
        for item in shots:
            conn.execute(
                """UPDATE template_shots SET ai_text='',voice_file_path='',voice_duration=0,
                   estimated_duration=0,subtitle_file_path='',subtitle_render_hash='',voice_text_hash='',updated_at=?
                   WHERE id=?""",
                (now(), int(item["id"])),
            )
            conn.execute(
                "DELETE FROM generation_shot_materials WHERE shot_id=? AND task_id IS NULL",
                (int(item["id"]),),
            )
    return "\x1f".join(texts), texts


def batch_ai_retry_logger(task_id: int):
    """Translate backend retry events into truthful per-task progress logs."""
    labels = {
        "AI_TIMEOUT": "AI\u6587\u6848\u751f\u6210\u8d85\u65f6",
        "AI_NETWORK_ERROR": "AI\u670d\u52a1\u7f51\u7edc\u8fde\u63a5\u5931\u8d25",
        "AI_RATE_LIMITED": "AI\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41",
        "AI_PROVIDER_ERROR": "AI\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528",
    }

    def report(event: dict) -> None:
        phase = str(event.get("phase") or "")
        attempt = int(event.get("attempt") or 0)
        total = int(event.get("total") or 3)
        if phase == "attempt":
            heartbeat(
                task_id, stage="ai_rewrite",
                step=f"AI\u6539\u5199 \u7b2c {attempt}/{total} \u6b21\u5c1d\u8bd5",
                progress=min(3, attempt),
                log=f"AI\u6539\u5199 \u00b7 \u7b2c {attempt}/{total} \u6b21\u5c1d\u8bd5",
            )
        elif phase == "retry":
            delay = float(event.get("delay") or 0)
            label = labels.get(str(event.get("code") or ""), "AI\u8bf7\u6c42\u5931\u8d25")
            append_task_log(task_id, f"{label}\uff0c{delay:g}\u79d2\u540e\u91cd\u8bd5")
        elif phase == "failed":
            code = str(event.get("code") or "AI_PROVIDER_ERROR")
            append_task_log(task_id, f"AI\u6539\u5199\u6700\u7ec8\u5931\u8d25 \u00b7 \u9519\u8bef\u7801={code}")

    return report


def rewrite_batch_or_fallback(task_id: int, template_id: int, *, variation: str,
                              temperature: float) -> dict:
    """Run AI rewrite with retry and degrade to original copy when possible."""
    try:
        from generation_performance import measure, event
        import time
        ai_started = time.perf_counter()
        event("ai_start")
        event("ai_request_start")
        with measure("ai_copywriting"):
            rewrite_template_shots(
                int(template_id), variation_context=variation, temperature=temperature,
                retry_callback=batch_ai_retry_logger(int(task_id)),
            )
        event("ai_request_end", time.perf_counter() - ai_started)
        event("ai_response_parse", 0.0)
        event("ai_result_save", 0.0)
        signature, texts = template_script_snapshot(int(template_id))
        return {"used_original": False, "signature": signature, "texts": texts, "error_code": ""}
    except (AIRequestError, ServiceError, TimeoutError, ConnectionError, ValueError) as exc:
        # Only the known missing-config ValueError is a degradable AI failure.
        # Preserve unrelated validation/programming errors rather than hiding
        # them behind an original-copy fallback.
        if isinstance(exc, ValueError) and "配置并启用 AI 服务" not in str(exc):
            raise
        error_code = str(getattr(exc, "code", "") or "AI_PROVIDER_ERROR")
        signature, texts = fallback_template_to_original(int(template_id))
        if error_code == "AI_AUTH_FAILED":
            message = "AI\u914d\u7f6e\u65e0\u6548\uff0c\u672c\u6761\u5df2\u4f7f\u7528\u539f\u6587\u7ee7\u7eed\u751f\u6210"
        else:
            message = "AI\u6539\u5199\u6682\u65f6\u4e0d\u53ef\u7528\uff0c\u672c\u6761\u5df2\u4f7f\u7528\u539f\u6587\u7ee7\u7eed\u751f\u6210"
        heartbeat(
            int(task_id), stage="ai_rewrite_fallback", step=message, progress=3,
            log=f"{message} \u00b7 \u9519\u8bef\u7801={error_code}",
        )
        return {"used_original": True, "signature": signature, "texts": texts, "error_code": error_code}


def start_batch_generation(task_id: int):
    """Generate a unique script inside the globally claimed generation slot."""
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

    def prepare_unique_script():
        heartbeat(
            task_id, status="preparing", stage="ai_rewrite",
            step=f"\u751f\u6210\u7b2c {context['index_no']} \u6761\u72ec\u7acb\u6587\u6848", progress=1,
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
                previous_examples.append(f"\u7248\u672c{item['index_no']} " + " | ".join(texts))

        current_signature = ""
        for attempt in range(1, 4):
            check_cancelled(task_id)
            heartbeat(
                task_id, stage="ai_rewrite",
                step=f"\u6587\u6848\u5dee\u5f02\u68c0\u67e5 \u7b2c {attempt}/3 \u8f6e",
                progress=min(3, attempt),
            )
            variation = (
                f"\u5f53\u524d\u9700\u8981\u751f\u6210\u7b2c {context['index_no']} \u6761 \u5171 {context['total_count']} \u6761 "
                f"\u8fd9\u662f\u7b2c {attempt} \u6b21\u5dee\u5f02\u5316\u5c1d\u8bd5 \u5fc5\u987b\u4e0e\u5176\u4ed6\u7248\u672c\u660e\u663e\u4e0d\u540c"
            )
            if previous_examples:
                variation += (
                    "\n\u7981\u6b62\u590d\u7528\u4ee5\u4e0b\u5df2\u751f\u6210\u7248\u672c\u7684\u5b8c\u6574\u53e5\u5f0f\uff1a\n"
                    + "\n".join(previous_examples[-8:])[:5000]
                )
            rewrite_result = rewrite_batch_or_fallback(
                task_id, context["template_id"], variation=variation,
                temperature=min(0.95, 0.72 + attempt * 0.08),
            )
            current_signature = rewrite_result["signature"]
            if rewrite_result["used_original"]:
                return
            if current_signature and current_signature not in previous_signatures:
                break
        if not current_signature or current_signature in previous_signatures:
            raise ValueError("\u0041\u0049 \u8fde\u7eed\u751f\u6210\u4e86\u91cd\u590d\u6587\u6848\uff0c\u8bf7\u91cd\u8bd5\u6216\u8c03\u6574\u539f\u59cb\u6587\u6848")

    # Batch continuations outrank standalone work already waiting in memory so
    # one batch stays contiguous: 001 -> 002 -> 003.
    return start_generate(task_id, before_generation=prepare_unique_script)


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
            smart_result = smart_split_get(parsed.path, parse_qs(parsed.query))
            if smart_result is not None:
                return self.json_response({"ok": True, "data": smart_result})
            if parsed.path == "/api/bootstrap":
                FRONTEND_CONNECTED_EVENT.set()
                startup_log(
                    "frontend_connected",
                    client=str(self.client_address[0] if self.client_address else ""),
                    path=parsed.path,
                )
                return self.json_response({"ok": True, "data": bootstrap()})
            if parsed.path == "/api/style-presets":
                return self.json_response({"ok": True, "data": style_presets()})
            if parsed.path == "/api/runtime/media":
                return self.json_response({"ok": True, "data": get_media_runtime_status()})
            if parsed.path == "/api/video-acceleration":
                return self.json_response({"ok": True, "data": video_acceleration_status()})
            if parsed.path == "/api/materials/root":
                return self.json_response({"ok": True, "data": material_root_status()})
            if parsed.path == "/api/dashboard/tasks":
                return self.json_response({"ok": True, "data": dashboard_task_snapshot()})
            if parsed.path == "/api/task-center/list":
                query = parse_qs(parsed.query)
                value = lambda key, default="": (query.get(key) or [default])[0]
                result = task_center_list(
                    view=value("view", "current"), page=int(value("page", 1) or 1),
                    page_size=int(value("page_size", TASK_CENTER_PAGE_SIZE) or TASK_CENTER_PAGE_SIZE),
                    status=value("status"), project_id=int(value("project_id", 0) or 0),
                    date_from=value("date_from"), date_to=value("date_to"), keyword=value("keyword"),
                )
                return self.json_response({"ok": True, "data": result})
            if parsed.path == "/api/task-center/tasks":
                query = parse_qs(parsed.query)
                value = lambda key, default="": (query.get(key) or [default])[0]
                result = task_center_flat_list(
                    view=value("view", "current"), page=int(value("page", 1) or 1),
                    page_size=int(value("page_size", TASK_CENTER_PAGE_SIZE) or TASK_CENTER_PAGE_SIZE),
                    status=value("status"), project_id=int(value("project_id", 0) or 0),
                    batch_id=int(value("batch_id", 0) or 0), date_from=value("date_from"),
                    date_to=value("date_to"), keyword=value("keyword"),
                    focus_task_id=int(value("focus_task_id", 0) or 0),
                )
                return self.json_response({"ok": True, "data": result})
            if parsed.path == "/api/tasks/current":
                current = task_center_flat_list(view="current", page=1, page_size=100)
                summary = dict(current.get("summary") or {})
                summary["total_count"] = sum(summary.values())
                summary["tasks"] = current.get("items") or []
                summary["queue"] = current.get("queue") or {}
                return self.json_response({"ok": True, "data": summary})
            if parsed.path == "/api/generation-queue/status":
                return self.json_response({"ok": True, "data": get_generation_queue_status()})
            if parsed.path == "/api/tasks/invalid-scan":
                return self.json_response({"ok": True, "data": scan_invalid_generation_tasks(False)})
            task_items_match = re.fullmatch(r"/api/task-center/(batch|single)/(\d+)/items", parsed.path)
            if task_items_match:
                result = task_center_items(task_items_match.group(1), int(task_items_match.group(2)))
                return self.json_response({"ok": True, "data": result})
            task_log_match = re.fullmatch(r"/api/tasks/(\d+)/logs", parsed.path)
            if task_log_match:
                return self.json_response({"ok": True, "data": task_log_details(int(task_log_match.group(1)))})
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
                return self.json_response({"ok": True, "data": {**publishing_capabilities(), "adapters": adapter_catalog(), "publishers": publisher_catalog(include_guarded=True)}})
            preflight_match = re.fullmatch(r"/api/oauth/(youtube|facebook_page|facebook-page|instagram_business|instagram-business|x)/preflight", parsed.path)
            if preflight_match:
                query = parse_qs(parsed.query)
                account_id = int((query.get("account_id") or [0])[0] or 0)
                return self.json_response({"ok": True, "data": provider_preflight(preflight_match.group(1), account_id)})
            session_match = re.fullmatch(r"/api/oauth/session/([^/]+)", parsed.path)
            if session_match:
                return self.json_response({"ok": True, "data": session_status(session_match.group(1))})
            callback_match = re.fullmatch(r"/api/oauth/(youtube|facebook-page|instagram-business|x)/callback", parsed.path)
            if callback_match:
                query = parse_qs(parsed.query)
                state = (query.get("state") or [""])[0]
                if query.get("error"):
                    code = query["error"][0]
                    description = (query.get("error_description") or [""])[0]
                    raise ValueError(cancel_authorization(state, code + ((": " + description) if description else "")))
                result = complete_authorization(state, (query.get("code") or [""])[0], callback_match.group(1))
                return self.json_response({"ok": True, "data": result})
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
                start, end, status = 0, max(0, size - 1), 200
                range_header = str(self.headers.get("Range") or "").strip()
                if range_header.startswith("bytes="):
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                    if not match:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    elif last:
                        suffix = min(int(last), size)
                        start, end = size - suffix, size - 1
                    if start >= size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = 206
                content_length = max(0, end - start + 1)
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(content_length))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with file_path.open("rb") as f:
                    f.seek(start)
                    remaining = content_length
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            if parsed.path == "/settings" or parsed.path.startswith("/settings/"):
                self.path = "/index.html"
            return super().do_GET()
        except Exception as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        data = self.read_json()
        try:
            smart_result = smart_split_post(parsed.path, data, scan_materials)
            if smart_result is not None:
                return self.json_response({"ok": True, "data": smart_result})
            result = None
            if parsed.path == "/api/select-folder": result = {"path": select_folder()}
            elif parsed.path == "/api/materials/open-root": result = open_material_root()
            elif parsed.path == "/api/select-cover": result = {"path": select_cover_file()}
            elif parsed.path == "/api/select-google-oauth-client":
                if not is_admin_mode():
                    raise ValueError("只有管理员可以导入平台 OAuth 配置")
                result = select_google_oauth_client()
            elif re.fullmatch(r"/api/oauth/(youtube|facebook_page|facebook-page|instagram_business|instagram-business|x)/config", parsed.path):
                if not is_admin_mode():
                    raise ValueError("只有管理员可以配置平台开发者接入")
                provider = parsed.path.split("/")[3]
                result = save_provider_config(provider, data)
            elif re.fullmatch(r"/api/oauth/(youtube|facebook_page|facebook-page|instagram_business|instagram-business|x)/start", parsed.path):
                provider = parsed.path.split("/")[3]
                account_id = int(data.get("account_id") or 0)
                platform, account = publishing_account_context(account_id)
                expected = {"facebook_page": "facebook", "facebook-page": "facebook", "instagram_business": "instagram", "instagram-business": "instagram"}.get(provider, provider)
                if str(platform.get("code")) != expected:
                    raise ValueError("当前账号与授权平台不一致")
                result = start_authorization(platform, account)
            elif re.fullmatch(r"/api/oauth/session/[^/]+/select", parsed.path):
                state = parsed.path.split("/")[4]
                result = select_oauth_account(state, str(data.get("external_id") or ""))
            elif re.fullmatch(r"/api/oauth/session/[^/]+/cancel", parsed.path):
                state = parsed.path.split("/")[4]
                result = {"status": "authorization_cancelled", "message": cancel_authorization(state)}
            elif re.fullmatch(r"/api/oauth/session/[^/]+/reopen", parsed.path):
                state = parsed.path.split("/")[4]
                result = reopen_authorization(state)
            elif parsed.path == "/api/select-voice-sample": result = {"path": select_voice_sample()}
            elif parsed.path == "/api/update/check": result = check_configured_update()
            elif parsed.path == "/api/update/apply": result = apply_configured_update()
            elif parsed.path == "/api/materials/scan": result = scan_materials(str(data.get("folder_path", "")))
            elif parsed.path == "/api/materials/category":
                with connect() as conn: conn.execute("UPDATE materials SET category_id=?,updated_at=? WHERE id=?", (int(data.get("category_id") or 0) or None, now(), int(data["material_id"])))
                result = True
            elif parsed.path == "/api/bgm/scan":
                folder = str(data.get("folder_path", "")); result = scan_bgm_folder(folder)
                with connect() as conn: conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('bgm_dir',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (folder, now()))
            elif parsed.path == "/api/sfx/scan":
                folder = str(data.get("folder_path", ""))
                result = scan_sfx_folder(folder)
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
            elif parsed.path == "/api/publish/groups": result = {"id": save_account_group(data)}
            elif re.fullmatch(r"/api/publish/groups/\d+", parsed.path): result = {"id": save_account_group(data, int(parsed.path.split("/")[4]))}
            elif re.fullmatch(r"/api/publish/groups/\d+/copy", parsed.path): result = {"id": copy_account_group(int(parsed.path.split("/")[4]))}
            elif parsed.path == "/api/publish/group-batches/preflight": result = group_publish_preflight(data)
            elif parsed.path == "/api/publish/group-batches": result = create_group_publish_batch(data)
            elif re.fullmatch(r"/api/publish/group-batches/\d+/retry-failed", parsed.path):
                result = retry_group_batch_failed(int(parsed.path.split("/")[4]))
            elif parsed.path == "/api/publish/tasks":
                if not str(data.get("platform") or "").strip():
                    raise ValueError("缺少发布平台参数，请重新选择平台")
                publish_task_id = create_publish_task(data)
                with connect() as conn:
                    publish_task = conn.execute("SELECT status FROM publish_tasks WHERE id=?", (publish_task_id,)).fetchone()
                result = {"id": publish_task_id, "status": publish_task["status"] if publish_task else "pending"}
            elif parsed.path == "/api/publish/sync-output": result = sync_missing_publish_videos()
            elif parsed.path == "/api/platform-tests": result = {"id": create_platform_test(data), "status": "pending"}
            elif re.fullmatch(r"/api/platform-tests/\d+/retry", parsed.path):
                result = {"id": retry_platform_test(int(parsed.path.split("/")[3])), "status": "pending"}
            elif parsed.path == "/api/publish/ai-content":
                result = generate_publish_content(
                    int(data.get("video_id") or 0), str(data.get("platform") or ""),
                    int(data.get("profile_id") or 0) or None, str(data.get("current_title") or ""),
                    str(data.get("current_tags") or ""), str(data.get("current_cover_text") or ""),
                )
            elif re.fullmatch(r"/api/publish/drafts/\d+/metadata", parsed.path):
                task_id = int(parsed.path.split("/")[4])
                result = save_draft_field(
                    task_id,
                    str(data.get("field") or ""),
                    str(data.get("value") or ""),
                    str(data.get("state") or "manually_edited"),
                )
            elif re.fullmatch(r"/api/publish/tasks/\d+/enqueue", parsed.path):
                result = enqueue_publish_task(int(parsed.path.split("/")[4]))
                PUBLISH_WORKER.wake()
            elif re.fullmatch(r"/api/publish/accounts/\d+/login", parsed.path):
                result = start_account_login(
                    int(parsed.path.split("/")[4]),
                    str(data.get("login_mode") or "qr"),
                )
            elif re.fullmatch(r"/api/publish/accounts/\d+/login/status", parsed.path):
                account_id = int(parsed.path.split("/")[4])
                platform, account = publishing_account_context(account_id)
                platform_code = str(platform.get("code") or "")
                if platform_code == "tiktok":
                    result = probe_tiktok_login(account_id, account["browser_profile_path"]) if data.get("check") else tiktok_login_status(account_id, account)
                elif platform_code in DOMESTIC_LOGIN_PLATFORMS:
                    result = domestic_login_status(account_id, {**account, "platform_code": platform_code})
                else:
                    raise ValueError("当前平台不支持登录会话状态查询")
            elif re.fullmatch(r"/api/publish/accounts/\d+/login/cancel", parsed.path):
                account_id = int(parsed.path.split("/")[4])
                platform, _account = publishing_account_context(account_id)
                platform_code = str(platform.get("code") or "")
                if platform_code == "tiktok":
                    result = cancel_tiktok_login(account_id)
                elif platform_code in DOMESTIC_LOGIN_PLATFORMS:
                    result = cancel_domestic_login(account_id)
                else:
                    raise ValueError("当前平台不支持取消登录会话")
            elif re.fullmatch(r"/api/publish/accounts/\d+/login/reopen", parsed.path):
                result = start_account_login(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/publish/accounts/\d+/authorize", parsed.path):
                result = start_account_login(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/publish/accounts/\d+/check", parsed.path):
                result = check_account_login(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/publish/accounts/\d+/open-profile", parsed.path):
                result = open_account_profile(int(parsed.path.split("/")[4]))
            elif parsed.path == "/api/profiles/ai-suggestions": result = generate_profile_suggestions(data)
            elif parsed.path == "/api/ai-services": result = {"id": save_ai_service(data)}
            elif parsed.path == "/api/ai-services/open-bailian-key": result = open_bailian_api_key_page()
            elif parsed.path == "/api/ai-services/test-config":
                result = test_ai_service(resolve_ai_test_config(data))
            elif re.fullmatch(r"/api/ai-services/\d+/test", parsed.path):
                config = runtime_config("ai_service_configs", int(parsed.path.split("/")[3])); result = test_ai_service(config or {})
            elif parsed.path == "/api/tts-services/test-config":
                config = dict(data.get("config") or {})
                if config.get("id") and not str(config.get("api_key") or "").strip():
                    stored = runtime_config("tts_service_configs", int(config["id"])) or {}
                    stored.update({key: value for key, value in config.items() if key != "api_key"})
                    config = stored
                provider = str(config.get("provider") or "")
                if provider == "aliyun_bailian":
                    voice_info = list_bailian_voices(config)[0]
                    voice = {"voice_code": str(data.get("voice_code") or voice_info["voice_code"])}
                else:
                    voice = row("SELECT * FROM tts_voices WHERE service_id=? ORDER BY is_default DESC,id LIMIT 1", (int(config.get("id") or 0),))
                if not voice: raise ValueError("请先配置一个可用音色")
                path, duration = test_tts_service(config, voice, settings(), str(data.get("text") or ""))
                result = {"voice_file_path": path, "duration": duration, "voice_url": media_url(path)}
            elif parsed.path == "/api/tts-services": result = {"id": save_tts_service(data)}
            elif re.fullmatch(r"/api/tts-services/\d+/test", parsed.path):
                service_id = int(parsed.path.split("/")[3]); config = runtime_config("tts_service_configs", service_id); voice = row("SELECT * FROM tts_voices WHERE service_id=? ORDER BY is_default DESC,id LIMIT 1", (service_id,))
                if not config or not voice: raise ValueError("TTS 服务不存在或尚未配置音色")
                path, duration = test_tts_service(config, voice, settings(), str(data.get("text") or "")); result = {"voice_file_path": path, "duration": duration, "voice_url": media_url(path)}
            elif re.fullmatch(r"/api/tts-services/\d+/voices/sync", parsed.path):
                result = sync_tts_voices(int(parsed.path.split("/")[3]))
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
            elif parsed.path == "/api/tasks/start-single": result = create_and_start_single_task(data)
            elif re.fullmatch(r"/api/dashboard/tasks/\d+/open-output", parsed.path):
                result = open_task_output(int(parsed.path.split("/")[4]))
            elif re.fullmatch(r"/api/tasks/\d+/open-log", parsed.path):
                result = open_task_log(int(parsed.path.split("/")[3]))
            elif parsed.path == "/api/batches":
                result = create_batch_task(data, detailed=True)
                if int(result.get("created_count") or 0) <= 0 or len(result.get("task_ids") or []) != int(result.get("created_count") or 0):
                    raise RuntimeError("批量任务创建失败：没有写入有效的视频生成任务")
                BATCH_QUEUE.enqueue(int(result["batch_id"]), start_batch_generation)
                result["id"] = int(result["batch_id"])
            elif parsed.path == "/api/tasks/clear-completed":
                with connect() as conn:
                    completed_ids = [r["id"] for r in conn.execute("SELECT id FROM generation_tasks WHERE status IN ('succeeded','completed')").fetchall()]
                    if completed_ids:
                        marks = ",".join("?" for _ in completed_ids)
                        conn.execute(f"DELETE FROM generation_shot_materials WHERE task_id IN ({marks})", completed_ids)
                    result = {"deleted": conn.execute("DELETE FROM generation_tasks WHERE status IN ('succeeded','completed')").rowcount}
            elif parsed.path == "/api/task-center/delete-records":
                result = delete_task_center_records(list(data.get("records") or []), bool(data.get("delete_outputs")))
            elif parsed.path == "/api/task-center/delete-old-completed":
                result = delete_old_completed_records(bool(data.get("delete_outputs")))
            elif parsed.path == "/api/tasks/bulk-delete":
                result = delete_generation_task_records(list(data.get("task_ids") or []), bool(data.get("delete_outputs")))
            elif parsed.path == "/api/tasks/cleanup-records":
                result = cleanup_generation_task_records(list(data.get("statuses") or []), str(data.get("age") or "all"), bool(data.get("delete_outputs")))
            elif parsed.path == "/api/tasks/cleanup-invalid":
                result = scan_invalid_generation_tasks(True)
            elif parsed.path == "/api/generation-queue/repair":
                result = repair_generation_queue()
            elif re.fullmatch(r"/api/tasks/\d+/generate", parsed.path): result = start_generate(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/tasks/\d+/retry", parsed.path): result = retry_generation_task(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/tasks/\d+/cancel", parsed.path): result = cancel_task(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/tasks/\d+/mark-failed", parsed.path): result = mark_generation_failed(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/tasks/\d+/skip-and-continue", parsed.path): result = skip_generation_task_and_continue(int(parsed.path.split("/")[3]))
            elif re.fullmatch(r"/api/tasks/\d+/delete-and-continue", parsed.path): result = delete_generation_task_and_continue(int(parsed.path.split("/")[3]))
            elif parsed.path == "/api/settings":
                data.pop("ffmpeg_path", None)
                data.pop("ffprobe_path", None)
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
                result = public_settings()
            elif parsed.path == "/api/runtime/media/recheck":
                result = get_media_runtime_status(refresh=True)
            elif parsed.path == "/api/title-template":
                result = save_title_template(data)
            elif parsed.path == "/api/title-template/activate":
                result = activate_title_template(data.get("template_scope"))
            elif parsed.path == "/api/title-template/reset":
                result = reset_title_template(data.get("template_scope"))
            elif parsed.path == "/api/title-preview":
                result = generate_title_preview(data)
            else: return self.json_response({"ok": False, "error": "接口不存在"}, 404)
            return self.json_response({"ok": True, "data": result})
        except AIRequestError as exc:
            return self.json_response({
                "ok": False,
                "error": str(exc),
                "code": exc.code,
            }, 400)
        except PublishAIError as exc:
            return self.json_response({
                "ok": False,
                "error": exc.user_message,
                "code": exc.code,
                "log_id": exc.log_id,
            }, 400)
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
            group_match = re.fullmatch(r"/api/publish/groups/(\d+)", parsed.path)
            if group_match:
                delete_account_group(int(group_match.group(1)))
                return self.json_response({"ok": True})
            publish_match = re.fullmatch(r"/api/publish/accounts/(\d+)", parsed.path)
            if publish_match:
                with connect() as conn:
                    conn.execute("DELETE FROM oauth_sessions WHERE account_id=?", (int(publish_match.group(1)),))
                    conn.execute("DELETE FROM oauth_tokens WHERE account_id=?", (int(publish_match.group(1)),))
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
    # Retain legacy columns/keys for schema compatibility, but purge values so
    # paths from another machine can never leak back into runtime behavior/UI.
    with connect() as conn:
        conn.execute(
            "UPDATE app_settings SET value='',updated_at=? WHERE key IN ('ffmpeg_path','ffprobe_path') AND value<>''",
            (now(),),
        )
    recover_smart_split_jobs()
    migrate_provider_secrets()
    repair_missing_publish_records()
    with connect() as conn:
        # Seed only empty prompts; administrator customizations are preserved.
        conn.execute(
            "UPDATE ai_service_configs SET system_prompt=? WHERE COALESCE(TRIM(system_prompt),'')=''",
            (DEFAULT_REWRITE_PROMPT,),
        )
    cfg = settings()
    output = cfg.get("output_dir", "")
    if not output or not Path(output).parent.exists():
        with connect() as conn:
            conn.execute("UPDATE app_settings SET value=?,updated_at=? WHERE key='output_dir'", (str(APP_DIR / "outputs"), now()))
    # Consumers exist for the whole application lifetime.  Recovery merely
    # registers durable DB rows; it no longer owns worker lifecycle.
    ensure_generation_worker_running()
    recover_and_enqueue(start_batch_generation)
    recover_standalone_generation_queue()
    start_generation_watchdog()
    start_publish_worker()
