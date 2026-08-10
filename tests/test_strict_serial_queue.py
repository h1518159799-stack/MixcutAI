import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_service
import commercial_api
import task_runtime


class StrictSerialQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp.name) / "queue.db"
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE generation_tasks(
                  id INTEGER PRIMARY KEY,status TEXT DEFAULT 'queued',progress INTEGER DEFAULT 0,
                  current_progress INTEGER DEFAULT 0,current_stage TEXT DEFAULT '',current_step TEXT DEFAULT '',
                  current_shot TEXT DEFAULT '',worker_id TEXT DEFAULT '',started_at TEXT DEFAULT '',
                  last_heartbeat_at TEXT DEFAULT '',updated_at TEXT DEFAULT '',log_path TEXT DEFAULT '',
                  technical_log_id TEXT DEFAULT '',diagnostic_log TEXT DEFAULT '',cancel_requested INTEGER DEFAULT 0,
                  process_id INTEGER DEFAULT 0,process_name TEXT DEFAULT '',ffmpeg_pid INTEGER DEFAULT 0,
                  ffprobe_pid INTEGER DEFAULT 0,error_code TEXT DEFAULT '',error_message TEXT DEFAULT '',
                  failed_stage TEXT DEFAULT '',finished_at TEXT DEFAULT ''
                );
                CREATE UNIQUE INDEX uq_one_active ON generation_tasks((1))
                  WHERE status IN ('preparing','rendering','verifying');
                """
            )
            conn.executemany("INSERT INTO generation_tasks(id,status) VALUES(?,'queued')", [(1,), (2,), (3,)])

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def test_global_dispatcher_runs_only_one_task_and_preserves_fifo(self):
        dispatcher = task_runtime.SerialGenerationQueue()
        lock = threading.Lock()
        release_first = threading.Event()
        done = threading.Event()
        running = 0
        max_running = 0
        order = []

        def runner(task_id):
            def execute():
                nonlocal running, max_running
                with lock:
                    running += 1
                    max_running = max(max_running, running)
                    order.append(task_id)
                if task_id == 1:
                    release_first.wait(2)
                time.sleep(0.03)
                with self.connect() as conn:
                    conn.execute("UPDATE generation_tasks SET status='succeeded',progress=100 WHERE id=?", (task_id,))
                with lock:
                    running -= 1
                    if len(order) == 3:
                        done.set()
            return execute

        with patch.object(task_runtime, "connect", self.connect), \
             patch.object(task_runtime, "append_task_log", lambda *_args, **_kwargs: None):
            dispatcher.enqueue(1, runner(1))
            dispatcher.enqueue(2, runner(2))
            dispatcher.enqueue(3, runner(3))
            release_first.set()
            self.assertTrue(done.wait(5))
            self.assertEqual(order, [1, 2, 3])
            self.assertEqual(max_running, 1)
            dispatcher.stop()


class SkipAndContinueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp.name) / "skip.db"
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE generation_tasks(
                  id INTEGER PRIMARY KEY,task_name TEXT,status TEXT,progress INTEGER DEFAULT 0,
                  current_stage TEXT DEFAULT '',current_step TEXT DEFAULT '',current_shot TEXT DEFAULT '',
                  worker_id TEXT DEFAULT '',process_id INTEGER DEFAULT 0,process_name TEXT DEFAULT '',
                  ffmpeg_pid INTEGER DEFAULT 0,ffprobe_pid INTEGER DEFAULT 0,error_code TEXT DEFAULT '',
                  error_message TEXT DEFAULT '',finished_at TEXT DEFAULT '',updated_at TEXT DEFAULT ''
                );
                CREATE TABLE batch_tasks(
                  id INTEGER PRIMARY KEY,total_count INTEGER,status TEXT,completed_count INTEGER DEFAULT 0,
                  failed_count INTEGER DEFAULT 0,continue_on_child_error INTEGER DEFAULT 0,
                  updated_at TEXT DEFAULT '',finished_at TEXT DEFAULT ''
                );
                CREATE TABLE batch_task_items(
                  id INTEGER PRIMARY KEY,batch_id INTEGER,task_id INTEGER,index_no INTEGER,status TEXT,
                  output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',updated_at TEXT DEFAULT ''
                );
                INSERT INTO batch_tasks(id,total_count,status) VALUES(1,2,'needs_action');
                INSERT INTO generation_tasks(id,task_name,status,progress) VALUES(1,'001','failed',20),(2,'002','queued',0);
                INSERT INTO batch_task_items(id,batch_id,task_id,index_no,status) VALUES(1,1,1,1,'failed'),(2,1,2,2,'queued');
                """
            )

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def test_skip_records_cancelled_and_releases_batch(self):
        enqueued = []
        with patch.object(commercial_api, "connect", self.connect), \
             patch.object(batch_service, "connect", self.connect), \
             patch.object(commercial_api, "terminate_process", return_value=False), \
             patch.object(commercial_api, "append_task_log", lambda *_args: None), \
             patch.object(commercial_api.BATCH_QUEUE, "enqueue", side_effect=lambda batch_id, _cb: enqueued.append(batch_id)):
            result = commercial_api.skip_generation_task_and_continue(1)
        with self.connect() as conn:
            first = conn.execute("SELECT status,error_code FROM generation_tasks WHERE id=1").fetchone()
            second = conn.execute("SELECT status FROM generation_tasks WHERE id=2").fetchone()
        self.assertEqual((first["status"], first["error_code"]), ("cancelled", "SKIPPED_BY_USER"))
        self.assertEqual(second["status"], "queued")
        self.assertEqual(enqueued, [1])
        self.assertTrue(result["continued"])


if __name__ == "__main__":
    unittest.main()
