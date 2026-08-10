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


class GenerationWorkerLifecycleV22Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp.name) / "worker.db"
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE generation_tasks(
                  id INTEGER PRIMARY KEY,status TEXT DEFAULT 'queued',queue_seq INTEGER NOT NULL,
                  progress INTEGER DEFAULT 0,current_progress INTEGER DEFAULT 0,
                  current_stage TEXT DEFAULT '',current_step TEXT DEFAULT '',current_shot TEXT DEFAULT '',
                  worker_id TEXT DEFAULT '',started_at TEXT DEFAULT '',last_heartbeat_at TEXT DEFAULT '',
                  updated_at TEXT DEFAULT '',log_path TEXT DEFAULT '',technical_log_id TEXT DEFAULT '',
                  diagnostic_log TEXT DEFAULT '',cancel_requested INTEGER DEFAULT 0,
                  process_id INTEGER DEFAULT 0,process_name TEXT DEFAULT '',ffmpeg_pid INTEGER DEFAULT 0,
                  ffprobe_pid INTEGER DEFAULT 0,error_code TEXT DEFAULT '',error_message TEXT DEFAULT '',
                  failed_stage TEXT DEFAULT '',finished_at TEXT DEFAULT ''
                );
                CREATE UNIQUE INDEX uq_one_active ON generation_tasks((1))
                  WHERE status IN ('preparing','rendering','verifying');
                CREATE TABLE batch_task_items(id INTEGER PRIMARY KEY,batch_id INTEGER,task_id INTEGER);
                INSERT INTO generation_tasks(id,status,queue_seq) VALUES
                  (1,'queued',2),(2,'queued',1),(3,'queued',3);
                """
            )

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def test_worker_survives_idle_and_wakes_for_new_task(self):
        dispatcher = task_runtime.SerialGenerationQueue()
        completed = threading.Event()

        def runner():
            with self.connect() as conn:
                conn.execute("UPDATE generation_tasks SET status='succeeded',progress=100 WHERE id=2")
            completed.set()

        with patch.object(task_runtime, "connect", self.connect), \
             patch.object(task_runtime, "append_task_log", lambda *_args, **_kwargs: None), \
             patch.object(task_runtime, "append_generation_worker_log", lambda *_args, **_kwargs: None):
            dispatcher.ensure_started()
            time.sleep(1.2)  # the regressed one-shot worker exited after one second
            self.assertTrue(dispatcher.snapshot()["worker_alive"])
            dispatcher.enqueue(2, runner, priority=1)
            self.assertTrue(completed.wait(3))
            time.sleep(1.2)
            self.assertTrue(dispatcher.snapshot()["worker_alive"])
            dispatcher.stop()
            self.assertFalse(dispatcher.snapshot()["worker_alive"])

    def test_claim_is_atomic_and_uses_queue_sequence_not_id(self):
        with patch.object(task_runtime, "connect", self.connect), \
             patch.object(task_runtime, "append_task_log", lambda *_args, **_kwargs: None):
            claimed, blocker = task_runtime.try_claim_generation_task(1, "worker-1")
            self.assertFalse(claimed)
            self.assertEqual(blocker, 2)
            claimed, blocker = task_runtime.try_claim_generation_task(2, "worker-2")
            self.assertTrue(claimed)
            self.assertIsNone(blocker)

    def test_batch_worker_is_long_lived_while_idle(self):
        worker = batch_service.SerialBatchQueue()
        worker.ensure_started()
        time.sleep(1.2)
        self.assertTrue(worker.snapshot()["worker_alive"])
        worker.stop()
        self.assertFalse(worker.snapshot()["worker_alive"])

    def test_stopped_generation_worker_can_be_restarted_once(self):
        dispatcher = task_runtime.SerialGenerationQueue()
        dispatcher.ensure_started()
        first_id = dispatcher.snapshot()["worker_id"]
        dispatcher.stop()
        self.assertTrue(dispatcher.ensure_started())
        second = dispatcher.snapshot()
        self.assertTrue(second["worker_alive"])
        self.assertNotEqual(first_id, second["worker_id"])
        self.assertFalse(dispatcher.ensure_started())
        dispatcher.stop()

    def test_startup_recovery_registers_queued_tasks_by_queue_sequence(self):
        recovered = []
        with patch.object(commercial_api, "connect", self.connect), \
             patch.object(commercial_api, "start_generate", side_effect=lambda task_id: recovered.append(task_id)):
            result = commercial_api.recover_standalone_generation_queue()
        self.assertEqual(result, [2, 1, 3])
        self.assertEqual(recovered, [2, 1, 3])


if __name__ == "__main__":
    unittest.main()
