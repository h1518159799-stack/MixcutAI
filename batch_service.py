"""Serial batch orchestration over the existing single-video generation flow."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable

from db import connect, now, generation_queue_seq_supported, reserve_generation_queue_seqs
from task_runtime import (
    ACTIVE_TASK_STATUSES, HEARTBEAT_STALLED_SECONDS, TERMINAL_TASK_STATUSES,
    canonical_task_status, heartbeat_age_seconds, initialize_task_log,
    task_health_snapshot, append_task_log,
)


STRATEGIES = {"random", "prefer_unused", "avoid_repeat"}
TERMINAL_BATCH_STATUSES = {"succeeded", "partially_succeeded", "failed", "cancelled"}
ACTIVE_BATCH_STATUSES = {"queued", "running", "partially_blocked"}


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


def _batch_result(conn, batch_id: int, requested_count: int, *, duplicate: bool) -> dict:
    task_ids = [
        int(item["task_id"])
        for item in conn.execute(
            """SELECT i.task_id FROM batch_task_items i
               JOIN generation_tasks g ON g.id=i.task_id
               WHERE i.batch_id=? ORDER BY i.index_no,i.id""",
            (int(batch_id),),
        ).fetchall()
    ]
    if len(task_ids) != int(requested_count):
        raise RuntimeError(
            f"批量任务创建不完整：计划创建 {requested_count} 条，数据库实际写入 {len(task_ids)} 条"
        )
    return {
        "batch_id": int(batch_id),
        "task_ids": task_ids,
        "created_count": len(task_ids),
        "status": "queued",
        "duplicate": bool(duplicate),
    }


def create_batch_task(data: dict, *, detailed: bool = False) -> int | dict:
    created_tasks: list[tuple[int, str]] = []
    template_id = int(data.get("template_id") or 0)
    total_count = int(data.get("total_count") or 0)
    if total_count < 1 or total_count > 100:
        raise ValueError("批量生成数量必须在 1 到 100 之间")
    strategy = str(data.get("material_strategy") or "random")
    if strategy not in STRATEGIES:
        raise ValueError("素材策略不受支持")
    category_ids = sorted({int(value) for value in (data.get("category_ids") or []) if int(value) > 0})

    result: dict | None = None
    with connect() as conn:
        # Parent batch, child generation tasks and item relations are one atomic
        # write.  The UI must never observe a committed parent without children.
        conn.execute("BEGIN IMMEDIATE")
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
        category_json = json.dumps(category_ids)
        duplicate_rows = conn.execute(
            """SELECT id FROM batch_tasks
               WHERE template_id=? AND total_count=? AND material_strategy=?
                  AND category_ids_json=?
                  AND status IN ('queued','running','partially_blocked','needs_action')
               ORDER BY id DESC""",
            (template_id, total_count, strategy, category_json),
        ).fetchall()
        for duplicate in duplicate_rows:
            duplicate_id = int(duplicate["id"])
            item_count = int(conn.execute(
                """SELECT COUNT(*) FROM batch_task_items i
                   JOIN generation_tasks g ON g.id=i.task_id WHERE i.batch_id=?""",
                (duplicate_id,),
            ).fetchone()[0])
            if item_count == total_count:
                result = _batch_result(conn, duplicate_id, total_count, duplicate=True)
                conn.commit()
                return result if detailed else duplicate_id
            # Preserve malformed historical data for audit, but never reuse it
            # as a successful creation result.
            conn.execute(
                "UPDATE batch_tasks SET status='needs_action',updated_at=? WHERE id=?",
                (now(), duplicate_id),
            )
        timestamp = now()
        conn.execute(
            """INSERT INTO batch_tasks(name,template_id,total_count,status,material_strategy,category_ids_json,
               continue_on_child_error,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?,?,?)""",
            (name, template_id, total_count, strategy, category_json,
             int(bool(data.get('continue_on_child_error', False))), timestamp, timestamp),
        )
        batch_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        shot_count = len(shots)
        queue_seqs = reserve_generation_queue_seqs(conn, total_count)
        queue_seq_enabled = generation_queue_seq_supported(conn)
        for index in range(1, total_count + 1):
            task_name = f"{name} · {index:03d}/{total_count:03d}"
            task_template_id = clone_template_for_task(conn, template_id, task_name, timestamp)
            if queue_seq_enabled:
                conn.execute(
                    """INSERT INTO generation_tasks(template_id,task_name,status,queue_seq,progress,total_shots,created_at,updated_at)
                       VALUES(?,?,'queued',?,0,?,?,?)""",
                    (task_template_id, task_name, queue_seqs[index - 1], shot_count, timestamp, timestamp),
                )
            else:  # compatibility for isolated legacy fixtures before migration
                conn.execute(
                    """INSERT INTO generation_tasks(template_id,task_name,status,progress,total_shots,created_at,updated_at)
                       VALUES(?,?,'queued',0,?,?,?)""",
                    (task_template_id, task_name, shot_count, timestamp, timestamp),
                )
            task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            created_tasks.append((task_id, task_name))
            conn.execute(
                """INSERT INTO batch_task_items(batch_id,task_id,index_no,status,created_at,updated_at)
                   VALUES(?,?,?,'queued',?,?)""",
                (batch_id, task_id, index, timestamp, timestamp),
            )
        result = _batch_result(conn, batch_id, total_count, duplicate=False)
        conn.commit()
    for task_id, task_name in created_tasks:
        try:
            initialize_task_log(task_id, task_name)
        except OSError:
            # Log-file initialization must not turn a committed database task
            # into an apparent task-creation failure.
            pass
    if not result:
        raise RuntimeError("批量任务创建失败：数据库没有返回有效任务")
    return result if detailed else int(result["batch_id"])


def _canonical_item_status(value: object) -> str:
    return canonical_task_status(value)


def sync_batch(batch_id: int) -> dict:
    """Persist one authoritative batch summary derived only from child rows."""
    with connect() as conn:
        batch = conn.execute(
            "SELECT total_count,continue_on_child_error,status FROM batch_tasks WHERE id=?", (int(batch_id),)
        ).fetchone()
        if not batch:
            return {}
        rows_ = conn.execute(
            "SELECT i.status,COALESCE(g.progress,0) progress FROM batch_task_items i "
            "LEFT JOIN generation_tasks g ON g.id=i.task_id WHERE i.batch_id=? ORDER BY i.index_no",
            (int(batch_id),),
        ).fetchall()
        statuses = [_canonical_item_status(item["status"]) for item in rows_]
        total = len(statuses) or int(batch["total_count"] or 0)
        succeeded = statuses.count("succeeded")
        failed = statuses.count("failed")
        stalled = statuses.count("stalled")
        paused = statuses.count("paused")
        cancelled = statuses.count("cancelled")
        queued = statuses.count("queued")
        preparing = statuses.count("preparing")
        rendering = statuses.count("rendering")
        verifying = statuses.count("verifying")
        running = preparing + rendering + verifying
        continue_on_error = bool(batch["continue_on_child_error"])

        if total and succeeded == total:
            status = "succeeded"
        elif running and (stalled or paused or failed):
            status = "partially_blocked"
        elif stalled or paused or (failed and queued and not continue_on_error):
            status = "partially_blocked" if (continue_on_error and (running or queued)) else "needs_action"
        elif failed and (running or queued) and continue_on_error:
            status = "partially_blocked"
        elif running:
            status = "running"
        elif queued:
            status = "queued"
        elif succeeded and (failed or cancelled):
            status = "partially_succeeded"
        elif failed or cancelled:
            status = "failed"
        else:
            status = "queued"

        terminal = status in TERMINAL_BATCH_STATUSES
        finished_at = now() if terminal else ""
        conn.execute(
            """UPDATE batch_tasks SET completed_count=?,failed_count=?,status=?,updated_at=?,
               finished_at=CASE WHEN ?<>'' THEN ? ELSE '' END WHERE id=?""",
            (succeeded, failed, status, now(), finished_at, finished_at, int(batch_id)),
        )
    avg_progress = round(sum(float(item["progress"] or 0) for item in rows_) / max(1, total))
    return {
        "total": total, "succeeded": succeeded, "failed": failed, "stalled": stalled,
        "paused": paused, "cancelled": cancelled, "queued": queued, "running": running,
        "preparing": preparing, "rendering": rendering, "verifying": verifying,
        "needs_action": stalled + paused, "progress": avg_progress, "status": status,
    }


def _sync_items_from_tasks(batch_id: int) -> None:
    with connect() as conn:
        records = conn.execute(
            """SELECT i.id,g.status,g.output_path,g.error_message
               FROM batch_task_items i JOIN generation_tasks g ON g.id=i.task_id
               WHERE i.batch_id=?""", (int(batch_id),)
        ).fetchall()
        for record in records:
            status = canonical_task_status(record["status"])
            conn.execute(
                "UPDATE batch_task_items SET status=?,output_path=?,error_message=?,updated_at=? WHERE id=?",
                (status, record["output_path"] or "", record["error_message"] or "", now(), int(record["id"])),
            )


def _mark_stalled(task_id: int, item_id: int, age: float) -> None:
    reason = (
        f"任务心跳已中断 {int(age)} 秒，Worker、FFmpeg 和 FFprobe 均已退出。"
        "任务已安全标记为停滞，请查看日志后终止并重试。"
    )
    with connect() as conn:
        conn.execute(
            """UPDATE generation_tasks SET status='stalled',current_stage='stalled',
               current_step='任务已停滞',current_shot='任务已停滞',worker_id='',process_id=0,
               process_name='',ffmpeg_pid=0,ffprobe_pid=0,error_code='WORKER_STALLED',
               error_message=?,updated_at=? WHERE id=?""",
            (reason, now(), int(task_id)),
        )
        conn.execute(
            "UPDATE batch_task_items SET status='stalled',error_message=?,updated_at=? WHERE id=?",
            (reason, now(), int(item_id)),
        )
    append_task_log(task_id, f"停滞诊断 · {reason}")


def process_batch(batch_id: int, start_generation: Callable[[int], object], poll_interval: float = 0.5) -> str:
    """Run one batch serially.  Default policy pauses at the first child error."""
    while True:
        with connect() as conn:
            batch = conn.execute("SELECT * FROM batch_tasks WHERE id=?", (int(batch_id),)).fetchone()
            if not batch:
                return "missing"
            if batch["status"] in TERMINAL_BATCH_STATUSES or batch["status"] == "cancelled":
                return str(batch["status"])
            items = conn.execute(
                """SELECT i.*,g.status task_status,g.last_heartbeat_at,g.updated_at task_updated_at
                   FROM batch_task_items i JOIN generation_tasks g ON g.id=i.task_id
                   WHERE i.batch_id=? ORDER BY i.index_no,i.id""", (int(batch_id),)
            ).fetchall()
        _sync_items_from_tasks(batch_id)
        summary = sync_batch(batch_id)
        if not items:
            return summary.get("status", "failed")

        active_item = next(
            (item for item in items if canonical_task_status(item["task_status"]) in {"preparing", "rendering", "verifying"}),
            None,
        )
        if active_item:
            heartbeat_value = str(active_item["last_heartbeat_at"] or active_item["task_updated_at"] or "")
            age = heartbeat_age_seconds(heartbeat_value)
            if age is not None and age > HEARTBEAT_STALLED_SECONDS:
                health = task_health_snapshot(int(active_item["task_id"]))
                if not any((
                    health.get("worker_alive"), health.get("process_alive"),
                    health.get("ffmpeg_alive"), health.get("ffprobe_alive"),
                )):
                    _mark_stalled(int(active_item["task_id"]), int(active_item["id"]), age)
                    result = sync_batch(batch_id)
                    if not bool(batch["continue_on_child_error"]):
                        return "needs_action"
                    continue
            time.sleep(max(0.05, poll_interval))
            continue

        blocked = next(
            (item for item in items if canonical_task_status(item["task_status"]) in {"failed", "stalled", "paused"}),
            None,
        )
        if blocked and not bool(batch["continue_on_child_error"]):
            sync_batch(batch_id)
            return "needs_action"

        queued_item = next(
            (item for item in items if canonical_task_status(item["task_status"]) == "queued"),
            None,
        )
        if queued_item:
            try:
                result = start_generation(int(queued_item["task_id"])) or {}
                returned_status = canonical_task_status(
                    result.get("status", "queued") if isinstance(result, dict) else "queued"
                )
                with connect() as conn:
                    conn.execute(
                        "UPDATE batch_task_items SET status=?,error_message='',updated_at=? WHERE id=?",
                        (returned_status, now(), int(queued_item["id"])),
                    )
            except BaseException as exc:
                reason = f"启动生成任务失败：{type(exc).__name__}: {exc}"
                with connect() as conn:
                    conn.execute(
                        """UPDATE generation_tasks SET status='failed',current_stage='failed',
                           current_step='启动失败',error_code='START_FAILED',error_message=?,
                           finished_at=?,updated_at=? WHERE id=?""",
                        (reason, now(), now(), int(queued_item["task_id"])),
                    )
                    conn.execute(
                        "UPDATE batch_task_items SET status='failed',error_message=?,updated_at=? WHERE id=?",
                        (reason, now(), int(queued_item["id"])),
                    )
                append_task_log(int(queued_item["task_id"]), reason)
                sync_batch(batch_id)
                if not bool(batch["continue_on_child_error"]):
                    return "needs_action"
            time.sleep(max(0.05, poll_interval))
            continue

        return sync_batch(batch_id).get("status", "failed")


class SerialBatchQueue:
    """A durable, one-batch-at-a-time queue backed by database task claims."""

    def __init__(self):
        self._queue: queue.Queue[tuple[int, Callable[[int], object]]] = queue.Queue()
        self._lock = threading.Lock()
        self._enqueued: set[int] = set()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._status = "stopped"
        self._current_batch_id = 0

    def ensure_started(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._status = "idle"
            self._thread = threading.Thread(target=self._worker, name="mixcut-batch-serial", daemon=True)
            self._thread.start()
            return True

    def signal(self) -> None:
        self.ensure_started()
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "worker_status": self._status,
                "current_batch_id": int(self._current_batch_id or 0),
                "pending_batches": len(self._enqueued),
            }

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            thread = self._thread
            self._status = "stopping"
            self._stop.set()
            self._wake.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def enqueue(self, batch_id: int, callback: Callable[[int], object]) -> None:
        with self._lock:
            if int(batch_id) in self._enqueued:
                self._wake.set()
                return
            self._enqueued.add(int(batch_id))
            self._queue.put((int(batch_id), callback))
        self.ensure_started()
        self._wake.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                batch_id, callback = self._queue.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    self._status = "idle"
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            with self._lock:
                self._status = "running"
                self._current_batch_id = int(batch_id)
            try:
                result = process_batch(batch_id, callback)
                # A default-policy error blocks later batches until the user
                # retries, cancels, or explicitly changes the batch policy.
                while result == "needs_action":
                    time.sleep(1)
                    with connect() as conn:
                        current = conn.execute(
                            "SELECT status FROM batch_tasks WHERE id=?", (int(batch_id),)
                        ).fetchone()
                    if not current or current["status"] in TERMINAL_BATCH_STATUSES or current["status"] == "cancelled":
                        break
                    if current["status"] in {"queued", "running", "partially_blocked"}:
                        result = process_batch(batch_id, callback)
            except BaseException as exc:
                reason = f"批次队列异常中断：{type(exc).__name__}: {exc}"
                try:
                    with connect() as conn:
                        conn.execute(
                            "UPDATE batch_tasks SET status='needs_action',updated_at=? WHERE id=?",
                            (now(), int(batch_id)),
                        )
                        conn.execute(
                            """UPDATE generation_tasks SET status='failed',current_stage='failed',
                               current_step='批次队列异常',error_code='BATCH_QUEUE_ERROR',
                               error_message=?,finished_at=?,updated_at=?
                               WHERE id IN (SELECT task_id FROM batch_task_items WHERE batch_id=?)
                               AND status IN ('preparing','rendering','verifying')""",
                            (reason, now(), now(), int(batch_id)),
                        )
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._enqueued.discard(int(batch_id))
                    self._current_batch_id = 0
                    self._status = "idle"
                self._queue.task_done()
                self._wake.set()
        with self._lock:
            self._status = "stopped"
            self._current_batch_id = 0
            self._thread = None


BATCH_QUEUE = SerialBatchQueue()


def recover_and_enqueue(callback: Callable[[int], object]) -> list[int]:
    """Recover the serial queue without auto-retrying interrupted children."""
    with connect() as conn:
        interrupted = conn.execute(
            """SELECT g.id task_id,i.id item_id,i.batch_id
               FROM generation_tasks g
               LEFT JOIN batch_task_items i ON i.task_id=g.id
               WHERE g.status IN ('preparing','rendering','verifying','running','processing','processing_shot','merging')"""
        ).fetchall()
        for item in interrupted:
            reason = "软件重启后检测到旧 Worker 已退出，任务执行现场已保留，请诊断后重试。"
            conn.execute(
                """UPDATE generation_tasks SET status='stalled',current_stage='stalled',
                   current_step='任务被异常中断',current_shot='等待人工处理',worker_id='',
                   process_id=0,process_name='',ffmpeg_pid=0,ffprobe_pid=0,
                   error_code='INTERRUPTED',error_message=?,updated_at=? WHERE id=?""",
                (reason, now(), int(item["task_id"])),
            )
            if item["item_id"]:
                conn.execute(
                    "UPDATE batch_task_items SET status='stalled',error_message=?,updated_at=? WHERE id=?",
                    (reason, now(), int(item["item_id"])),
                )
        batch_ids = [int(row["id"]) for row in conn.execute(
            """SELECT b.id,MIN(CASE WHEN COALESCE(g.queue_seq,0)>0 THEN g.queue_seq ELSE g.id END) first_queue_seq
               FROM batch_tasks b
               JOIN batch_task_items i ON i.batch_id=b.id
               JOIN generation_tasks g ON g.id=i.task_id
               WHERE b.status IN ('queued','running','partially_blocked')
               GROUP BY b.id
               ORDER BY first_queue_seq,b.id"""
        ).fetchall()]
    recoverable = []
    for batch_id in batch_ids:
        summary = sync_batch(batch_id)
        if summary.get("status") in {"queued", "running", "partially_blocked"}:
            BATCH_QUEUE.enqueue(batch_id, callback)
            recoverable.append(batch_id)
    return recoverable
