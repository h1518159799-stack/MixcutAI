"""Serial batch orchestration over the existing single-video generation flow."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable

from db import connect, now


STRATEGIES = {"random", "prefer_unused", "avoid_repeat"}
TERMINAL_TASK_STATUSES = {"completed", "failed"}
TERMINAL_BATCH_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}


def clone_template_for_task(conn, source_template_id: int, task_name: str, timestamp: str) -> int:
    source_shots = conn.execute(
        "SELECT id,sort_order FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
        (source_template_id,),
    ).fetchall()
    conn.execute(
        """INSERT INTO edit_templates(
               name,description,output_width,output_height,is_hidden,source_template_id,created_at,updated_at
           ) SELECT ?,description,output_width,output_height,1,id,?,?
             FROM edit_templates WHERE id=?""",
        (task_name, timestamp, timestamp, source_template_id),
    )
    task_template_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """INSERT INTO template_shots(
               template_id,shot_name,original_text,ai_text,material_category_id,duration,
               estimated_duration,mute_original_audio,enable_subtitle,enable_tts,
               voice_file_path,voice_duration,voice_text_hash,subtitle_file_path,
               subtitle_render_hash,sort_order,tts_voice_id,created_at,updated_at
           ) SELECT ?,shot_name,original_text,'',material_category_id,duration,
               0,mute_original_audio,enable_subtitle,enable_tts,
               '',0,'','','',sort_order,tts_voice_id,?,?
             FROM template_shots WHERE template_id=? ORDER BY sort_order,id""",
        (task_template_id, timestamp, timestamp, source_template_id),
    )
    cloned_shots = conn.execute(
        "SELECT id,sort_order FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
        (task_template_id,),
    ).fetchall()
    shot_id_map = {
        int(source["id"]): int(cloned["id"])
        for source, cloned in zip(source_shots, cloned_shots)
    }
    transition_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_transitions'"
    ).fetchone()
    if transition_table and shot_id_map:
        marks = ",".join("?" for _ in shot_id_map)
        transitions = conn.execute(
            f"""SELECT * FROM shot_transitions
                WHERE from_shot_id IN ({marks}) AND to_shot_id IN ({marks})""",
            tuple(shot_id_map) + tuple(shot_id_map),
        ).fetchall()
        for transition in transitions:
            conn.execute(
                """INSERT INTO shot_transitions
                   (from_shot_id,to_shot_id,effect,duration,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    shot_id_map[int(transition["from_shot_id"])],
                    shot_id_map[int(transition["to_shot_id"])],
                    transition["effect"],
                    transition["duration"],
                    transition["enabled"],
                    timestamp,
                    timestamp,
                ),
            )
    return task_template_id


