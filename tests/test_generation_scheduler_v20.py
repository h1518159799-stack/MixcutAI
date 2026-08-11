from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_service
import commercial_api
import db
import task_runtime


class GenerationSchedulerV20Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp.name) / "scheduler.db"
        self.output_dir = Path(self.temp.name) / "outputs"
        self.output_dir.mkdir()
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE generation_tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,template_id INTEGER,task_name TEXT,status TEXT DEFAULT 'queued',
                    progress INTEGER DEFAULT 0,current_progress INTEGER DEFAULT 0,current_shot TEXT DEFAULT '',
                    queue_seq INTEGER NOT NULL DEFAULT 0,
                    total_shots INTEGER DEFAULT 0,output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',updated_at TEXT DEFAULT '',started_at TEXT DEFAULT '',finished_at TEXT DEFAULT '',
                    last_heartbeat_at TEXT DEFAULT '',current_stage TEXT DEFAULT '',current_step TEXT DEFAULT '',
                    current_item TEXT DEFAULT '',worker_id TEXT DEFAULT '',process_id INTEGER DEFAULT 0,
                    process_name TEXT DEFAULT '',ffmpeg_pid INTEGER DEFAULT 0,ffprobe_pid INTEGER DEFAULT 0,
                    cancel_requested INTEGER DEFAULT 0,retry_count INTEGER DEFAULT 0,retry_of_task_id INTEGER,
                    diagnostic_log TEXT DEFAULT '',last_log_at TEXT DEFAULT '',error_code TEXT DEFAULT '',
                    failed_stage TEXT DEFAULT '',technical_log_id TEXT DEFAULT '',log_path TEXT DEFAULT '',attempt_no INTEGER DEFAULT 1
                );
                CREATE UNIQUE INDEX uq_single_active_generation_slot ON generation_tasks((1))
                    WHERE status IN ('preparing','rendering','verifying');
                CREATE UNIQUE INDEX uq_generation_tasks_queue_seq ON generation_tasks(queue_seq) WHERE queue_seq>0;
                CREATE TABLE batch_tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,template_id INTEGER,total_count INTEGER DEFAULT 0,
                    completed_count INTEGER DEFAULT 0,failed_count INTEGER DEFAULT 0,status TEXT DEFAULT 'queued',
                    material_strategy TEXT DEFAULT 'random',category_ids_json TEXT DEFAULT '[]',
                    continue_on_child_error INTEGER DEFAULT 0,created_at TEXT DEFAULT '',updated_at TEXT DEFAULT '',finished_at TEXT DEFAULT ''
                );
                CREATE TABLE batch_task_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id INTEGER,task_id INTEGER,index_no INTEGER,status TEXT DEFAULT 'queued',
                    output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',created_at TEXT DEFAULT '',updated_at TEXT DEFAULT ''
                );
                """
            )
        self.patches = [
            patch.object(task_runtime, "connect", self.connect),
            patch.object(batch_service, "connect", self.connect),
            patch.object(commercial_api, "connect", self.connect),
            patch.object(task_runtime, "TASK_LOG_DIR", Path(self.temp.name) / "logs"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5, factory=db.ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def make_batch(self, statuses, progresses=None, continue_on_error=False):
        progresses = progresses or [0] * len(statuses)
        stamp = db.now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO batch_tasks(name,template_id,total_count,status,continue_on_child_error,created_at,updated_at) VALUES('test',1,?,'queued',?,?,?)",
                (len(statuses), int(continue_on_error), stamp, stamp),
            )
            batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            task_ids = []
            for index, (status, progress) in enumerate(zip(statuses, progresses), 1):
                conn.execute(
                    "INSERT INTO generation_tasks(template_id,task_name,status,progress,current_progress,created_at,updated_at,last_heartbeat_at) VALUES(1,?,?,?,?,?,?,?)",
                    (f"{index:03d}/{len(statuses):03d}", status, progress, progress, stamp, stamp, stamp),
                )
                task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                task_ids.append(task_id)
                conn.execute(
                    "INSERT INTO batch_task_items(batch_id,task_id,index_no,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, task_id, index, status, stamp, stamp),
                )
        return int(batch_id), [int(value) for value in task_ids]

    def test_summary_counts_only_succeeded_and_uses_average_progress(self):
        batch_id, _ = self.make_batch(
            ["stalled", "rendering", "queued"], [22, 42, 0]
        )
        summary = batch_service.sync_batch(batch_id)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["stalled"], 1)
        self.assertEqual(summary["rendering"], 1)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["needs_action"], 1)
        self.assertEqual(summary["progress"], 21)
        self.assertEqual(summary["status"], "partially_blocked")

    def test_default_policy_does_not_start_next_after_stall(self):
        batch_id, _ = self.make_batch(["stalled", "queued"])
        started = []
        result = batch_service.process_batch(batch_id, lambda task_id: started.append(task_id), poll_interval=0.01)
        self.assertEqual(result, "needs_action")
        self.assertEqual(started, [])

    def test_continue_policy_starts_next_after_failure(self):
        batch_id, task_ids = self.make_batch(["failed", "queued"], continue_on_error=True)

        def complete(task_id):
            with self.connect() as conn:
                conn.execute("UPDATE generation_tasks SET status='succeeded',progress=100 WHERE id=?", (task_id,))

        result = batch_service.process_batch(batch_id, complete, poll_interval=0.01)
        self.assertEqual(result, "partially_succeeded")
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM generation_tasks WHERE id=?", (task_ids[1],)).fetchone()[0], "succeeded")

    def test_stale_heartbeat_with_live_ffmpeg_is_not_marked_stalled(self):
        batch_id, task_ids = self.make_batch(["rendering"], [50])
        sleep_calls = {"count": 0}

        def finish_after_wait(_seconds):
            sleep_calls["count"] += 1
            with self.connect() as conn:
                conn.execute("UPDATE generation_tasks SET status='succeeded',progress=100 WHERE id=?", (task_ids[0],))

        with patch.object(batch_service, "heartbeat_age_seconds", return_value=181), \
             patch.object(batch_service, "task_health_snapshot", return_value={"worker_alive": False, "process_alive": False, "ffmpeg_alive": True, "ffprobe_alive": False}), \
             patch.object(batch_service.time, "sleep", side_effect=finish_after_wait):
            result = batch_service.process_batch(batch_id, lambda _task_id: None, poll_interval=0.01)
        self.assertEqual(result, "succeeded")
        self.assertGreaterEqual(sleep_calls["count"], 1)

    def test_dead_worker_and_processes_are_marked_stalled(self):
        batch_id, task_ids = self.make_batch(["rendering", "queued"], [22, 0])
        with patch.object(batch_service, "heartbeat_age_seconds", return_value=181), \
             patch.object(batch_service, "task_health_snapshot", return_value={"worker_alive": False, "process_alive": False, "ffmpeg_alive": False, "ffprobe_alive": False}):
            result = batch_service.process_batch(batch_id, lambda _task_id: None, poll_interval=0.01)
        self.assertEqual(result, "needs_action")
        with self.connect() as conn:
            first = conn.execute("SELECT status,error_code FROM generation_tasks WHERE id=?", (task_ids[0],)).fetchone()
            second = conn.execute("SELECT status FROM generation_tasks WHERE id=?", (task_ids[1],)).fetchone()
        self.assertEqual((first["status"], first["error_code"]), ("stalled", "WORKER_STALLED"))
        self.assertEqual(second["status"], "queued")

    def test_database_claim_allows_only_one_active_task(self):
        _batch_id, task_ids = self.make_batch(["queued", "queued"])
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(task_id):
            barrier.wait()
            outcomes.append((task_id, task_runtime.try_claim_generation_task(task_id, f"worker-{task_id}")))

        threads = [threading.Thread(target=claim, args=(task_id,)) for task_id in task_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(1 for _task_id, result in outcomes if result[0]), 1)
        with self.connect() as conn:
            active = conn.execute("SELECT COUNT(*) FROM generation_tasks WHERE status IN ('preparing','rendering','verifying')").fetchone()[0]
        self.assertEqual(active, 1)

    def test_retry_preserves_original_and_replaces_only_current_batch_item(self):
        batch_id, task_ids = self.make_batch(["stalled", "succeeded"], [22, 100])
        original = task_ids[0]
        with self.connect() as conn:
            conn.execute("UPDATE generation_tasks SET queue_seq=10 WHERE id=?", (task_ids[0],))
            conn.execute("UPDATE generation_tasks SET queue_seq=11 WHERE id=?", (task_ids[1],))
        original_log = Path(self.temp.name) / "logs" / f"{original}.log"
        task_runtime.initialize_task_log(original, "original")
        original_log.write_text("original failure evidence", encoding="utf-8")
        with patch.object(commercial_api, "single_generation_preflight", return_value={}), \
             patch.object(commercial_api, "settings", return_value={"output_dir": str(self.output_dir), "ffmpeg_path": "ffmpeg"}), \
             patch.object(commercial_api, "find_ffmpeg", return_value="ffmpeg"), \
             patch.object(commercial_api.BATCH_QUEUE, "enqueue") as enqueue:
            result = commercial_api.retry_generation_task(original)
        self.assertNotEqual(result["task_id"], original)
        self.assertTrue(original_log.is_file())
        self.assertIn("original failure evidence", original_log.read_text(encoding="utf-8"))
        with self.connect() as conn:
            current_ids = [row[0] for row in conn.execute("SELECT task_id FROM batch_task_items WHERE batch_id=? ORDER BY index_no", (batch_id,))]
            old = conn.execute("SELECT status,retry_count,queue_seq FROM generation_tasks WHERE id=?", (original,)).fetchone()
            retry = conn.execute("SELECT queue_seq FROM generation_tasks WHERE id=?", (result["task_id"],)).fetchone()
            sibling = conn.execute("SELECT queue_seq FROM generation_tasks WHERE id=?", (task_ids[1],)).fetchone()
        self.assertEqual(current_ids, [result["task_id"], task_ids[1]])
        self.assertEqual(old["status"], "stalled")
        self.assertEqual(old["retry_count"], 1)
        self.assertEqual(retry["queue_seq"], 10)
        self.assertEqual(sibling["queue_seq"], 11)
        self.assertGreater(old["queue_seq"], sibling["queue_seq"])
        enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
