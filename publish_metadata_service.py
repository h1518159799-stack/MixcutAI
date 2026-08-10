"""Persistent AI metadata for completed videos waiting in Publish Center."""

from __future__ import annotations

import threading
from typing import Iterable

from ai_publish_service import PublishAIError, generate_publish_content
from db import connect, now


VALID_STATES = {"not_started", "generating", "completed", "failed", "manually_edited"}
EDITABLE_FIELDS = {
    "title": "title_state",
    "hashtags": "hashtags_state",
    "cover_text": "cover_text_state",
}
_running: set[int] = set()
_running_lock = threading.Lock()


def _metadata_row(task_id: int) -> dict | None:
    with connect() as conn:
        value = conn.execute(
            "SELECT * FROM publish_draft_metadata WHERE generation_task_id=?",
            (int(task_id),),
        ).fetchone()
        return dict(value) if value else None


def public_metadata(task_id: int) -> dict:
    value = _metadata_row(task_id)
    if value:
        return value
    return {
        "generation_task_id": int(task_id),
        "title": "",
        "hashtags": "",
        "cover_text": "",
        "description": "",
        "ai_status": "not_started",
        "title_state": "not_started",
        "hashtags_state": "not_started",
        "cover_text_state": "not_started",
        "auto_generation_attempted": 0,
        "error_code": "",
        "error_message": "",
        "technical_log_id": "",
        "ai_generated_at": "",
        "legacy_record": True,
    }


def _feature_cutoff() -> str:
    with connect() as conn:
        value = conn.execute(
            "SELECT value FROM app_settings WHERE key='publish_metadata_autogen_cutoff'"
        ).fetchone()
    return str(value["value"] if value else "")


