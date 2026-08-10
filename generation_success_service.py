"""Atomic generation-success registration for Publish Center drafts.

This module is deliberately independent from the generation queue and the
publisher adapters.  Every successful render enters here exactly once after
FFprobe acceptance, regardless of whether it originated from a single task,
batch task, retry, or recovered queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db import connect, now
from task_runtime import append_task_log
from video_scan import probe_video
from runtime_paths import get_ffprobe_path


SUCCESS_STATUSES = {"succeeded", "completed"}


class GenerationRegistrationError(RuntimeError):
    """The rendered file could not be registered as a publishable draft."""


def _configured_ffprobe() -> str:
    return get_ffprobe_path()


def verify_generated_output(
    video_path: str | Path,
    *,
    verified_duration: float = 0.0,
    ffprobe_path: str = "",
) -> dict[str, Any]:
    """Verify that a real, readable video exists and return stable metadata."""
    path = Path(str(video_path or "")).expanduser()
    if not path.is_file():
        raise GenerationRegistrationError("成片文件不存在，不能登记到发布中心")
    size = int(path.stat().st_size)
    if size <= 0:
        raise GenerationRegistrationError("成片文件为空，不能登记到发布中心")

    duration = float(verified_duration or 0.0)
    if duration <= 0:
        info = probe_video(path, _configured_ffprobe())
        duration = float(info.get("duration") or 0.0)
        if info.get("error") or duration <= 0 or int(info.get("width") or 0) <= 0:
            raise GenerationRegistrationError(
                str(info.get("error") or "FFprobe无法读取有效视频轨道")
            )
    return {
        "path": str(path.resolve()),
        "filename": path.name,
        "duration": duration,
        "size": size,
    }


def on_generation_succeeded(
    task_id: int,
    video_path: str | Path,
    *,
    verified_duration: float = 0.0,
    ffprobe_path: str = "",
    schedule_metadata: bool = True,
) -> dict[str, Any]:
    """Finalize a generated video and create its idempotent publish draft.

    Task success and the draft registry row share one SQLite transaction.  AI
    metadata generation is intentionally scheduled only after commit and its
    failure never removes the draft.
    """
    task_id = int(task_id)
    append_task_log(task_id, "Generation completed")
    try:
        output = verify_generated_output(
            video_path,
            verified_duration=verified_duration,
            ffprobe_path=ffprobe_path,
        )
        append_task_log(task_id, "Output verified")
        stamp = now()
        append_task_log(task_id, "Register generated video")
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT id,template_id,task_name FROM generation_tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not task:
                raise GenerationRegistrationError(f"生成任务不存在：{task_id}")
            batch = conn.execute(
                "SELECT batch_id FROM batch_task_items WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            source_batch_id = int(batch["batch_id"]) if batch else None
            project_id = int(task["template_id"]) if task["template_id"] else None
            existing_draft = conn.execute(
                "SELECT id FROM publish_draft_metadata WHERE generation_task_id=?",
                (task_id,),
            ).fetchone()

            conn.execute(
                """UPDATE generation_tasks SET status='succeeded',progress=100,
                   current_progress=100,current_stage='succeeded',current_step='完成',
                   current_shot='完成',output_path=?,error_message='',error_code='',
                   failed_stage='',finished_at=?,updated_at=? WHERE id=?""",
                (output["path"], stamp, stamp, task_id),
            )
            append_title = str(task["task_name"] or "")
            conn.execute(
                """INSERT INTO publish_draft_metadata(
                       generation_task_id,title,ai_status,source_batch_id,project_id,
                       video_path,video_filename,video_duration,publish_status,created_at,updated_at
                   ) VALUES(?,?, 'not_started',?,?,?,?,?,'pending',?,?)
                   ON CONFLICT(generation_task_id) DO UPDATE SET
                       source_batch_id=excluded.source_batch_id,
                       project_id=excluded.project_id,
                       video_path=excluded.video_path,
                       video_filename=excluded.video_filename,
                       video_duration=excluded.video_duration,
                       publish_status=CASE
                           WHEN publish_draft_metadata.publish_status='' THEN 'pending'
                           ELSE publish_draft_metadata.publish_status END,
                       updated_at=excluded.updated_at""",
                (
                    task_id,
                    append_title,
                    source_batch_id,
                    project_id,
                    output["path"],
                    output["filename"],
                    output["duration"],
                    stamp,
                    stamp,
                ),
            )
            draft = conn.execute(
                "SELECT * FROM publish_draft_metadata WHERE generation_task_id=?",
                (task_id,),
            ).fetchone()
            conn.commit()
        draft_id = int(draft["id"])
        append_task_log(task_id, "Create publish item")
        append_task_log(task_id, f"Publish item created: ID {draft_id}")
    except Exception as exc:
        append_task_log(task_id, f"Publish registration failed | task_id={task_id} | {exc}")
        raise

    metadata_scheduled = False
    metadata_error = ""
    if schedule_metadata:
        append_task_log(task_id, "Schedule metadata generation")
        try:
            from publish_metadata_service import queue_auto_generation

            metadata_scheduled = bool(queue_auto_generation(task_id))
        except Exception as exc:  # Draft must survive any optional AI failure.
            metadata_error = str(exc)
            append_task_log(task_id, f"Metadata scheduling failed | task_id={task_id} | {exc}")
    return {
        "task_id": task_id,
        "publish_item_id": draft_id,
        "created": existing_draft is None,
        "video_path": output["path"],
        "video_duration": output["duration"],
        "metadata_scheduled": metadata_scheduled,
        "metadata_error": metadata_error,
    }


def repair_missing_publish_records(*, schedule_metadata: bool = True) -> dict[str, Any]:
    """Backfill only real successful outputs missing the idempotent draft row."""
    with connect() as conn:
        candidates = [
            dict(row)
            for row in conn.execute(
                """SELECT g.id,g.output_path,g.status FROM generation_tasks g
                   WHERE g.status IN ('succeeded','completed')
                     AND COALESCE(g.output_path,'')<>''
                     AND NOT EXISTS(
                         SELECT 1 FROM publish_draft_metadata d
                         WHERE d.generation_task_id=g.id
                     )
                   ORDER BY g.id"""
            ).fetchall()
        ]
    repaired: list[int] = []
    skipped: list[dict[str, Any]] = []
    for task in candidates:
        try:
            on_generation_succeeded(
                int(task["id"]),
                str(task["output_path"]),
                schedule_metadata=schedule_metadata,
            )
            repaired.append(int(task["id"]))
        except Exception as exc:
            skipped.append({"task_id": int(task["id"]), "reason": str(exc)})
    return {
        "candidate_count": len(candidates),
        "repaired_count": len(repaired),
        "repaired_task_ids": repaired,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }


def audit_generation_publish_records() -> dict[str, Any]:
    """Read-only audit used by acceptance tests and support diagnostics."""
    with connect() as conn:
        succeeded = [
            dict(row)
            for row in conn.execute(
                "SELECT id,task_name,status,output_path FROM generation_tasks WHERE status IN ('succeeded','completed') ORDER BY id"
            ).fetchall()
        ]
        draft_task_ids = {
            int(row["generation_task_id"])
            for row in conn.execute("SELECT generation_task_id FROM publish_draft_metadata").fetchall()
        }
        duplicate_rows = [
            dict(row)
            for row in conn.execute(
                """SELECT generation_task_id,COUNT(*) count FROM publish_draft_metadata
                   GROUP BY generation_task_id HAVING COUNT(*)>1"""
            ).fetchall()
        ]
        publish_tasks = int(conn.execute("SELECT COUNT(*) FROM publish_tasks").fetchone()[0])
    items = []
    for task in succeeded:
        path = Path(str(task.get("output_path") or ""))
        items.append(
            {
                **task,
                "output_exists": path.is_file(),
                "has_publish_item": int(task["id"]) in draft_task_ids,
            }
        )
    return {
        "succeeded_count": len(succeeded),
        "publish_draft_count": len(draft_task_ids),
        "publish_task_count": publish_tasks,
        "missing_task_ids": [item["id"] for item in items if item["output_exists"] and not item["has_publish_item"]],
        "missing_file_task_ids": [item["id"] for item in items if not item["output_exists"]],
        "duplicate_source_task_ids": duplicate_rows,
        "items": items,
    }
