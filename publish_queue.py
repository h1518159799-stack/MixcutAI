"""Database-backed publish task queue. No upload worker is started in phase one."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from db import connect, now
from cover_service import normalize_cover_path
from publishers import get_publisher


SUPPORTED_PLATFORM_CODES = {"douyin", "tiktok", "youtube", "instagram", "facebook", "xiaohongshu", "kuaishou"}
TASK_STATUSES = {"pending", "queued", "publishing", "success", "failed"}


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


def create_publish_task(data: dict) -> int:
    generation_task_id = int(data.get("generation_task_id") or 0)
    account_ids = sorted({int(value) for value in data.get("account_ids", []) if int(value) > 0})
    if not generation_task_id:
        raise ValueError("请选择已生成视频")
    if not account_ids:
        raise ValueError("请至少选择一个发布账号")
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("请输入发布标题")
    cover_path = normalize_cover_path(data.get("cover_path") or "")
    with connect() as conn:
        video = conn.execute(
            "SELECT id,task_name,output_path,status FROM generation_tasks WHERE id=?",
            (generation_task_id,),
        ).fetchone()
        if not video or video["status"] != "completed" or not video["output_path"]:
            raise ValueError("所选视频尚未生成完成")
        video_path = Path(video["output_path"])
        if not video_path.is_file():
            raise ValueError("成片文件不存在，请重新生成或检查输出目录")
        marks = ",".join("?" for _ in account_ids)
        accounts = conn.execute(
            f"""SELECT a.id,a.platform_id,p.code,p.name,p.capability_status FROM publish_accounts a
                JOIN platforms p ON p.id=a.platform_id
                WHERE a.status='active' AND p.enabled=1 AND a.id IN ({marks})""",
            tuple(account_ids),
        ).fetchall()
        if len(accounts) != len(account_ids):
            raise ValueError("部分发布账号不存在或已停用")
        unverified = [row["name"] for row in accounts if row["capability_status"] != "passed"]
        if unverified:
            raise ValueError("平台尚未完成测试，不能加入正式发布：" + "、".join(unverified))
        unsupported = [row["code"] for row in accounts if row["code"] not in SUPPORTED_PLATFORM_CODES]
        if unsupported:
            raise ValueError("暂不支持所选平台: " + ", ".join(unsupported))
        stamp = now()
        scheduled_at = str(data.get("scheduled_at") or "").strip().replace("T", " ")
        ai_hashtags = normalize_tags(data.get("ai_hashtags"))
        conn.execute(
            """INSERT INTO publish_tasks(generation_task_id,video_path,title,description,tags_json,
               cover_path,ai_title,ai_description,ai_hashtags,cover_text,ai_generated_time,
               scheduled_at,delete_after_publish,status,total_channels,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (generation_task_id, str(video_path), title, str(data.get("description") or "").strip(),
             json.dumps(normalize_tags(data.get("tags")), ensure_ascii=False),
             cover_path, str(data.get("ai_title") or "").strip(), str(data.get("ai_description") or "").strip(),
             json.dumps(ai_hashtags, ensure_ascii=False), str(data.get("cover_text") or "").strip(),
             str(data.get("ai_generated_time") or "").strip(), scheduled_at,
             1 if data.get("delete_after_publish") else 0, len(accounts), stamp, stamp),
        )
        task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for account in accounts:
            conn.execute(
                """INSERT INTO publish_channels(publish_task_id,account_id,platform_id,status,created_at,updated_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (task_id, account["id"], account["platform_id"], stamp, stamp),
            )
    return task_id


def enqueue_publish_task(task_id: int) -> dict:
    stamp = now()
    with connect() as conn:
        task = conn.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("发布任务不存在")
        if task["status"] not in ("pending", "failed"):
            raise ValueError("当前任务状态不能重新入队")
        unverified = conn.execute(
            """SELECT DISTINCT p.name FROM publish_channels c
               JOIN platforms p ON p.id=c.platform_id
               WHERE c.publish_task_id=? AND p.capability_status<>'passed'""", (task_id,)
        ).fetchall()
        if unverified:
            raise ValueError("平台尚未完成测试，不能加入正式发布：" + "、".join(row["name"] for row in unverified))
        conn.execute(
            "UPDATE publish_tasks SET status='queued',last_error='',updated_at=? WHERE id=?",
            (stamp, task_id),
        )
        conn.execute(
            "UPDATE publish_channels SET status='queued',error_message='',updated_at=? WHERE publish_task_id=? AND status IN ('pending','failed')",
            (stamp, task_id),
        )
    return {"id": task_id, "status": "queued"}


def delete_publish_task(task_id: int) -> None:
    with connect() as conn:
        task = conn.execute("SELECT status FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return
        if task["status"] == "publishing":
            raise ValueError("发布中的任务不能删除")
        conn.execute("DELETE FROM publish_tasks WHERE id=?", (task_id,))


def sync_missing_publish_videos() -> dict:
    """Synchronize publish records against the configured output directory.

    The output folder is the source of truth here. This deliberately does not
    scan or delete ``generation_tasks`` records.
    """
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
    }


def next_queued_channel() -> dict | None:
    """Return the next due channel for the serial publish worker."""
    with connect() as conn:
        row = conn.execute(
            """SELECT c.*,t.video_path,t.title,t.description,t.tags_json,t.cover_path,t.scheduled_at,
               t.delete_after_publish,a.account_name,a.browser_profile_path,p.code platform_code
               FROM publish_channels c JOIN publish_tasks t ON t.id=c.publish_task_id
               JOIN publish_accounts a ON a.id=c.account_id JOIN platforms p ON p.id=c.platform_id
               WHERE c.status='queued' AND t.status IN ('queued','publishing')
               AND p.capability_status='passed'
               AND (t.scheduled_at='' OR t.scheduled_at<=?) ORDER BY t.id,c.id LIMIT 1""",
            (now(),),
        ).fetchone()
        return dict(row) if row else None


def _finish_task(task_id: int) -> None:
    with connect() as conn:
        states = conn.execute(
            "SELECT status FROM publish_channels WHERE publish_task_id=?", (task_id,)
        ).fetchall()
        success = sum(1 for item in states if item["status"] == "success")
        failed = sum(1 for item in states if item["status"] == "failed")
        pending = sum(1 for item in states if item["status"] in ("pending", "queued", "publishing"))
        if pending:
            return
        status = "success" if states and failed == 0 else "failed"
        stamp = now()
        conn.execute(
            "UPDATE publish_tasks SET status=?,success_count=?,failed_count=?,finished_at=?,updated_at=? WHERE id=?",
            (status, success, failed, stamp, stamp, task_id),
        )
        task = conn.execute(
            "SELECT video_path,delete_after_publish FROM publish_tasks WHERE id=?", (task_id,)
        ).fetchone()
    if task and status == "success" and task["delete_after_publish"]:
        try:
            Path(task["video_path"]).unlink(missing_ok=True)
        except OSError:
            pass


def process_queued_channel(channel: dict) -> None:
    channel_id, task_id = int(channel["id"]), int(channel["publish_task_id"])
    stamp = now()
    with connect() as conn:
        conn.execute("UPDATE publish_channels SET status='publishing',started_at=?,updated_at=? WHERE id=?", (stamp, stamp, channel_id))
        conn.execute("UPDATE publish_tasks SET status='publishing',updated_at=? WHERE id=?", (stamp, task_id))
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
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE publish_channels SET status='failed',error_message=?,finished_at=?,updated_at=? WHERE id=?",
                (str(exc)[:1000], now(), now(), channel_id),
            )
            conn.execute("UPDATE publish_tasks SET last_error=?,updated_at=? WHERE id=?", (str(exc)[:1000], now(), task_id))
    _finish_task(task_id)


class PublishWorker:
    def __init__(self):
        self._wake = threading.Event()
        self._thread = None

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
            self._wake.wait(5)
            self._wake.clear()


PUBLISH_WORKER = PublishWorker()


def start_publish_worker() -> None:
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
    PUBLISH_WORKER.start()
