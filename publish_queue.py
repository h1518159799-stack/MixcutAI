"""Database-backed publish task queue. No upload worker is started in phase one."""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from pathlib import Path

from db import connect, now
from cover_service import normalize_cover_path
from publishers import get_publisher
from publish_safety import (
    ACTIVE_STATUSES, GUARDED_DOMESTIC_PLATFORMS, PublishGuardError,
    guarded_error, map_publish_exception, preflight_douyin_publish_check, preflight_publish_check, recover_guarded_channels,
    status_for_error,
)
from runtime_paths import DATA_DIR


SUPPORTED_PLATFORM_CODES = {"douyin", "tiktok", "youtube", "instagram", "facebook", "xiaohongshu", "kuaishou", "wechat_channels", "bilibili"}
PUBLISH_QUEUE_BUILD_MARKER = "ACCOUNT_REBIND_20260912_02"
PUBLISH_QUEUE_SOURCE_REVISION = "BFC850579EE83E08BEDCFB858AAFCD6104E4603FBB34415F2A128DCF4994BBFC"
def normalize_platform(value: str) -> str:
    v = str(value or '').strip().lower()
    return 'wechat_channels' if v in {'视频号','wechat_channels','wechat_channel','channels','wechat','wx_channels'} else v
TASK_STATUSES = {"pending", "queued", "publishing", "success", "failed", "draft", "prechecking", "ready", "uploading", "submitting", "processing", "waiting_confirmation", "succeeded", "needs_login", "needs_manual_action", "cancelled"}
CREATE_TASK_LOCK = threading.Lock()
BATCH_QUEUED_STATUSES = {"pending", "ready", "queued", "prechecking"}
BATCH_RUNNING_STATUSES = {"publishing", "uploading", "submitting", "processing", "waiting_confirmation"}
BATCH_SUCCESS_STATUSES = {"success", "succeeded"}
BATCH_NEEDS_ACTION_STATUSES = {"needs_login", "needs_manual_action"}
BATCH_SKIPPED_STATUSES = {"skipped", "blocked"}

PLATFORM_LABELS = {
    "douyin": "抖音", "wechat_channels": "视频号", "xiaohongshu": "小红书",
    "kuaishou": "快手", "bilibili": "B站", "tiktok": "TikTok",
    "facebook": "Facebook Page", "youtube": "YouTube", "instagram": "Instagram Business",
}


def platform_label(code: str) -> str:
    return PLATFORM_LABELS.get(str(code or "").strip(), str(code or "未知平台"))


def _publisher_adapter_name(code: str) -> str:
    # This is the public routing identity recorded in diagnostics.  Domestic
    # guarded publishers share an execution wrapper, so retain the concrete
    # platform adapter name rather than making them look like Douyin.
    return {
        "douyin": "BrowserPublisher",
        "wechat_channels": "WechatChannelsPostFlowPublisher",
        "xiaohongshu": "XiaohongshuBrowserAdapter",
        "bilibili": "BilibiliBrowserAdapter",
        "tiktok": "TikTokBrowserAdapter",
        "facebook": "FacebookAdapter",
    }.get(str(code or ""), type(get_publisher(code)).__name__)


def _write_route_log(*, task_id=0, selected_platform="", account_id=0, account_platform="", publisher_adapter="", publish_url="", status="", error_stage="", error_message="") -> None:
    try:
        path = DATA_DIR / "logs" / "publish-routing.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": str(task_id), "selected_platform": str(selected_platform or ""),
            "account_id": str(account_id or ""), "account_platform": str(account_platform or ""),
            "publisher_adapter": str(publisher_adapter or ""), "publish_url": str(publish_url or ""),
            "status": str(status or ""), "error_stage": str(error_stage or ""),
            "error_message": str(error_message or "")[:500], "created_at": now(),
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recalculate_group_batch(conn, batch_id: int) -> dict:
    states = [str(row["status"] or "pending") for row in conn.execute(
        "SELECT status FROM publish_group_batch_items WHERE batch_id=?", (batch_id,)
    ).fetchall()]
    counts = {
        "total_count": len(states),
        "queued_count": sum(state in BATCH_QUEUED_STATUSES for state in states),
        "running_count": sum(state in BATCH_RUNNING_STATUSES for state in states),
        "succeeded_count": sum(state in BATCH_SUCCESS_STATUSES for state in states),
        "failed_count": sum(state == "failed" for state in states),
        "skipped_count": sum(state in BATCH_SKIPPED_STATUSES for state in states),
        "needs_action_count": sum(state in BATCH_NEEDS_ACTION_STATUSES for state in states),
    }
    actual = counts["total_count"] - counts["skipped_count"]
    cancelled = sum(state == "cancelled" for state in states)
    if counts["queued_count"] or counts["running_count"] or counts["needs_action_count"]:
        status = "publishing"
    elif counts["total_count"] and counts["skipped_count"] == counts["total_count"]:
        status = "all_skipped"
    elif actual and counts["succeeded_count"] == actual and counts["skipped_count"] == 0:
        status = "succeeded"
    elif counts["succeeded_count"]:
        status = "partial_success"
    elif actual and counts["failed_count"] == actual:
        status = "failed"
    elif cancelled and cancelled + counts["skipped_count"] == counts["total_count"]:
        status = "cancelled"
    else:
        status = "pending"
    stamp = now()
    finished = "" if status in {"pending", "publishing"} else stamp
    conn.execute(
        """UPDATE publish_group_batches SET total_count=?,queued_count=?,running_count=?,
           succeeded_count=?,success_count=?,failed_count=?,skipped_count=?,needs_action_count=?,
           status=?,updated_at=?,finished_at=? WHERE id=?""",
        (counts["total_count"], counts["queued_count"], counts["running_count"],
         counts["succeeded_count"], counts["succeeded_count"], counts["failed_count"],
         counts["skipped_count"], counts["needs_action_count"], status, stamp, finished, batch_id),
    )
    return {**counts, "status": status}