def ensure_new_draft(task: dict) -> dict | None:
    """Create metadata only for videos produced after the feature was installed."""
    task_id = int(task.get("id") or 0)
    if not task_id or task.get("status") not in {"succeeded", "completed"} or not task.get("output_exists"):
        return None
    current = _metadata_row(task_id)
    if current:
        return current
    cutoff = _feature_cutoff()
    created_at = str(task.get("created_at") or "")
    if not cutoff or not created_at or created_at < cutoff:
        return None
    stamp = now()
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO publish_draft_metadata(
               generation_task_id,title,ai_status,created_at,updated_at
               ) VALUES(?,?, 'not_started',?,?)""",
            (task_id, str(task.get("task_name") or ""), stamp, stamp),
        )
    return _metadata_row(task_id)


def attach_and_schedule(tasks: Iterable[dict], scheduled_task_ids: set[int] | None = None) -> None:
    """Attach public metadata and queue one background generation for new drafts."""
    scheduled_task_ids = scheduled_task_ids or set()
    for task in tasks:
        task_id = int(task.get("id") or 0)
        if task_id not in scheduled_task_ids:
            ensured = ensure_new_draft(task)
            if ensured and ensured.get("ai_status") == "not_started" and not int(ensured.get("auto_generation_attempted") or 0):
                queue_auto_generation(task_id)
        task["publish_metadata"] = public_metadata(task_id)


def queue_auto_generation(task_id: int) -> bool:
    task_id = int(task_id)
    with _running_lock:
        if task_id in _running:
            return False
        metadata = _metadata_row(task_id)
        if not metadata or metadata.get("ai_status") != "not_started" or int(metadata.get("auto_generation_attempted") or 0):
            return False
        _running.add(task_id)
    worker = threading.Thread(
        target=_auto_generate_worker,
        args=(task_id,),
        name=f"publish-metadata-{task_id}",
        daemon=True,
    )
    worker.start()
    return True


def _auto_generate_worker(task_id: int) -> None:
    stamp = now()
    try:
        with connect() as conn:
            changed = conn.execute(
                """UPDATE publish_draft_metadata
                   SET ai_status='generating',auto_generation_attempted=1,error_code='',
                       error_message='',technical_log_id='',updated_at=?
                   WHERE generation_task_id=? AND ai_status='not_started'
                     AND auto_generation_attempted=0""",
                (stamp, task_id),
            ).rowcount
        if not changed:
            return
        current = _metadata_row(task_id) or {}
        result = generate_publish_content(
            task_id,
            "douyin",
            None,
            str(current.get("title") or ""),
            str(current.get("hashtags") or ""),
            str(current.get("cover_text") or ""),
        )
        hashtags = " ".join(f"#{tag}" for tag in result.get("hashtags") or [])
        with connect() as conn:
            row = conn.execute(
                "SELECT title_state,hashtags_state,cover_text_state FROM publish_draft_metadata WHERE generation_task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return
            has_manual = any(str(row[key]) == "manually_edited" for key in row.keys())
            conn.execute(
                """UPDATE publish_draft_metadata SET
                   title=CASE WHEN title_state='manually_edited' THEN title ELSE ? END,
                   hashtags=CASE WHEN hashtags_state='manually_edited' THEN hashtags ELSE ? END,
                   cover_text=CASE WHEN cover_text_state='manually_edited' THEN cover_text ELSE ? END,
                   description=?,
                   title_state=CASE WHEN title_state='manually_edited' THEN title_state ELSE 'completed' END,
                   hashtags_state=CASE WHEN hashtags_state='manually_edited' THEN hashtags_state ELSE 'completed' END,
                   cover_text_state=CASE WHEN cover_text_state='manually_edited' THEN cover_text_state ELSE 'completed' END,
                   ai_status=?,ai_generated_at=?,error_code='',error_message='',technical_log_id='',updated_at=?
                   WHERE generation_task_id=?""",
                (
                    str(result.get("title") or ""), hashtags, str(result.get("cover_text") or ""),
                    str(result.get("description") or ""),
                    "manually_edited" if has_manual else "completed",
                    str(result.get("ai_generated_time") or now()), now(), task_id,
                ),
            )
    except PublishAIError as exc:
        with connect() as conn:
            conn.execute(
                """UPDATE publish_draft_metadata SET ai_status='failed',error_code=?,
                   error_message=?,technical_log_id=?,updated_at=? WHERE generation_task_id=?""",
                (exc.code, exc.user_message, exc.log_id, now(), task_id),
            )
    except Exception:
        # Unexpected detail is intentionally not persisted to the user-facing DB.
        with connect() as conn:
            conn.execute(
                """UPDATE publish_draft_metadata SET ai_status='failed',error_code='AI_REQUEST_FAILED',
                   error_message='发布信息自动生成失败，你仍然可以手动填写或重新生成。',updated_at=?
                   WHERE generation_task_id=?""",
                (now(), task_id),
            )
    finally:
        with _running_lock:
            _running.discard(task_id)


def save_draft_field(task_id: int, field: str, value: str, state: str = "manually_edited") -> dict:
    if field not in EDITABLE_FIELDS:
        raise ValueError("不支持的发布信息字段")
    if state not in {"completed", "manually_edited"}:
        raise ValueError("发布信息状态无效")
    task = None
    with connect() as conn:
        raw = conn.execute("SELECT id,task_name FROM generation_tasks WHERE id=?", (int(task_id),)).fetchone()
        task = dict(raw) if raw else None
        if not task:
            raise ValueError("成片任务不存在")
        stamp = now()
        conn.execute(
            """INSERT OR IGNORE INTO publish_draft_metadata(
               generation_task_id,title,ai_status,auto_generation_attempted,created_at,updated_at
               ) VALUES(?,?, 'not_started',1,?,?)""",
            (int(task_id), str(task.get("task_name") or ""), stamp, stamp),
        )
        state_column = EDITABLE_FIELDS[field]
        conn.execute(
            f"UPDATE publish_draft_metadata SET {field}=?,{state_column}=?,ai_status=?,updated_at=? WHERE generation_task_id=?",
            (str(value or ""), state, "manually_edited" if state == "manually_edited" else "completed", stamp, int(task_id)),
        )
    return public_metadata(task_id)