def create_batch_task(data: dict) -> int:
    template_id = int(data.get("template_id") or 0)
    total_count = int(data.get("total_count") or 0)
    if total_count < 1 or total_count > 100:
        raise ValueError("批量生成数量必须在 1 到 100 之间")
    strategy = str(data.get("material_strategy") or "random")
    if strategy not in STRATEGIES:
        raise ValueError("素材策略不受支持")
    category_ids = sorted({int(value) for value in (data.get("category_ids") or []) if int(value) > 0})

    with connect() as conn:
        template = conn.execute("SELECT id,name FROM edit_templates WHERE id=?", (template_id,)).fetchone()
        shots = conn.execute(
            "SELECT id,material_category_id FROM template_shots WHERE template_id=? ORDER BY sort_order,id",
            (template_id,),
        ).fetchall()
        if not template or not shots:
            raise ValueError("模板不存在或没有镜头")
        required_categories = {int(shot["material_category_id"]) for shot in shots if shot["material_category_id"]}
        if not required_categories:
            raise ValueError("模板镜头尚未配置素材分类")
        if not category_ids:
            category_ids = sorted(required_categories)
        missing = required_categories - set(category_ids)
        if missing:
            marks = ",".join("?" for _ in missing)
            names = [
                row["name"]
                for row in conn.execute(
                    f"SELECT name FROM material_categories WHERE id IN ({marks})", tuple(missing)
                ).fetchall()
            ]
            raise ValueError("素材池缺少模板所需分类：" + "、".join(names))
        marks = ",".join("?" for _ in category_ids)
        pool_count = conn.execute(
            f"SELECT COUNT(*) FROM materials WHERE status='active' AND category_id IN ({marks})",
            tuple(category_ids),
        ).fetchone()[0]
        if pool_count <= 0:
            raise ValueError("所选素材池没有可用视频")
        if strategy == "avoid_repeat":
            for category_id in required_categories:
                shots_per_video = sum(1 for shot in shots if int(shot["material_category_id"] or 0) == category_id)
                available = conn.execute(
                    "SELECT COUNT(*) FROM materials WHERE status='active' AND category_id=?",
                    (category_id,),
                ).fetchone()[0]
                needed = shots_per_video * total_count
                if available < needed:
                    category = conn.execute(
                        "SELECT name FROM material_categories WHERE id=?", (category_id,)
                    ).fetchone()
                    raise ValueError(
                        f"避免重复需要至少 {needed} 个{(category or {'name': ''})['name']}素材，当前只有 {available} 个"
                    )

        name = str(data.get("name") or f"{template['name']} 批量生成 {total_count} 条").strip()
        timestamp = now()
        conn.execute(
            """INSERT INTO batch_tasks(name,template_id,total_count,status,material_strategy,category_ids_json,
               created_at,updated_at) VALUES(?,?,?,'pending',?,?,?,?)""",
            (name, template_id, total_count, strategy, json.dumps(category_ids), timestamp, timestamp),
        )
        batch_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        shot_count = len(shots)
        for index in range(1, total_count + 1):
            task_name = f"{name} · {index:03d}/{total_count:03d}"
            task_template_id = clone_template_for_task(conn, template_id, task_name, timestamp)
            conn.execute(
                """INSERT INTO generation_tasks(template_id,task_name,status,progress,total_shots,created_at,updated_at)
                   VALUES(?,?,'pending',0,?,?,?)""",
                (task_template_id, task_name, shot_count, timestamp, timestamp),
            )
            task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """INSERT INTO batch_task_items(batch_id,task_id,index_no,status,created_at,updated_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (batch_id, task_id, index, timestamp, timestamp),
            )
    return batch_id


def sync_batch(batch_id: int, force_finish: bool = False) -> dict:
    with connect() as conn:
        counts = conn.execute(
            """SELECT COUNT(*) total,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
               SUM(CASE WHEN status IN ('pending','running') THEN 1 ELSE 0 END) remaining
               FROM batch_task_items WHERE batch_id=?""",
            (batch_id,),
        ).fetchone()
        completed = int(counts["completed"] or 0)
        failed = int(counts["failed"] or 0)
        remaining = int(counts["remaining"] or 0)
        status = "running"
        finished_at = ""
        if force_finish or remaining == 0:
            status = "completed_with_errors" if failed else "completed"
            finished_at = now()
        conn.execute(
            """UPDATE batch_tasks SET completed_count=?,failed_count=?,status=?,updated_at=?,
               finished_at=CASE WHEN ?<>'' THEN ? ELSE finished_at END WHERE id=?""",
            (completed, failed, status, now(), finished_at, finished_at, batch_id),
        )
    return {"completed": completed, "failed": failed, "remaining": remaining, "status": status}


def process_batch(batch_id: int, start_generation: Callable[[int], object], poll_interval: float = 0.5) -> None:
    with connect() as conn:
        batch = conn.execute("SELECT * FROM batch_tasks WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] in TERMINAL_BATCH_STATUSES:
            return
        conn.execute("UPDATE batch_tasks SET status='running',updated_at=? WHERE id=?", (now(), batch_id))
        items = conn.execute(
            "SELECT * FROM batch_task_items WHERE batch_id=? ORDER BY index_no,id", (batch_id,)
        ).fetchall()

    for item in items:
        already_completed = False
        with connect() as conn:
            current_batch = conn.execute("SELECT status FROM batch_tasks WHERE id=?", (batch_id,)).fetchone()
            if not current_batch or current_batch["status"] == "cancelled":
                return
            task = conn.execute("SELECT * FROM generation_tasks WHERE id=?", (item["task_id"],)).fetchone()
            if not task:
                conn.execute(
                    "UPDATE batch_task_items SET status='failed',error_message='生成任务已被删除',updated_at=? WHERE id=?",
                    (now(), item["id"]),
                )
                already_completed = True
            elif task["status"] == "completed":
                conn.execute(
                    "UPDATE batch_task_items SET status='completed',output_path=?,error_message='',updated_at=? WHERE id=?",
                    (task["output_path"], now(), item["id"]),
                )
                already_completed = True
            else:
                conn.execute(
                    "UPDATE batch_task_items SET status='running',error_message='',updated_at=? WHERE id=?",
                    (now(), item["id"]),
                )
        if already_completed:
            sync_batch(batch_id)
            continue

        retry_count = 0
        try:
            start_generation(int(item["task_id"]))
        except Exception as exc:
            with connect() as conn:
                conn.execute(
                    "UPDATE generation_tasks SET status='failed',error_message=?,updated_at=? WHERE id=?",
                    (str(exc), now(), item["task_id"]),
                )

        while True:
            with connect() as conn:
                task = conn.execute("SELECT * FROM generation_tasks WHERE id=?", (item["task_id"],)).fetchone()
                batch_status = conn.execute("SELECT status FROM batch_tasks WHERE id=?", (batch_id,)).fetchone()
            if not batch_status or batch_status["status"] == "cancelled":
                return
            if not task:
                with connect() as conn:
                    conn.execute(
                        "UPDATE batch_task_items SET status='failed',error_message='生成任务已被删除',updated_at=? WHERE id=?",
                        (now(), item["id"]),
                    )
                sync_batch(batch_id)
                break
            if task and task["status"] in TERMINAL_TASK_STATUSES:
                if task["status"] == "failed" and retry_count < 1:
                    retry_count += 1
                    with connect() as conn:
                        conn.execute(
                            """UPDATE generation_tasks SET status='pending',progress=0,current_shot=?,
                               error_message='',updated_at=? WHERE id=?""",
                            ("首次失败，正在重新排队", now(), item["task_id"]),
                        )
                        conn.execute(
                            "UPDATE batch_task_items SET status='running',error_message='',updated_at=? WHERE id=?",
                            (now(), item["id"]),
                        )
                    time.sleep(1)
                    try:
                        start_generation(int(item["task_id"]))
                    except Exception as exc:
                        with connect() as conn:
                            conn.execute(
                                "UPDATE generation_tasks SET status='failed',error_message=?,updated_at=? WHERE id=?",
                                (str(exc), now(), item["task_id"]),
                            )
                    continue
                item_status = "completed" if task["status"] == "completed" else "failed"
                with connect() as conn:
                    conn.execute(
                        """UPDATE batch_task_items SET status=?,output_path=?,error_message=?,updated_at=? WHERE id=?""",
                        (item_status, task["output_path"] or "", task["error_message"] or "", now(), item["id"]),
                    )
                sync_batch(batch_id)
                break
            time.sleep(max(0.05, poll_interval))
    sync_batch(batch_id, force_finish=True)


class SerialBatchQueue:
    """A single-worker queue; only one batch item is generated at a time."""

    def __init__(self):
        self._queue: queue.Queue[tuple[int, Callable[[int], object]]] = queue.Queue()
        self._lock = threading.Lock()
        self._enqueued: set[int] = set()
        self._thread: threading.Thread | None = None

    def enqueue(self, batch_id: int, callback: Callable[[int], object]) -> None:
        with self._lock:
            if batch_id in self._enqueued:
                return
            self._enqueued.add(batch_id)
            self._queue.put((batch_id, callback))
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="mixcut-batch-serial", daemon=True)
                self._thread.start()

    def _worker(self) -> None:
        while True:
            try:
                batch_id, callback = self._queue.get(timeout=1)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._thread = None
                        return
                continue
            try:
                process_batch(batch_id, callback)
            finally:
                with self._lock:
                    self._enqueued.discard(batch_id)
                self._queue.task_done()


BATCH_QUEUE = SerialBatchQueue()


def recover_and_enqueue(callback: Callable[[int], object]) -> list[int]:
    """Resume interrupted batches after application restart."""
    with connect() as conn:
        legacy_items = conn.execute(
            """SELECT i.task_id,g.task_name,b.template_id source_template_id
               FROM batch_task_items i
               JOIN batch_tasks b ON b.id=i.batch_id
               JOIN generation_tasks g ON g.id=i.task_id
               LEFT JOIN edit_templates t ON t.id=g.template_id
               WHERE b.status IN ('pending','running')
                 AND i.status IN ('pending','running')
                 AND COALESCE(t.is_hidden,0)=0"""
        ).fetchall()
        for item in legacy_items:
            cloned_template_id = clone_template_for_task(
                conn, item["source_template_id"], item["task_name"], now()
            )
            conn.execute(
                "UPDATE generation_tasks SET template_id=?,updated_at=? WHERE id=?",
                (cloned_template_id, now(), item["task_id"]),
            )
        batches = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM batch_tasks WHERE status IN ('pending','running') ORDER BY id"
            ).fetchall()
        ]
        if batches:
            marks = ",".join("?" for _ in batches)
            conn.execute(
                f"""UPDATE generation_tasks SET status='pending',progress=0,current_shot='',error_message='',updated_at=?
                    WHERE id IN (SELECT task_id FROM batch_task_items WHERE batch_id IN ({marks}) AND status='running')
                    AND status NOT IN ('completed','failed')""",
                (now(), *batches),
            )
            conn.execute(
                f"UPDATE batch_task_items SET status='pending',updated_at=? WHERE batch_id IN ({marks}) AND status='running'",
                (now(), *batches),
            )
            conn.execute(
                f"UPDATE batch_tasks SET status='pending',updated_at=? WHERE id IN ({marks})",
                (now(), *batches),
            )
    for batch_id in batches:
        BATCH_QUEUE.enqueue(batch_id, callback)
    return batches
