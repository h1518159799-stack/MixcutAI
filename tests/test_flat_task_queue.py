from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import commercial_api
import task_runtime


class FlatTaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp.name) / "flat-task.db"
        self.output = Path(self.temp.name) / "finished.mp4"
        self.output.write_bytes(b"video")
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE edit_templates(id INTEGER PRIMARY KEY,name TEXT,source_template_id INTEGER);
                CREATE TABLE template_shots(id INTEGER PRIMARY KEY,template_id INTEGER,voice_duration REAL DEFAULT 0);
                CREATE TABLE generation_tasks(
                    id INTEGER PRIMARY KEY,template_id INTEGER,task_name TEXT,status TEXT,progress INTEGER DEFAULT 0,
                    current_progress INTEGER DEFAULT 0,current_shot TEXT DEFAULT '',total_shots INTEGER DEFAULT 0,
                    output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',created_at TEXT,updated_at TEXT,
                    started_at TEXT DEFAULT '',finished_at TEXT DEFAULT '',current_stage TEXT DEFAULT '',
                    failed_stage TEXT DEFAULT '',current_step TEXT DEFAULT '',current_item TEXT DEFAULT '',
                    error_code TEXT DEFAULT '',technical_log_id TEXT DEFAULT '',log_path TEXT DEFAULT '',
                    diagnostic_log TEXT DEFAULT '',last_heartbeat_at TEXT DEFAULT '',worker_id TEXT DEFAULT '',
                    process_id INTEGER DEFAULT 0,process_name TEXT DEFAULT '',ffmpeg_pid INTEGER DEFAULT 0,
                    ffprobe_pid INTEGER DEFAULT 0,retry_of_task_id INTEGER,cancel_requested INTEGER DEFAULT 0
                );
                CREATE UNIQUE INDEX uq_one_active ON generation_tasks((1))
                  WHERE status IN ('preparing','rendering','verifying');
                CREATE TABLE batch_tasks(
                    id INTEGER PRIMARY KEY,name TEXT,template_id INTEGER,total_count INTEGER DEFAULT 0,
                    completed_count INTEGER DEFAULT 0,failed_count INTEGER DEFAULT 0,status TEXT,
                    continue_on_child_error INTEGER DEFAULT 0,created_at TEXT,updated_at TEXT,finished_at TEXT DEFAULT ''
                );
                CREATE TABLE batch_task_items(
                    id INTEGER PRIMARY KEY,batch_id INTEGER,task_id INTEGER,index_no INTEGER,status TEXT DEFAULT 'pending',
                    output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',updated_at TEXT DEFAULT '',
                    FOREIGN KEY(batch_id) REFERENCES batch_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES generation_tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE generation_shot_materials(id INTEGER PRIMARY KEY,task_id INTEGER);
                INSERT INTO edit_templates(id,name,source_template_id) VALUES(1,'洗护项目',NULL);
                """
            )
        self.api_connect = patch.object(commercial_api, "connect", self.connect)
        self.runtime_connect = patch.object(task_runtime, "connect", self.connect)
        self.runtime_snapshot = patch.object(
            commercial_api,
            "generation_worker_snapshot",
            return_value={"alive_task_ids": [], "alive_count": 0, "dispatcher_running": False, "pending_count": 0},
        )
        self.api_connect.start()
        self.runtime_connect.start()
        self.runtime_snapshot.start()

    def tearDown(self):
        self.runtime_snapshot.stop()
        self.runtime_connect.stop()
        self.api_connect.stop()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def stamp(minutes=0):
        return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

    def insert_batch(self, batch_id=1, status="needs_action", total=3):
        stamp = self.stamp()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO batch_tasks(id,name,template_id,total_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, "洗护批量生成3条", 1, total, status, stamp, stamp),
            )

    def insert_task(self, task_id, status, index_no, error="", progress=0, output_path=""):
        stamp = self.stamp()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO generation_tasks(
                       id,template_id,task_name,status,progress,total_shots,output_path,error_message,
                       created_at,updated_at,current_step,current_stage,last_heartbeat_at)
                   VALUES(?,?,?,?,?,3,?,?,?,?,'等待生成','',?)""",
                (task_id, 1, f"洗护 · {index_no:03d}/003", status, progress, output_path, error, stamp, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO batch_task_items(id,batch_id,task_id,index_no,status,updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, 1, task_id, index_no, status, stamp),
            )

    def test_flat_list_has_one_row_per_video_and_exposes_blocker(self):
        self.insert_batch()
        self.insert_task(1, "failed", 1, "素材文件不存在", 22)
        self.insert_task(2, "queued", 2)
        self.insert_task(3, "queued", 3)

        listing = commercial_api.task_center_flat_list("current")
        self.assertEqual([item["id"] for item in listing["items"]], [1, 2, 3])
        self.assertTrue(all("batch_name" in item for item in listing["items"]))
        self.assertEqual(listing["queue"]["blocking_task"]["id"], 1)
        self.assertEqual(listing["queue"]["next_task"]["id"], 2)
        self.assertEqual(listing["queue"]["queued_count"], 2)
        self.assertIn("素材文件不存在", listing["queue"]["blocking_reason"])
        queued = [item for item in listing["items"] if item["status"] == "queued"]
        self.assertEqual([item["queue_position"] for item in queued], [1, 2])
        self.assertEqual(listing["summary"]["failed_count"], 1)
        self.assertEqual(listing["summary"]["queued_count"], 2)
        # The list endpoint must stay lightweight so newly-created queued jobs
        # can render immediately. Full logs are loaded only after selecting a
        # task through the dedicated detail endpoint.
        self.assertNotIn("diagnostic_log", listing["items"][0])

    def test_source_project_filter_includes_cloned_task_template(self):
        stamp = self.stamp()
        with self.connect() as conn:
            conn.execute("INSERT INTO edit_templates(id,name,source_template_id) VALUES(2,'任务副本',1)")
            conn.execute(
                """INSERT INTO generation_tasks(
                       id,template_id,task_name,status,progress,total_shots,created_at,updated_at)
                   VALUES(9,2,'洗护项目 · 001/001','queued',0,1,?,?)""",
                (stamp, stamp),
            )
        listing = commercial_api.task_center_flat_list("current", project_id=1)
        self.assertEqual([item["id"] for item in listing["items"]], [9])

    def test_zero_child_active_batch_is_reported_not_deleted(self):
        self.insert_batch(status="queued", total=3)
        audit = commercial_api.scan_invalid_generation_tasks(apply=False)
        finding = next(item for item in audit["items"] if item.get("batch_id") == 1)
        self.assertEqual(finding["type"], "batch_item_missing")
        self.assertIn("当前只有0条", finding["message"])
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM batch_tasks WHERE id=1").fetchone())

    def test_delete_record_keeps_finished_video_by_default(self):
        self.insert_batch(status="succeeded", total=1)
        self.insert_task(1, "succeeded", 1, progress=100, output_path=str(self.output))
        result = commercial_api.delete_generation_task_records([1], delete_outputs=False)
        self.assertEqual(result["deleted"], [1])
        self.assertTrue(self.output.is_file())
        with self.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM generation_tasks WHERE id=1").fetchone())

    def test_running_task_cannot_be_deleted(self):
        self.insert_batch(status="running", total=1)
        self.insert_task(1, "rendering", 1, progress=42)
        result = commercial_api.delete_generation_task_records([1])
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["skipped"][0]["reason"], "生成中的任务必须先停止")

    def test_stale_worker_is_marked_needs_action_without_deleting_output(self):
        self.insert_batch(status="running", total=1)
        self.insert_task(1, "rendering", 1, progress=42, output_path=str(self.output))
        stale = self.stamp(minutes=10)
        with self.connect() as conn:
            conn.execute("UPDATE generation_tasks SET last_heartbeat_at=? WHERE id=1", (stale,))
        with patch.object(commercial_api, "task_health_snapshot", return_value={
            "heartbeat_age_seconds": 600, "worker_alive": False, "process_alive": False,
            "ffmpeg_alive": False, "ffprobe_alive": False,
        }):
            result = commercial_api.scan_invalid_generation_tasks(apply=True)
        self.assertEqual(result["fixed"], 1)
        with self.connect() as conn:
            task = conn.execute("SELECT status,error_code FROM generation_tasks WHERE id=1").fetchone()
        self.assertEqual((task["status"], task["error_code"]), ("stalled", "WORKER_STALLED"))
        self.assertTrue(self.output.is_file())

    def test_repair_queue_starts_only_first_available_task(self):
        self.insert_batch(status="queued")
        self.insert_task(1, "queued", 1)
        self.insert_task(2, "queued", 2)
        self.insert_task(3, "queued", 3)
        enqueued = []
        with patch.object(commercial_api, "scan_invalid_generation_tasks", return_value={"count": 0, "fixed": 0}), \
             patch.object(commercial_api.BATCH_QUEUE, "enqueue", side_effect=lambda batch_id, callback: enqueued.append((batch_id, callback))):
            result = commercial_api.repair_generation_queue()
        self.assertTrue(result["started"])
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0][0], 1)
        self.assertEqual(result["task_id"], 1)


if __name__ == "__main__":
    unittest.main()
