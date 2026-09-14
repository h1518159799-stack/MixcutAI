"""Generation task heartbeat, cancellation and managed subprocess helpers."""

from __future__ import annotations

import os
import itertools
import queue
import subprocess
import threading
import time
import uuid
import re
from contextlib import contextmanager
from datetime import datetime

from db import connect, now
from runtime_paths import TASK_LOG_DIR, ensure_runtime_dirs
from subprocess_utils import background_process_kwargs, infer_process_stage


HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_SUSPECT_SECONDS = 60
HEARTBEAT_STALLED_SECONDS = 180
MAX_GENERATION_CONCURRENCY = 1

TASK_STATUS_ALIASES = {
    "pending": "queued", "running": "preparing", "processing": "preparing",
    "processing_shot": "preparing", "merging": "verifying",
    "completed": "succeeded", "cancelling": "cancelled",
}
QUEUED_TASK_STATUSES = {"queued", "pending"}
ACTIVE_TASK_STATUSES = {
    "preparing", "rendering", "verifying",
    "running", "processing", "processing_shot", "merging",
}
TERMINAL_TASK_STATUSES = {"succeeded", "completed", "failed", "stalled", "paused", "cancelled"}

_local = threading.local()
_lock = threading.RLock()
_workers: dict[int, threading.Thread] = {}
_processes: dict[int, subprocess.Popen] = {}

_SECRET_RE = re.compile(r"(?i)(api[_ -]?key|authorization|bearer|access[_ -]?token|refresh[_ -]?token|cookie|client[_ -]?secret)(\s*[:=]\s*)([^\s,;]+)")


def _safe_log_text(value: object) -> str:
    text = str(value or "").replace("\x00", "")
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)


def canonical_task_status(value: object) -> str:
    status = str(value or "queued").strip().lower()
    return TASK_STATUS_ALIASES.get(status, status)


def process_is_alive(pid: int | str | None) -> bool:
    try:
        value = int(pid or 0)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def task_log_path(task_id: int) -> str:
    ensure_runtime_dirs()
    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return str(TASK_LOG_DIR / f"{int(task_id)}.log")


def initialize_task_log(task_id: int, task_name: str = "") -> str:
    """Create the durable UTF-8 task log before any worker can fail."""
    path = task_log_path(task_id)
    log_id = f"MIX-{datetime.now():%Y%m%d}-{int(task_id):06d}"
    file = __import__("pathlib").Path(path)
    if not file.exists():
        file.write_text(f"[{now()}] 任务创建 · {_safe_log_text(task_name or task_id)}\n", encoding="utf-8")
    with connect() as conn:
        conn.execute(
            "UPDATE generation_tasks SET log_path=?,technical_log_id=CASE WHEN technical_log_id='' THEN ? ELSE technical_log_id END WHERE id=?",
            (path, log_id, int(task_id)),
        )
    return path


def append_task_log(task_id: int, message: str) -> None:
    line = _safe_log_text(message).strip()
    if not line:
        return
    path = initialize_task_log(task_id)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"[{now()}] {line}\n")
    except OSError:
        # The database copy remains available even if the filesystem is read-only.
        pass


