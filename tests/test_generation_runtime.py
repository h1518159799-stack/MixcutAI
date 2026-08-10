from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import commercial_api
import db
import task_runtime


TASK_COLUMNS = """
id INTEGER PRIMARY KEY,template_id INTEGER,task_name TEXT,status TEXT,progress INTEGER DEFAULT 0,
current_shot TEXT DEFAULT '',total_shots INTEGER DEFAULT 0,output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',
created_at TEXT,updated_at TEXT,started_at TEXT DEFAULT '',last_heartbeat_at TEXT DEFAULT '',current_stage TEXT DEFAULT '',
current_step TEXT DEFAULT '',current_item TEXT DEFAULT '',current_progress INTEGER DEFAULT 0,worker_id TEXT DEFAULT '',
process_id INTEGER DEFAULT 0,last_log_at TEXT DEFAULT '',cancel_requested INTEGER DEFAULT 0,retry_count INTEGER DEFAULT 0,
diagnostic_log TEXT DEFAULT '',error_code TEXT DEFAULT '',failed_stage TEXT DEFAULT '',technical_log_id TEXT DEFAULT '',
log_path TEXT DEFAULT '',finished_at TEXT DEFAULT '',retry_of_task_id INTEGER,
process_name TEXT DEFAULT '',ffmpeg_pid INTEGER DEFAULT 0,ffprobe_pid INTEGER DEFAULT 0,attempt_no INTEGER DEFAULT 1
"""


class GenerationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.temp.name) / "runtime.db"
        with self.connect() as conn:
            conn.execute(f"CREATE TABLE generation_tasks({TASK_COLUMNS})")
            conn.execute(
                "INSERT INTO generation_tasks(id,template_id,task_name,status,created_at,updated_at) VALUES(1,1,'test','preparing',datetime('now'),datetime('now'))"
            )
        self.patch = patch.object(task_runtime, "connect", self.connect)
        self.patch.start()
        self.log_patch = patch.object(task_runtime, "TASK_LOG_DIR", Path(self.temp.name) / "logs")
        self.log_patch.start()

    def tearDown(self):
        self.patch.stop()
        self.log_patch.stop()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.path, factory=db.ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def test_heartbeat_records_stage_progress_and_log(self):
        task_runtime.heartbeat(1, stage="tts", step="生成配音 1/3", item="镜头1", progress=12, log="开始配音")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM generation_tasks WHERE id=1").fetchone()
        self.assertEqual(row["current_stage"], "tts")
        self.assertEqual(row["current_step"], "生成配音 1/3")
        self.assertEqual(row["current_progress"], 12)
        self.assertIn("开始配音", row["diagnostic_log"])
        self.assertTrue((Path(self.temp.name) / "logs" / "1.log").is_file())

    def test_failure_records_code_stage_and_durable_log(self):
        task_runtime._local.stage = "tts"
        code, message = task_runtime.fail_task(1, RuntimeError("No audio was received"))
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM generation_tasks WHERE id=1").fetchone()
        self.assertEqual(code, "TTS_NO_AUDIO")
        self.assertEqual(row["failed_stage"], "tts")
        self.assertEqual(row["error_code"], "TTS_NO_AUDIO")
        self.assertIn("配音服务", message)
        self.assertTrue(Path(row["log_path"]).is_file())

    def test_managed_process_timeout_terminates_process(self):
        with self.assertRaises(task_runtime.ManagedProcessTimeout):
            with task_runtime.task_context(1):
                task_runtime.run_managed_command(
                    [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2,
                    failure_message="测试进程超时",
                )
        with self.connect() as conn:
            row = conn.execute("SELECT process_id,diagnostic_log FROM generation_tasks WHERE id=1").fetchone()
        self.assertEqual(row["process_id"], 0)
        self.assertIn("进程已终止", row["diagnostic_log"])


class SingleGenerationEntryTests(unittest.TestCase):
    def test_reuses_pending_single_task_and_starts_it(self):
        summary = {"template_id": 7, "template_name": "测试模板", "shot_count": 3}
        pending = {"id": 23, "status": "pending"}
        with patch.object(commercial_api, "single_generation_preflight", return_value=summary), \
             patch.object(commercial_api, "row", return_value=pending), \
             patch.object(commercial_api, "start_generate", return_value={"task_id": 23, "status": "preparing"}) as starter, \
             patch.object(commercial_api, "create_task") as creator:
            result = commercial_api.create_and_start_single_task({"template_id": 7})
        creator.assert_not_called()
        starter.assert_called_once_with(23)
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["batch_size"], 1)

    def test_active_single_task_is_idempotent(self):
        summary = {"template_id": 7, "template_name": "测试模板", "shot_count": 3}
        active = {"id": 24, "status": "rendering"}
        with patch.object(commercial_api, "single_generation_preflight", return_value=summary), \
             patch.object(commercial_api, "row", return_value=active), \
             patch.object(commercial_api, "worker_is_alive", return_value=True), \
             patch.object(commercial_api, "start_generate") as starter:
            result = commercial_api.create_and_start_single_task({"template_id": 7})
        starter.assert_not_called()
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["task_id"], 24)


if __name__ == "__main__":
    unittest.main()