def sync_group_item_for_task(conn, task_id: int, status: str, error_message: str = "", error_code: str = "") -> None:
    item = conn.execute(
        "SELECT batch_id FROM publish_group_batch_items WHERE task_id=?", (task_id,)
    ).fetchone()
    if not item:
        return
    # Group-batch history used ``success`` before the strict browser state
    # machine introduced ``succeeded``. Keep batch items backward compatible.
    item_status = "success" if status == "succeeded" else status
    conn.execute(
        """UPDATE publish_group_batch_items SET status=?,blocked_reason=?,error_code=?,updated_at=?
           WHERE task_id=?""",
        (item_status, str(error_message or ""), str(error_code or ""), now(), task_id),
    )
    recalculate_group_batch(conn, int(item["batch_id"]))


def normalize_tags(value) -> list[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[\s,，#]+", str(value or ""))
    result = []
    for part in parts:
        tag = str(part).strip().lstrip("#")[:50]
        if tag and tag not in result:
            result.append(tag)
    return result[:30]


def _create_publish_task(data: dict) -> int:
    generation_task_id = int(data.get("generation_task_id") or 0)
    account_ids = sorted({int(value) for value in data.get("account_ids", []) if int(value) > 0})
    if not generation_task_id:
        raise ValueError("请选择已生成视频")
    if not account_ids:
        raise ValueError("请至少选择一个发布账号")
    selected_platform = normalize_platform(data.get("platform"))
    if not selected_platform:
        raise ValueError("缺少发布平台参数，请重新选择平台")
    if selected_platform not in SUPPORTED_PLATFORM_CODES:
        raise ValueError(f"发布平台不受支持：{selected_platform}")
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("请输入发布标题")
    cover_path = str(data.get("cover_path") or "").strip()
    with connect() as conn:
        video = conn.execute(
            "SELECT id,task_name,output_path,status FROM generation_tasks WHERE id=?",
            (generation_task_id,),
        ).fetchone()
        if not video or video["status"] not in {"succeeded", "completed"} or not video["output_path"]:
            raise ValueError("所选视频尚未生成完成")
        video_path = Path(video["output_path"])
        if not video_path.is_file():
            raise ValueError("成片文件不存在，请重新生成或检查输出目录")
        marks = ",".join("?" for _ in account_ids)
        accounts = conn.execute(
            f"""SELECT a.*,p.code,p.name,p.capability_status,p.integration_type,p.login_url,
                p.cookie_domains_json,p.login_cookie_names_json FROM publish_accounts a
                JOIN platforms p ON p.id=a.platform_id
                WHERE a.status='active' AND p.enabled=1 AND a.id IN ({marks})""",
            tuple(account_ids),
        ).fetchall()
        if len(accounts) != len(account_ids):
            if selected_platform == "wechat_channels":
                raise ValueError("当前账号未绑定视频号，请重新登录绑定")
            raise ValueError("部分发布账号不存在或已停用")
        if any(normalize_platform(item["code"]) != selected_platform for item in accounts):
            if selected_platform == "wechat_channels":
                raise ValueError("当前账号未绑定视频号，请重新登录绑定")
            raise ValueError("当前账号类型与发布平台不匹配")
        publisher_adapter = _publisher_adapter_name(selected_platform)
        metadata = {
            "title": title, "description": str(data.get("description") or "").strip(),
            "tags": normalize_tags(data.get("tags")), "cover_path": cover_path,
            "schedule": str(data.get("scheduled_at") or "").strip().replace("T", " "),
        }
        guarded_preflight = {}
        for account_row in accounts:
            account = dict(account_row)
            if account["code"] in GUARDED_DOMESTIC_PLATFORMS:
                platform = {
                    "id": account["platform_id"], "code": account["code"], "name": account["name"],
                    "capability_status": account["capability_status"], "integration_type": account["integration_type"],
                }
                guarded_preflight[account["id"]] = preflight_publish_check(platform, account, str(video_path), metadata)
        unauthorized = [row["name"] for row in accounts if row["integration_type"] == "api" and row["authorization_status"] != "authorized"]
        if unauthorized:
            raise ValueError("请先完成平台授权：" + "、".join(unauthorized))
        unsupported = [row["code"] for row in accounts if row["code"] not in SUPPORTED_PLATFORM_CODES]
        if unsupported:
            raise ValueError("暂不支持所选平台: " + ", ".join(unsupported))
        cover_path = normalize_cover_path(cover_path)
        stamp = now()
        scheduled_at = str(data.get("scheduled_at") or "").strip().replace("T", " ")
        ai_hashtags = normalize_tags(data.get("ai_hashtags"))
        guarded_only = bool(accounts) and all(row["code"] in GUARDED_DOMESTIC_PLATFORMS for row in accounts)
        initial_task_status = "ready" if guarded_only else "pending"
        conn.execute(
            """INSERT INTO publish_tasks(generation_task_id,video_path,title,description,tags_json,
               cover_path,ai_title,ai_description,ai_hashtags,cover_text,ai_generated_time,
               scheduled_at,delete_after_publish,platform,status,total_channels,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (generation_task_id, str(video_path), title, str(data.get("description") or "").strip(),
             json.dumps(normalize_tags(data.get("tags")), ensure_ascii=False),
             cover_path, str(data.get("ai_title") or "").strip(), str(data.get("ai_description") or "").strip(),
             json.dumps(ai_hashtags, ensure_ascii=False), str(data.get("cover_text") or "").strip(),
             str(data.get("ai_generated_time") or "").strip(), scheduled_at,
             1 if data.get("delete_after_publish") else 0, selected_platform, initial_task_status, len(accounts), stamp, stamp),
        )
        task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for account in accounts:
            checked = guarded_preflight.get(account["id"])
            if checked:
                conn.execute(
                    """INSERT INTO publish_channels(publish_task_id,account_id,platform_id,selected_platform,account_platform,publisher_adapter,status,
                       idempotency_key,video_file_hash,title_hash,created_at,updated_at)
                       VALUES(?,?,?,?,?,?, 'ready',?,?,?,?,?)""",
                    (task_id, account["id"], account["platform_id"], selected_platform, account["code"], publisher_adapter, checked["idempotency_key"],
                     checked["video_file_hash"], checked["title_hash"], stamp, stamp),
                )
            else:
                conn.execute(
                    """INSERT INTO publish_channels(publish_task_id,account_id,platform_id,selected_platform,account_platform,publisher_adapter,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?, 'pending',?,?)""",
                    (task_id, account["id"], account["platform_id"], selected_platform, account["code"], publisher_adapter, stamp, stamp),
                )
            _write_route_log(task_id=task_id, selected_platform=selected_platform, account_id=account["id"], account_platform=account["code"], publisher_adapter=publisher_adapter, status=initial_task_status)
    return task_id


def create_publish_task(data: dict) -> int:
    # Serializes the duplicate check and channel insert in this desktop process.
    with CREATE_TASK_LOCK:
        return _create_publish_task(data)


def enqueue_publish_task(task_id: int) -> dict:
    stamp = now()
    with connect() as conn:
        task = conn.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("发布任务不存在")
        platform_codes = [row["code"] for row in conn.execute(
            """SELECT p.code FROM publish_channels c JOIN platforms p ON p.id=c.platform_id
               WHERE c.publish_task_id=?""", (task_id,)
        ).fetchall()]
        guarded_only = bool(platform_codes) and all(code in GUARDED_DOMESTIC_PLATFORMS for code in platform_codes)
        strict_browser_only = bool(platform_codes) and all(code in GUARDED_DOMESTIC_PLATFORMS or code == "douyin" for code in platform_codes)
        allowed = ("pending", "ready", "failed", "needs_login", "needs_manual_action") if strict_browser_only else ("pending", "failed")
        if task["status"] not in allowed:
            if strict_browser_only and task["status"] in ACTIVE_STATUSES:
                raise PublishGuardError("DUPLICATE_TASK")
            raise ValueError("当前任务状态不能重新入队")
        if guarded_only:
            conn.execute(
                "UPDATE publish_tasks SET status='prechecking',last_error='',error_code='',technical_log_id='',updated_at=? WHERE id=?",
                (stamp, task_id),
            )
            conn.execute(
                """UPDATE publish_channels SET status='prechecking',error_message='',error_code='',technical_log_id='',updated_at=?
                   WHERE publish_task_id=? AND status IN ('ready','failed','needs_login','needs_manual_action')""",
                (stamp, task_id),
            )
            status = "prechecking"
        else:
            # Preserve the previous browser attempt before clearing its visible
            # error fields.  This is also used by Douyin to confirm an already
            # published work before any retry upload is allowed.
            for channel in conn.execute(
                "SELECT * FROM publish_channels WHERE publish_task_id=? AND status IN ('failed','needs_login','needs_manual_action')", (task_id,)
            ).fetchall():
                try:
                    evidence = json.loads(channel["result_evidence_json"] or "{}")
                except (TypeError, ValueError):
                    evidence = {}
                attempts = list(evidence.get("attempts") or [])
                attempts.append({
                    "attempt": len(attempts) + 1, "status": channel["status"],
                    "error_code": channel["error_code"] or "UNKNOWN_ERROR",
                    "error_message": channel["error_message"] or "发布失败",
                    "technical_log_id": channel["technical_log_id"] or "",
                    "started_at": channel["started_at"] or "", "finished_at": channel["finished_at"] or "",
                })
                evidence["attempts"] = attempts
                conn.execute(
                    "UPDATE publish_channels SET result_evidence_json=? WHERE id=?",
                    (json.dumps(evidence, ensure_ascii=False), channel["id"]),
                )
            conn.execute(
                "UPDATE publish_tasks SET status='queued',last_error='',updated_at=? WHERE id=?",
                (stamp, task_id),
            )
            conn.execute(
                "UPDATE publish_channels SET status='queued',error_message='',updated_at=? WHERE publish_task_id=? AND status IN ('pending','failed')",
                (stamp, task_id),
            )
            status = "queued"
        sync_group_item_for_task(conn, task_id, status)
    return {"id": task_id, "status": status}


def retry_group_batch_failed(batch_id: int) -> dict:
    """Retry failed executable tasks only; successful and skipped rows stay untouched."""
    with connect() as conn:
        task_ids = [int(row["task_id"]) for row in conn.execute(
            """SELECT task_id FROM publish_group_batch_items
               WHERE batch_id=? AND status='failed' AND task_id IS NOT NULL ORDER BY id""",
            (batch_id,),
        ).fetchall()]
    retried = []
    errors = []
    for task_id in task_ids:
        try:
            enqueue_publish_task(task_id)
            with connect() as conn:
                conn.execute(
                    "UPDATE publish_group_batch_items SET retry_count=retry_count+1,updated_at=? WHERE batch_id=? AND task_id=?",
                    (now(), batch_id, task_id),
                )
                recalculate_group_batch(conn, batch_id)
            retried.append(task_id)
        except Exception as exc:
            errors.append({"task_id": task_id, "error": str(exc)})
    if retried:
        PUBLISH_WORKER.wake()
    return {"batch_id": batch_id, "retried_task_ids": retried, "errors": errors}


def delete_publish_task(task_id: int) -> None:
    with connect() as conn:
        task = conn.execute("""SELECT t.status,EXISTS(SELECT 1 FROM publish_channels c JOIN platforms p ON p.id=c.platform_id
                              WHERE c.publish_task_id=t.id AND p.code IN ('kuaishou','xiaohongshu','wechat_channels','bilibili')) guarded
                         FROM publish_tasks t WHERE t.id=?""", (task_id,)).fetchone()
        if not task:
            return
        if task["status"] == "publishing" or (task["guarded"] and task["status"] in ACTIVE_STATUSES):
            raise ValueError("发布中的任务不能删除")
        conn.execute("DELETE FROM publish_tasks WHERE id=?", (task_id,))


def sync_missing_publish_videos() -> dict:
    """Synchronize publish records against the configured output directory.

    The output folder is the source of truth here. This deliberately does not
    scan or delete ``generation_tasks`` records.
    """
    # Manual sync is only a fallback. Normal registration happens in the
    # generation success callback before the worker claims another task.
    from generation_success_service import repair_missing_publish_records

    repair_result = repair_missing_publish_records()
    removable_statuses = {"pending", "queued", "failed"}
    conn = connect()
    try:
        try:
            setting = conn.execute(
                "SELECT value FROM app_settings WHERE key='output_dir'"
            ).fetchone()
        except Exception:
            setting = None
        output_dir = Path(str(setting["value"])).expanduser() if setting and setting["value"] else None
        if output_dir is None or not output_dir.is_dir():
            return {
                "output_dir": str(output_dir or ""),
                "scanned_outputs": 0,
                "removed_publish_tasks": 0,
                "removed_publish_task_ids": [],
                "skipped_publishing_tasks": 0,
                "repaired_publish_records": repair_result["repaired_count"],
                "repaired_task_ids": repair_result["repaired_task_ids"],
                "message": "输出目录未配置或不存在，未删除发布记录",
            }
        video_extensions = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
        available_files = {
            path.resolve()
            for path in output_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in video_extensions
        }

        publish_tasks = conn.execute(
            """SELECT id,status,video_path FROM publish_tasks
               WHERE status IN ('pending','queued','failed','publishing')"""
        ).fetchall()
        removed_ids = []
        skipped_publishing_ids = []
        for task in publish_tasks:
            video_path = Path(str(task["video_path"] or "")).expanduser()
            if video_path.exists() and video_path.resolve() in available_files:
                continue
            task_id = int(task["id"])
            if task["status"] == "publishing":
                skipped_publishing_ids.append(task_id)
                continue
            if task["status"] in removable_statuses:
                conn.execute("DELETE FROM publish_tasks WHERE id=?", (task_id,))
                removed_ids.append(task_id)
        conn.commit()
    finally:
        conn.close()

    return {
        "output_dir": str(output_dir),
        "scanned_outputs": len(available_files),
        "removed_publish_tasks": len(removed_ids),
        "removed_publish_task_ids": removed_ids,
        "skipped_publishing_tasks": len(skipped_publishing_ids),
        "repaired_publish_records": repair_result["repaired_count"],
        "repaired_task_ids": repair_result["repaired_task_ids"],
        "repair_skipped": repair_result["skipped"],
    }


def next_queued_channel() -> dict | None:
    """Return the next due channel for the serial publish worker."""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT c.*,t.video_path,t.title,t.description,t.tags_json,t.cover_path,t.scheduled_at,
               t.delete_after_publish,a.account_name,a.browser_profile_path,p.code platform_code
               FROM publish_channels c JOIN publish_tasks t ON t.id=c.publish_task_id
               JOIN publish_accounts a ON a.id=c.account_id JOIN platforms p ON p.id=c.platform_id
               WHERE ((c.status='queued' AND t.status IN ('queued','publishing'))
                  OR (c.status='prechecking' AND t.status='prechecking' AND p.code IN ('kuaishou','xiaohongshu','wechat_channels','bilibili')))
               AND (t.scheduled_at='' OR t.scheduled_at<=?) ORDER BY t.id,c.id LIMIT 1""",
            (now(),),
        ).fetchone()
        return dict(row) if row else None


def _finish_task(task_id: int) -> None:
    with connect() as conn:
        states = conn.execute(
            "SELECT status FROM publish_channels WHERE publish_task_id=?", (task_id,)
        ).fetchall()
        success = sum(1 for item in states if item["status"] in {"success", "succeeded"})
        failed = sum(1 for item in states if item["status"] == "failed")
        pending = sum(1 for item in states if item["status"] in ("pending", "queued", "publishing", "uploading", "submitting", "processing", "waiting_confirmation"))
        if pending:
            return
        codes = [row["code"] for row in conn.execute(
            """SELECT p.code FROM publish_channels c JOIN platforms p ON p.id=c.platform_id
               WHERE c.publish_task_id=?""", (task_id,)
        ).fetchall()]
        guarded_only = bool(codes) and all(code in GUARDED_DOMESTIC_PLATFORMS for code in codes)
        verified_browser_only = bool(codes) and all(code in GUARDED_DOMESTIC_PLATFORMS or code == "douyin" for code in codes)
        if verified_browser_only:
            status = "succeeded" if states and all(item["status"] in {"success", "succeeded"} for item in states) else (
                "needs_manual_action" if any(item["status"] == "needs_manual_action" for item in states) else (
                    "needs_login" if any(item["status"] == "needs_login" for item in states) else "failed"
                )
            )
        else:
            status = "success" if states and failed == 0 else "failed"
        stamp = now()
        conn.execute(
            "UPDATE publish_tasks SET status=?,success_count=?,failed_count=?,finished_at=?,updated_at=? WHERE id=?",
            (status, success, failed, stamp, stamp, task_id),
        )
        task_error = conn.execute(
            "SELECT last_error,error_code FROM publish_tasks WHERE id=?", (task_id,)
        ).fetchone()
        sync_group_item_for_task(
            conn, task_id, status,
            task_error["last_error"] if task_error else "",
            task_error["error_code"] if task_error else "",
        )
        task = conn.execute(
            "SELECT video_path,delete_after_publish FROM publish_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if task and status in {"success", "succeeded"} and task["delete_after_publish"]:
        try:
            Path(task["video_path"]).unlink(missing_ok=True)
        except OSError:
            pass


def process_guarded_domestic_channel(channel: dict) -> None:
    """Run one fail-closed domestic browser publication.

    This path never treats an upload click, a URL change, or an HTTP 200 as
    success. The isolated publisher must return a verified platform ID and URL.
    """
    channel_id, task_id = int(channel["id"]), int(channel["publish_task_id"])
    artifact_dir = DATA_DIR / "logs" / "publish" / f"task-{task_id}-channel-{channel_id}-{int(time.time())}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = now()
    with connect() as conn:
        account = conn.execute(
            """SELECT a.*,p.id platform_id,p.code,p.name,p.capability_status,p.integration_type,
                      p.login_url,p.cookie_domains_json,p.login_cookie_names_json
               FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id WHERE a.id=?""",
            (channel["account_id"],),
        ).fetchone()
        task = conn.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
        conn.execute("UPDATE publish_channels SET status='prechecking',updated_at=? WHERE id=?", (stamp, channel_id))
        conn.execute("UPDATE publish_tasks SET status='prechecking',updated_at=? WHERE id=?", (stamp, task_id))
    try:
        account = dict(account or {})
        task = dict(task or {})
        selected_platform = normalize_platform(channel.get("selected_platform"))
        if not account:
            target_name = str(channel.get('account_name') or channel.get('account_name_snapshot') or '').strip()
            candidates = []
            with connect() as reb_conn:
                rows = reb_conn.execute("SELECT a.*,p.code FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id WHERE a.status='active'").fetchall()
                candidates = [dict(r) for r in rows if normalize_platform(r['code']) == normalize_platform(channel.get('platform_code')) and (not target_name or str(r['account_name']).strip() == target_name)]
                if len(candidates) == 1:
                    account = candidates[0]
                    reb_conn.execute("UPDATE publish_channels SET account_id=?,account_name=?,account_platform=?,updated_at=? WHERE id=?", (account['id'],account['account_name'],'wechat_channels',now(),channel_id))
                    channel['account_id']=account['id']; channel['account_name']=account['account_name']; channel['account_platform']='wechat_channels'
        account_platform = normalize_platform(account.get("code"))
        recorded_account_platform = normalize_platform(channel.get("account_platform"))
        channel_platform = normalize_platform(channel.get("platform_code"))
        if not account:
            raise guarded_error("ACCOUNT_NOT_FOUND", "发布账号不存在")
        if account_platform != channel_platform or selected_platform != channel_platform or recorded_account_platform != channel_platform:
            if selected_platform == "wechat_channels" or channel_platform == "wechat_channels":
                raise guarded_error("ACCOUNT_PLATFORM_MISMATCH")
            raise guarded_error("ACCOUNT_NOT_FOUND", "当前账号类型与发布平台不匹配")
        if int(account.get("platform_id") or 0) != int(channel.get("platform_id") or account.get("platform_id") or 0):
            raise guarded_error("ACCOUNT_PLATFORM_MISMATCH")
        metadata = {
            "title": task.get("title") or "", "description": task.get("description") or "",
            "tags": json.loads(task.get("tags_json") or "[]"), "cover_path": task.get("cover_path") or "",
            "schedule": task.get("scheduled_at") or "", "artifact_dir": str(artifact_dir),
        }
        def advance(state: str) -> None:
            if state not in {"uploading", "submitting", "processing"}:
                return
            with connect() as state_conn:
                state_conn.execute("UPDATE publish_channels SET status=?,updated_at=? WHERE id=?", (state, now(), channel_id))
                state_conn.execute("UPDATE publish_tasks SET status=?,updated_at=? WHERE id=?", (state, now(), task_id))
                sync_group_item_for_task(state_conn, task_id, state)

        metadata["state_callback"] = advance
        platform = dict(account, id=account['platform_id'], code=account_platform)
        result = preflight_publish_check(platform, account, task["video_path"], metadata, allow_unverified=False, check_duplicate=False)
        with connect() as conn:
            conn.execute("UPDATE publish_channels SET status='uploading',error_code='',updated_at=? WHERE id=?", (now(), channel_id))
            conn.execute("UPDATE publish_tasks SET status='uploading',updated_at=? WHERE id=?", (now(), task_id))
            sync_group_item_for_task(conn, task_id, "uploading")
        publisher_result = get_publisher(channel["platform_code"]).publish(task["video_path"], account, metadata) or {}
        if channel_platform == 'wechat_channels' and publisher_result.get('status') in ('processing', 'waiting_confirmation') and not publisher_result.get("error_code"):
            pending_status = publisher_result['status']
            with connect() as conn:
                conn.execute("UPDATE publish_channels SET status=?,result_evidence_json=?,error_code='',error_message='',finished_at='',updated_at=? WHERE id=?", (pending_status,json.dumps(publisher_result, ensure_ascii=False), now(), channel_id))
                conn.execute("UPDATE publish_tasks SET status=?,error_code='',last_error='',finished_at='',updated_at=? WHERE id=?", (pending_status,now(), task_id))
                sync_group_item_for_task(conn,task_id,pending_status)
            _finish_task(task_id)
            return
        redirect_published = channel_platform == 'wechat_channels' and publisher_result.get('status') == 'published' and publisher_result.get('verified') is True and publisher_result.get('backend_confirmed') is True
        if not redirect_published and (publisher_result.get("verified") is not True or publisher_result.get("backend_confirmed") is not True or not publisher_result.get("id") or not publisher_result.get("url")):
            raise PublishGuardError("SUBMIT_FAILED", "平台未返回可确认的真实作品ID和作品链接")
        with connect() as conn:
            conn.execute("UPDATE publish_channels SET status='processing',result_evidence_json=?,updated_at=? WHERE id=?", (json.dumps(publisher_result, ensure_ascii=False), now(), channel_id))
            conn.execute("UPDATE publish_tasks SET status='processing',updated_at=? WHERE id=?", (now(), task_id))
            conn.execute(
                """UPDATE publish_channels SET status='succeeded',remote_post_id=?,remote_url=?,error_message='',error_code='',
                   technical_log_id='',finished_at=?,updated_at=? WHERE id=?""",
                (str(publisher_result.get("id") or ""), str(publisher_result.get("url") or ""), now(), now(), channel_id),
            )
        _write_route_log(task_id=task_id, selected_platform=channel_platform, account_id=account.get("id") or 0, account_platform=account_platform, publisher_adapter=channel.get("publisher_adapter") or _publisher_adapter_name(channel_platform), publish_url=publisher_result.get("url"), status="succeeded")
    except Exception as exc:
        error = map_publish_exception(exc)
        final_status = status_for_error(error)
        original = getattr(exc, "__cause__", None) or exc
        original_type = type(original).__name__
        original_message = str(original)[:1000]
        original_traceback = "".join(traceback.format_exception(type(original), original, original.__traceback__))[-12000:]
        platform_error = f"{platform_label(channel['platform_code'])}发布失败：{error.user_message} 原始异常：{original_type}: {original_message}"
        with connect() as conn:
            if error.code == "AUTH_EXPIRED" and account.get("id"):
                conn.execute(
                    "UPDATE publish_accounts SET login_status='needs_login',last_checked_at=?,last_error=?,updated_at=? WHERE id=?",
                    (now(), platform_error, now(), int(account["id"])),
                )
            conn.execute(
                """UPDATE publish_channels SET status=?,error_code=?,technical_log_id=?,error_message=?,finished_at=?,updated_at=? WHERE id=?""",
                (final_status, error.code, error.log_id, platform_error, now(), now(), channel_id),
            )
            conn.execute(
                "UPDATE publish_tasks SET status=?,error_code=?,technical_log_id=?,last_error=?,updated_at=? WHERE id=?",
                (final_status, error.code, error.log_id, platform_error, now(), task_id),
            )
            conn.execute("UPDATE publish_channels SET result_evidence_json=? WHERE id=?", (json.dumps({"diagnosis": {"error_code": error.code, "original_exception_type": original_type, "original_exception_message": original_message, "traceback": original_traceback, "exception_cause": repr(getattr(exc, "__cause__", None))[:1000]}}, ensure_ascii=False), channel_id))
        _write_route_log(task_id=task_id, selected_platform=channel["platform_code"], account_id=channel.get("account_id"), account_platform=channel.get("account_platform") or channel["platform_code"], publisher_adapter=channel.get("publisher_adapter") or _publisher_adapter_name(channel["platform_code"]), status=final_status, error_stage="prechecking_or_publish", error_message=platform_error)
    _finish_task(task_id)


def process_douyin_channel(channel: dict) -> None:
    """Run Douyin with structured stages, diagnostics and safe retry recovery."""
    channel_id, task_id = int(channel["id"]), int(channel["publish_task_id"])
    artifact_dir = DATA_DIR / "logs" / "publish" / f"task-{task_id}-channel-{channel_id}-{int(time.time())}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = DATA_DIR / "logs" / "publish" / f"task-{task_id}-channel-{channel_id}-{int(time.time())}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = DATA_DIR / "logs" / "publish" / f"task-{task_id}-channel-{channel_id}-{int(time.time())}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        account_row = conn.execute(
            """SELECT a.*,p.id platform_id,p.code,p.name,p.capability_status,p.integration_type
               FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id WHERE a.id=?""",
            (channel["account_id"],),
        ).fetchone()
        task_row = conn.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
        evidence_row = conn.execute("SELECT result_evidence_json FROM publish_channels WHERE id=?", (channel_id,)).fetchone()
        conn.execute("UPDATE publish_channels SET status='prechecking',started_at=?,updated_at=? WHERE id=?", (now(), now(), channel_id))
        conn.execute("UPDATE publish_tasks SET status='prechecking',updated_at=? WHERE id=?", (now(), task_id))
    current_stage = "prechecking"
    artifact_dir = DATA_DIR / "logs" / "publish" / f"task-{task_id}-channel-{channel_id}-{int(time.time())}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        account, task = dict(account_row or {}), dict(task_row or {})
        metadata = {
            "title": task.get("title") or "", "description": task.get("description") or "",
            "tags": json.loads(task.get("tags_json") or "[]"), "cover_path": task.get("cover_path") or "",
            "schedule": task.get("scheduled_at") or "", "artifact_dir": str(artifact_dir),
        }
        try:
            previous = json.loads((evidence_row["result_evidence_json"] if evidence_row else "") or "{}")
        except (TypeError, ValueError):
            previous = {}
        attempts = list(previous.get("attempts") or [])
        if attempts:
            last_attempt = attempts[-1]
            if "未取得真实作品" in str(last_attempt.get("error_message") or ""):
                try:
                    metadata["recover_existing_after"] = int(time.mktime(time.strptime(last_attempt.get("started_at"), "%Y-%m-%d %H:%M:%S")))
                except (TypeError, ValueError, OverflowError):
                    pass

        def advance(state: str) -> None:
            nonlocal current_stage
            current_stage = state
            with connect() as state_conn:
                state_conn.execute("UPDATE publish_channels SET status=?,updated_at=? WHERE id=?", (state, now(), channel_id))
                state_conn.execute("UPDATE publish_tasks SET status=?,updated_at=? WHERE id=?", (state, now(), task_id))

        metadata["state_callback"] = advance
        checked = preflight_douyin_publish_check(account, account, task["video_path"], metadata)
        metadata["video_duration"] = float((checked.get("video") or {}).get("duration") or 0)
        result = get_publisher("douyin").publish(task["video_path"], account, metadata) or {}
        if result.get("verified") is not True or result.get("backend_confirmed") is not True or not result.get("id") or not result.get("url"):
            raise PublishGuardError("SUBMIT_FAILED", "抖音未返回可确认的真实作品ID和作品链接")
        combined_evidence = {**previous, **result, "preflight": checked, "artifact_dir": str(artifact_dir)}
        with connect() as conn:
            conn.execute(
                """UPDATE publish_channels SET status='succeeded',remote_post_id=?,remote_url=?,error_message='',error_code='',
                   technical_log_id='',result_evidence_json=?,finished_at=?,updated_at=? WHERE id=?""",
                (str(result["id"]), str(result["url"]), json.dumps(combined_evidence, ensure_ascii=False), now(), now(), channel_id),
            )
        _write_route_log(task_id=task_id, selected_platform="douyin", account_id=(dict(account_row or {}).get("id") or channel.get("account_id") or 0), account_platform="douyin", publisher_adapter=channel.get("publisher_adapter") or "BrowserPublisher", publish_url=result.get("url"), status="succeeded")
    except Exception as exc:
        error = map_publish_exception(exc)
        final_status = status_for_error(error)
        diagnosis = {
            "platform": "抖音", "account": (dict(account_row or {}).get("account_name") or ""),
            "video": Path(str(dict(task_row or {}).get("video_path") or "")).name,
            "stage": current_stage, "error_code": error.code,
            "message": f"抖音发布失败：{error.to_dict()['message']}", "technical_log_id": error.log_id,
            "artifact_dir": str(artifact_dir),
        }
        (artifact_dir / "diagnosis.json").write_text(json.dumps(diagnosis, ensure_ascii=False, indent=2), encoding="utf-8")
        with connect() as conn:
            conn.execute(
                """UPDATE publish_channels SET status=?,error_code=?,technical_log_id=?,error_message=?,
                   result_evidence_json=?,finished_at=?,updated_at=? WHERE id=?""",
                (final_status, error.code, error.log_id, f"抖音发布失败：{error.user_message}",
                 json.dumps({"diagnosis": diagnosis, "artifact_dir": str(artifact_dir)}, ensure_ascii=False), now(), now(), channel_id),
            )
            conn.execute(
                "UPDATE publish_tasks SET status=?,error_code=?,technical_log_id=?,last_error=?,updated_at=? WHERE id=?",
                (final_status, error.code, error.log_id, f"抖音发布失败：{error.user_message}", now(), task_id),
            )
        _write_route_log(task_id=task_id, selected_platform="douyin", account_id=(dict(account_row or {}).get("id") or 0), account_platform="douyin", publisher_adapter=channel.get("publisher_adapter") or "BrowserPublisher", status=final_status, error_stage=current_stage, error_message=f"抖音发布失败：{error.user_message}")
    _finish_task(task_id)


def process_queued_channel(channel: dict) -> None:
    if channel.get("platform_code") == "douyin":
        process_douyin_channel(channel)
        return
    if channel.get("platform_code") in GUARDED_DOMESTIC_PLATFORMS:
        process_guarded_domestic_channel(channel)
        return
    channel_id, task_id = int(channel["id"]), int(channel["publish_task_id"])
    selected = str(channel.get("selected_platform") or channel.get("platform_code") or "")
    account_platform = str(channel.get("account_platform") or channel.get("platform_code") or "")
    if selected != account_platform or selected != str(channel.get("platform_code") or ""):
        message = "当前账号类型与发布平台不匹配"
        with connect() as conn:
            conn.execute("UPDATE publish_channels SET status='failed',error_stage='routing',error_message=?,updated_at=? WHERE id=?", (message, now(), channel_id))
            conn.execute("UPDATE publish_tasks SET status='failed',last_error=?,updated_at=? WHERE id=?", (message, now(), task_id))
        _write_route_log(task_id=task_id, selected_platform=selected, account_id=channel.get("account_id"), account_platform=account_platform, publisher_adapter=channel.get("publisher_adapter"), status="failed", error_stage="routing", error_message=message)
        _finish_task(task_id)
        return
    stamp = now()
    with connect() as conn:
        conn.execute("UPDATE publish_channels SET status='publishing',started_at=?,updated_at=? WHERE id=?", (stamp, stamp, channel_id))
        conn.execute("UPDATE publish_tasks SET status='publishing',updated_at=? WHERE id=?", (stamp, task_id))
        sync_group_item_for_task(conn, task_id, "publishing")
        account = conn.execute(
            "SELECT a.*,p.code platform_code FROM publish_accounts a JOIN platforms p ON p.id=a.platform_id WHERE a.id=?",
            (channel["account_id"],),
        ).fetchone()
    try:
        metadata = {
            "title": channel.get("title") or "",
            "description": channel.get("description") or "",
            "tags": json.loads(channel.get("tags_json") or "[]"),
            "cover_path": channel.get("cover_path") or "",
            "schedule": channel.get("scheduled_at") or "",
        }
        result = get_publisher(channel["platform_code"]).publish(channel["video_path"], dict(account), metadata) or {}
        if result.get("verified") is not True:
            raise RuntimeError("平台未返回可验证的发布成功结果")
        with connect() as conn:
            conn.execute(
                "UPDATE publish_channels SET status='success',remote_post_id=?,remote_url=?,error_message='',finished_at=?,updated_at=? WHERE id=?",
                (str(result.get("id") or ""), str(result.get("url") or ""), now(), now(), channel_id),
            )
        _write_route_log(task_id=task_id, selected_platform=channel["platform_code"], account_id=channel.get("account_id"), account_platform=account_platform, publisher_adapter=channel.get("publisher_adapter") or _publisher_adapter_name(channel["platform_code"]), publish_url=result.get("url"), status="success")
    except Exception as exc:
        platform_error = f"{platform_label(channel['platform_code'])}发布失败：{str(exc)[:1000]}"
        with connect() as conn:
            conn.execute(
                "UPDATE publish_channels SET status='failed',error_message=?,finished_at=?,updated_at=? WHERE id=?",
                (platform_error, now(), now(), channel_id),
            )
            conn.execute("UPDATE publish_tasks SET last_error=?,updated_at=? WHERE id=?", (platform_error, now(), task_id))
        _write_route_log(task_id=task_id, selected_platform=channel["platform_code"], account_id=channel.get("account_id"), account_platform=account_platform, publisher_adapter=channel.get("publisher_adapter") or _publisher_adapter_name(channel["platform_code"]), status="failed", error_stage="publish", error_message=platform_error)
    _finish_task(task_id)


class PublishWorker:
    def __init__(self):
        self._wake = threading.Event()
        self._thread = None
        self._last_confirmation = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mixcut-publish-worker", daemon=True)
        self._thread.start()

    def wake(self):
        self.start()
        self._wake.set()

    def _run(self):
        while True:
            channel = next_queued_channel()
            if channel:
                process_queued_channel(channel)
                continue
            if time.monotonic() - self._last_confirmation >= 60:
                self._last_confirmation = time.monotonic()
                recheck_postflow_processing()
            self._wake.wait(5)
            self._wake.clear()


PUBLISH_WORKER = PublishWorker()


def recheck_postflow_processing():
    with connect() as conn:
        rows = conn.execute("""SELECT c.id,c.publish_task_id,c.result_evidence_json FROM publish_channels c
            JOIN platforms p ON p.id=c.platform_id WHERE p.code='wechat_channels' AND c.status='processing'
            ORDER BY c.updated_at LIMIT 1""").fetchall()
    for row in rows:
        try:
            evidence = json.loads(row['result_evidence_json'] or '{}')
            if not evidence.get('submitted') or not evidence.get('confirmation_job'):
                continue
            publisher = get_publisher('wechat_channels')
            if not hasattr(publisher, 'confirm'):
                continue
            result = publisher.confirm(evidence)
            published = result.get('verified') is True and bool(result.get('id') and result.get('url'))
            status = 'succeeded' if published else ('failed' if result.get('error_code') else 'processing')
            with connect() as conn:
                conn.execute("""UPDATE publish_channels SET status=?,remote_post_id=?,remote_url=?,result_evidence_json=?,
                    finished_at=?,updated_at=?,error_code=?,error_message=? WHERE id=? AND status='processing'""",
                    (status,result.get('id') or '',result.get('url') or '',json.dumps(result,ensure_ascii=False),
                     now() if status != 'processing' else '',now(),result.get('error_code') or '',result.get('error_message') or '',row['id']))
            if status != 'processing':
                _finish_task(row['publish_task_id'])
        except Exception:
            # Confirmation transport failure must not requeue a submitted video.
            continue


def start_publish_worker() -> None:
    recover_guarded_channels()
    with connect() as conn:
        conn.execute("UPDATE publish_channels SET status='queued',updated_at=? WHERE status='publishing'", (now(),))
        conn.execute("UPDATE publish_tasks SET status='queued',updated_at=? WHERE status='publishing'", (now(),))
        # v1.3.1 briefly treated a browser button click as publication success.
        # Browser channels without a platform post id are unverified and must
        # never remain in the successful-results view.
        false_successes = conn.execute(
            """SELECT c.id,c.publish_task_id FROM publish_channels c
               JOIN platforms p ON p.id=c.platform_id
               WHERE c.status='success' AND p.integration_type='browser'
               AND COALESCE(c.remote_post_id,'')=''"""
        ).fetchall()
        for item in false_successes:
            message = "历史记录未取得平台发布成功凭证，请重新发布"
            conn.execute(
                "UPDATE publish_channels SET status='failed',error_message=?,updated_at=? WHERE id=?",
                (message, now(), item["id"]),
            )
            conn.execute(
                "UPDATE publish_tasks SET status='failed',last_error=?,failed_count=1,success_count=0,updated_at=? WHERE id=?",
                (message, now(), item["publish_task_id"]),
            )
        conn.execute(
            """UPDATE publish_group_batch_items
               SET status=COALESCE((SELECT status FROM publish_tasks WHERE id=publish_group_batch_items.task_id),status),
                   blocked_reason=CASE WHEN task_id IS NOT NULL THEN COALESCE((SELECT last_error FROM publish_tasks WHERE id=publish_group_batch_items.task_id),'') ELSE blocked_reason END,
                   error_code=CASE WHEN task_id IS NOT NULL THEN COALESCE((SELECT error_code FROM publish_tasks WHERE id=publish_group_batch_items.task_id),'') ELSE error_code END,
                   updated_at=?
               WHERE task_id IS NOT NULL""",
            (now(),),
        )
        for batch in conn.execute("SELECT id FROM publish_group_batches").fetchall():
            recalculate_group_batch(conn, int(batch["id"]))
    PUBLISH_WORKER.start()