def append_generation_worker_log(message: str) -> None:
    """Write scheduler lifecycle evidence without exposing task secrets."""
    ensure_runtime_dirs()
    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(TASK_LOG_DIR / "generation-worker.log", "a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"[{now()}] {_safe_log_text(message).strip()}\n")
    except OSError:
        pass


def classify_task_error(exc: Exception, stage: str = "") -> tuple[str, str]:
    raw = str(exc or "").strip()
    lowered = raw.lower()
    ai_code = str(getattr(exc, "code", "") or "")
    if ai_code.startswith("AI_"):
        return ai_code, raw or "AI\u6587\u6848\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528\u3002"
    if "no audio was received" in lowered or "edge tts" in lowered:
        return "TTS_NO_AUDIO", "配音服务没有返回有效音频，请检查网络后重试，或在系统设置中切换其他配音服务。"
    if "ffmpeg" in lowered and ("未找到" in raw or "not found" in lowered):
        return "FFMPEG_NOT_FOUND", "未找到可用的 FFmpeg 或 FFprobe，请检查系统设置中的运行组件。"
    if "素材" in raw and ("不存在" in raw or "删除" in raw):
        return "MATERIAL_NOT_FOUND", "生成所需的素材文件已被移动或删除，请重新扫描或替换素材。"
    if "无法读取真实语音时长" in raw or ("配音" in raw and "不存在" in raw):
        return "VOICE_INVALID", "配音文件缺失或无法解析，请重新生成配音。"
    if "permission" in lowered or "拒绝访问" in raw or "无法写入" in raw:
        return "OUTPUT_NOT_WRITABLE", "无法写入输出目录，请更换目录或检查磁盘权限。"
    if "no space" in lowered or "磁盘空间" in raw:
        return "DISK_FULL", "磁盘剩余空间不足，无法继续生成视频。"
    if stage in {"rendering", "merging", "timeline", "verifying"}:
        return "FFMPEG_FAILED", "视频处理组件执行失败，请打开任务日志查看具体原因。"
    return "GENERATION_FAILED", raw or "视频生成失败，请打开任务日志查看具体原因。"


def fail_task(task_id: int, exc: Exception, stage: str = "") -> tuple[str, str]:
    failed_stage = stage or current_stage() or "unknown"
    error_code, message = classify_task_error(exc, failed_stage)
    detail = f"{type(exc).__name__}: {_safe_log_text(exc)}"
    append_task_log(task_id, f"任务失败 · 阶段={failed_stage} · 错误码={error_code} · {detail}")
    with connect() as conn:
        conn.execute(
            """UPDATE generation_tasks SET status='failed',current_stage='failed',failed_stage=?,
               current_step='生成失败',current_shot='生成失败',process_id=0,process_name='',ffmpeg_pid=0,
               ffprobe_pid=0,worker_id='',error_code=?,error_message=?,
               last_log_at=?,finished_at=?,diagnostic_log=substr(COALESCE(diagnostic_log,'') || ?, -12000),updated_at=? WHERE id=?""",
            (failed_stage, error_code, message, now(), now(), f"\n[{now()}] {detail}", now(), int(task_id)),
        )
    return error_code, message


class TaskCancelled(RuntimeError):
    pass


class ManagedProcessTimeout(RuntimeError):
    pass


def current_task_id() -> int:
    return int(getattr(_local, "task_id", 0) or 0)


def current_stage() -> str:
    return str(getattr(_local, "stage", "") or "")


def _append_log(existing: str, message: str) -> str:
    line = _safe_log_text(message).strip()
    if not line:
        return str(existing or "")
    stamp = now()
    merged = (str(existing or "") + f"\n[{stamp}] {line}").strip()
    return merged[-12000:]


def heartbeat(task_id: int | None = None, *, status: str | None = None,
              stage: str | None = None, step: str | None = None,
              item: str | None = None, progress: int | None = None,
              process_id: int | None = None, log: str = "") -> None:
    task_id = int(task_id or current_task_id() or 0)
    if not task_id:
        return
    timestamp = now()
    with connect() as conn:
        record = conn.execute(
            "SELECT diagnostic_log,cancel_requested,current_stage,current_step FROM generation_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not record:
            return
        fields = {"last_heartbeat_at": timestamp, "updated_at": timestamp}
        if status is not None:
            fields["status"] = status
        if stage is not None:
            fields["current_stage"] = stage
            _local.stage = stage
        if step is not None:
            fields["current_step"] = step
            fields["current_shot"] = step
        if item is not None:
            fields["current_item"] = item
        if progress is not None:
            bounded = max(0, min(100, int(progress)))
            fields["current_progress"] = bounded
            fields["progress"] = bounded
        if process_id is not None:
            fields["process_id"] = int(process_id or 0)
        transition_log = log
        if not transition_log and ((stage is not None and stage != record["current_stage"]) or (step is not None and step != record["current_step"])):
            transition_log = f"阶段更新 · {stage or record['current_stage'] or 'unknown'} · {step or record['current_step'] or ''}".strip()
        if transition_log:
            fields["diagnostic_log"] = _append_log(record["diagnostic_log"], transition_log)
            fields["last_log_at"] = timestamp
        sets = ",".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE generation_tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
    if transition_log:
        append_task_log(task_id, transition_log)


def task_cancel_requested(task_id: int | None = None) -> bool:
    task_id = int(task_id or current_task_id() or 0)
    if not task_id:
        return False
    with connect() as conn:
        row = conn.execute("SELECT cancel_requested,status FROM generation_tasks WHERE id=?", (task_id,)).fetchone()
    return bool(row and (int(row["cancel_requested"] or 0) or row["status"] in {"cancelled", "cancelling"}))


def check_cancelled(task_id: int | None = None) -> None:
    if task_cancel_requested(task_id):
        raise TaskCancelled("任务已取消")


def register_worker(task_id: int, thread: threading.Thread) -> str:
    worker_id = f"{os.getpid()}:{thread.name}:{uuid.uuid4().hex[:8]}"
    with _lock:
        _workers[int(task_id)] = thread
    with connect() as conn:
        conn.execute(
            "UPDATE generation_tasks SET worker_id=?,started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,"
            "last_heartbeat_at=?,updated_at=? WHERE id=?",
            (worker_id, now(), now(), now(), int(task_id)),
        )
    append_task_log(task_id, f"Worker 领取任务 · worker_id={worker_id}")
    return worker_id


def unregister_worker(task_id: int) -> None:
    with _lock:
        _workers.pop(int(task_id), None)
    try:
        with connect() as conn:
            conn.execute(
                "UPDATE generation_tasks SET worker_id='',process_id=0,process_name='',ffmpeg_pid=0,ffprobe_pid=0,"
                "last_heartbeat_at=?,updated_at=? WHERE id=?",
                (now(), now(), int(task_id)),
            )
    except Exception:
        pass


def worker_is_alive(task_id: int) -> bool:
    with _lock:
        thread = _workers.get(int(task_id))
    if thread and thread.is_alive():
        return True
    try:
        with connect() as conn:
            row = conn.execute("SELECT worker_id FROM generation_tasks WHERE id=?", (int(task_id),)).fetchone()
        worker_id = str(row["worker_id"] or "") if row else ""
        pid = int(worker_id.split(":", 1)[0]) if worker_id else 0
        if not pid or pid == os.getpid():
            return False
        return process_is_alive(pid)
    except (OSError, ValueError, TypeError):
        return False


def generation_worker_snapshot() -> dict:
    """Return a non-sensitive snapshot of the in-process generation runtime."""
    with _lock:
        alive = [int(task_id) for task_id, thread in _workers.items() if thread.is_alive()]
        process_tasks = [int(task_id) for task_id, process in _processes.items() if process.poll() is None]
    return {
        "alive_task_ids": sorted(alive),
        "alive_count": len(alive),
        "managed_process_task_ids": sorted(process_tasks),
        "dispatcher_alive": GENERATION_QUEUE.snapshot()["worker_alive"],
        **GENERATION_QUEUE.snapshot(),
    }


def _heartbeat_loop(task_id: int, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            heartbeat(task_id)
        except Exception:
            pass


@contextmanager
def task_context(task_id: int):
    previous = current_task_id()
    _local.task_id = int(task_id)
    _local.stage = ""
    stop = threading.Event()
    initialize_task_log(task_id)
    register_worker(task_id, threading.current_thread())
    ticker = threading.Thread(
        target=_heartbeat_loop, args=(int(task_id), stop), daemon=True,
        name=f"task-{task_id}-heartbeat",
    )
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        try:
            unregister_worker(task_id)
        finally:
            append_task_log(task_id, "Worker 已退出并释放任务上下文")
        _local.task_id = previous


def task_health_snapshot(task_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT worker_id,process_id,process_name,ffmpeg_pid,ffprobe_pid,last_heartbeat_at,status "
            "FROM generation_tasks WHERE id=?", (int(task_id),)
        ).fetchone()
    if not row:
        return {"worker_alive": False, "process_alive": False, "ffmpeg_alive": False,
                "ffprobe_alive": False, "heartbeat_age_seconds": None}
    return {
        "worker_alive": worker_is_alive(int(task_id)),
        "process_alive": process_is_alive(row["process_id"]),
        "process_name": str(row["process_name"] or ""),
        "ffmpeg_alive": process_is_alive(row["ffmpeg_pid"]),
        "ffprobe_alive": process_is_alive(row["ffprobe_pid"]),
        "heartbeat_age_seconds": heartbeat_age_seconds(str(row["last_heartbeat_at"] or "")),
        "status": canonical_task_status(row["status"]),
    }


def try_claim_generation_task(task_id: int, worker_token: str) -> tuple[bool, int | None]:
    """Claim the one global video-generation slot with a SQLite transaction."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(generation_tasks)")}
        queue_seq_enabled = "queue_seq" in columns
        task = conn.execute(
            ("SELECT status,queue_seq FROM generation_tasks WHERE id=?" if queue_seq_enabled
             else "SELECT status FROM generation_tasks WHERE id=?"),
            (int(task_id),),
        ).fetchone()
        if not task:
            conn.rollback()
            raise ValueError("任务不存在")
        task_status = canonical_task_status(task["status"])
        if task_status in ACTIVE_TASK_STATUSES:
            conn.rollback()
            return False, int(task_id)
        if task_status != "queued":
            conn.rollback()
            return False, None
        marks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
        active = conn.execute(
            f"SELECT id FROM generation_tasks WHERE id<>? AND status IN ({marks}) ORDER BY id LIMIT 1",
            (int(task_id), *sorted(ACTIVE_TASK_STATUSES)),
        ).fetchone()
        if active:
            conn.rollback()
            return False, int(active["id"])
        earliest = conn.execute(
            ("""SELECT id FROM generation_tasks WHERE status IN ('queued','pending')
                ORDER BY CASE WHEN COALESCE(queue_seq,0)>0 THEN queue_seq ELSE id END,id LIMIT 1"""
             if queue_seq_enabled else
             "SELECT id FROM generation_tasks WHERE status IN ('queued','pending') ORDER BY id LIMIT 1")
        ).fetchone()
        if earliest and int(earliest["id"]) != int(task_id):
            conn.rollback()
            return False, int(earliest["id"])
        stamp = now()
        conn.execute(
            """UPDATE generation_tasks SET status='preparing',current_stage='preparing',
               current_step='准备生成',current_shot='准备生成',progress=MAX(progress,1),
               current_progress=MAX(current_progress,1),error_message='',cancel_requested=0,
               worker_id=?,started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,
               last_heartbeat_at=?,updated_at=? WHERE id=?""",
            (worker_token, stamp, stamp, stamp, int(task_id)),
        )
        conn.commit()
    append_task_log(task_id, f"已原子领取生成任务 · worker={worker_token}")
    return True, None


class SerialGenerationQueue:
    """One process-wide dispatcher for every video generation task.

    The SQLite active-slot index remains the cross-process safety net.  This
    dispatcher removes the old one-waiting-thread-per-task race and guarantees
    that this process never invokes two render workers at the same time.
    """

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue[tuple[int, int, int, object]] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._lock = threading.RLock()
        self._pending: set[int] = set()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._status = "stopped"
        self._worker_id = ""
        self._started_at = ""
        self._last_heartbeat = ""
        self._current_task_id = 0

    def ensure_started(self) -> bool:
        """Start the only generation consumer and keep it alive until app shutdown."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._worker_id = f"{os.getpid()}:generation:{uuid.uuid4().hex[:8]}"
            self._started_at = now()
            self._last_heartbeat = self._started_at
            self._status = "idle"
            self._thread = threading.Thread(
                target=self._worker, name="mixcut-generation-serial", daemon=True
            )
            self._thread.start()
        append_generation_worker_log(f"Generation Worker started · worker_id={self._worker_id}")
        return True

    def signal(self) -> None:
        self.ensure_started()
        self._wake.set()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            thread = self._thread
            self._status = "stopping"
            self._stop.set()
            self._wake.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def snapshot(self) -> dict:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return {
                "worker_status": self._status if alive else "stopped",
                "worker_id": self._worker_id,
                "worker_started_at": self._started_at,
                "worker_last_heartbeat": self._last_heartbeat,
                "current_task_id": int(self._current_task_id or 0),
                "worker_alive": alive,
                "pending_count": len(self._pending),
            }

    def enqueue(self, task_id: int, runner, *, priority: int = 10) -> bool:
        task_id = int(task_id)
        with self._lock:
            if task_id in self._pending:
                self._wake.set()
                return False
            self._pending.add(task_id)
            self._queue.put((int(priority), next(self._sequence), task_id, runner))
        self.ensure_started()
        self._wake.set()
        append_task_log(task_id, "任务已进入全局串行生成队列")
        append_generation_worker_log(f"Queue wakeup · enqueue task #{task_id} · priority={int(priority)}")
        return True

    def is_pending(self, task_id: int) -> bool:
        with self._lock:
            return int(task_id) in self._pending

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _worker(self) -> None:
        idle_logged = False
        while not self._stop.is_set():
            with self._lock:
                self._last_heartbeat = now()
                if not self._current_task_id:
                    self._status = "idle"
            requeued = False
            try:
                _priority, _sequence, task_id, runner = self._queue.get(timeout=0.5)
            except queue.Empty:
                if not idle_logged:
                    append_generation_worker_log("Worker idle")
                    idle_logged = True
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            idle_logged = False
            with self._lock:
                self._status = "running"
                self._current_task_id = int(task_id)
                self._last_heartbeat = now()
            append_generation_worker_log(f"Claim task #{task_id}")
            try:
                while True:
                    with connect() as conn:
                        record = conn.execute(
                            "SELECT status FROM generation_tasks WHERE id=?", (int(task_id),)
                        ).fetchone()
                    if not record or canonical_task_status(record["status"]) != "queued":
                        break
                    claimed, busy_task_id = try_claim_generation_task(
                        task_id, f"{os.getpid()}:serial:{task_id}"
                    )
                    if claimed:
                        append_generation_worker_log(f"Start task #{task_id}")
                        runner()
                        with connect() as conn:
                            finished = conn.execute(
                                "SELECT status FROM generation_tasks WHERE id=?", (int(task_id),)
                            ).fetchone()
                        final_status = canonical_task_status(finished["status"]) if finished else "missing"
                        append_generation_worker_log(f"Task #{task_id} {final_status}")
                        break
                    if busy_task_id is None:
                        break
                    with connect() as conn:
                        blocker = conn.execute(
                            "SELECT status FROM generation_tasks WHERE id=?", (int(busy_task_id),)
                        ).fetchone()
                    blocker_status = canonical_task_status(blocker["status"]) if blocker else ""
                    if blocker_status == "queued" and int(busy_task_id) != int(task_id):
                        # The database found an older durable task.  Put this
                        # entry back and let the PriorityQueue select the older
                        # callback as soon as its batch/standalone owner wakes.
                        self._queue.put((_priority, next(self._sequence), task_id, runner))
                        requeued = True
                        append_generation_worker_log(
                            f"Defer task #{task_id} · older queued task #{busy_task_id}"
                        )
                        time.sleep(0.1)
                        break
                    time.sleep(0.5)
            except BaseException as exc:
                # The runner normally records its own failure.  This fallback
                # covers errors before task_context is entered.
                try:
                    fail_task(task_id, exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
                except Exception:
                    pass
            finally:
                with self._lock:
                    if not requeued:
                        self._pending.discard(int(task_id))
                    self._current_task_id = 0
                    self._status = "idle"
                    self._last_heartbeat = now()
                self._queue.task_done()
                append_generation_worker_log(
                    f"{'Defer' if requeued else 'Release'} task #{task_id} · claim next"
                )
                self._wake.set()
        with self._lock:
            self._status = "stopped"
            self._current_task_id = 0
            self._thread = None
            self._last_heartbeat = now()
        append_generation_worker_log("Generation Worker stopped")


GENERATION_QUEUE = SerialGenerationQueue()


def terminate_process(task_id: int) -> bool:
    with _lock:
        process = _processes.get(int(task_id))
    if not process or process.poll() is not None:
        return False
    try:
        if os.name == "nt":
            command = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
            subprocess.run(command, capture_output=True, timeout=10,
                           **background_process_kwargs(command, "task_cancel"))
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return True
    except Exception:
        try:
            process.kill()
            return True
        except Exception:
            return False


def cancel_task(task_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT status FROM generation_tasks WHERE id=?", (int(task_id),)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        if canonical_task_status(row["status"]) in {"succeeded", "failed", "cancelled"}:
            return {"task_id": int(task_id), "status": row["status"]}
        conn.execute(
            "UPDATE generation_tasks SET cancel_requested=1,status='cancelled',current_stage='cancelled',"
            "current_step='正在取消',current_shot='正在取消',updated_at=? WHERE id=?",
            (now(), int(task_id)),
        )
    terminate_process(int(task_id))
    return {"task_id": int(task_id), "status": "cancelled"}


def run_managed_command(cmd: list[str], *, timeout: float, error_type=RuntimeError,
                        failure_message: str = "外部程序执行失败") -> subprocess.CompletedProcess:
    task_id = current_task_id()
    check_cancelled(task_id)
    started = time.monotonic()
    spawn_started = time.monotonic()
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="ignore", shell=False,
        **background_process_kwargs(cmd, infer_process_stage(cmd, "managed_external"))
    )
    try:
        from generation_performance import event
        event("ffmpeg_process_spawn", time.monotonic() - spawn_started, executable=str(cmd[0]))
        event("ffmpeg_render_start", 0.0, executable=str(cmd[0]))
    except ImportError:
        pass
    if task_id:
        with _lock:
            _processes[task_id] = process
        executable = __import__("pathlib").Path(str(cmd[0])).name
        executable_lower = executable.lower()
        ffmpeg_pid = process.pid if "ffmpeg" in executable_lower and "ffprobe" not in executable_lower else 0
        ffprobe_pid = process.pid if "ffprobe" in executable_lower else 0
        with connect() as conn:
            conn.execute(
                "UPDATE generation_tasks SET process_id=?,process_name=?,ffmpeg_pid=?,ffprobe_pid=?,updated_at=? WHERE id=?",
                (process.pid, executable, ffmpeg_pid, ffprobe_pid, now(), int(task_id)),
            )
        input_count = sum(1 for item in cmd if str(item) == "-i")
        heartbeat(task_id, process_id=process.pid,
                  log=f"启动外部进程 · 程序={executable} · 输入数={input_count} · PID={process.pid}")
    try:
        wait_started = time.monotonic()
        stdout, stderr = process.communicate(timeout=max(1.0, float(timeout)))
        try:
            from generation_performance import event
            event("ffmpeg_wait_exit", time.monotonic() - wait_started, returncode=process.returncode)
            event("ffmpeg_render_end", time.monotonic() - started, returncode=process.returncode)
        except ImportError:
            pass
    except subprocess.TimeoutExpired:
        if task_id:
            terminate_process(task_id)
        else:
            process.kill()
        stdout, stderr = process.communicate()
        elapsed = time.monotonic() - started
        message = f"{failure_message}：执行超过 {elapsed:.0f} 秒，进程已终止"
        if task_id:
            heartbeat(task_id, process_id=0, log=message)
        raise ManagedProcessTimeout(message)
    finally:
        if task_id:
            with _lock:
                _processes.pop(task_id, None)
            with connect() as conn:
                conn.execute(
                    "UPDATE generation_tasks SET process_id=0,process_name='',ffmpeg_pid=0,ffprobe_pid=0,updated_at=? WHERE id=?",
                    (now(), int(task_id)),
                )
            heartbeat(task_id, process_id=0)
    check_cancelled(task_id)
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if task_id:
        heartbeat(task_id, log=f"外部进程结束 · 返回码={completed.returncode}")
    if completed.returncode != 0:
        detail = (stderr or stdout or failure_message).strip()[-4000:]
        if task_id:
            recent_lines = "\n".join(detail.splitlines()[-100:])
            heartbeat(task_id, log=f"{failure_message} · 返回码={completed.returncode}\n{recent_lines}")
        raise error_type(detail)
    return completed


def heartbeat_age_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            return max(0.0, (datetime.now(parsed.tzinfo) - parsed).total_seconds())
        return max(0.0, (datetime.now() - parsed).total_seconds())
    except (TypeError, ValueError):
        return None


def heartbeat_state(value: str) -> str:
    age = heartbeat_age_seconds(value)
    if age is None:
        return "unknown"
    if age > HEARTBEAT_STALLED_SECONDS:
        return "stalled"
    if age > HEARTBEAT_SUSPECT_SECONDS:
        return "suspect"
    return "normal"
