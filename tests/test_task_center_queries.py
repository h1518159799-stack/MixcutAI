from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import commercial_api
import task_runtime


class TaskCenterQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp.name) / "task-center.db"
        self.output = Path(self.temp.name) / "kept.mp4"
        self.output.write_bytes(b"test")
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE edit_templates(id INTEGER PRIMARY KEY,name TEXT,source_template_id INTEGER);
                CREATE TABLE template_shots(id INTEGER PRIMARY KEY,template_id INTEGER,voice_duration REAL DEFAULT 0);
                CREATE TABLE generation_tasks(
                    id INTEGER PRIMARY KEY,template_id INTEGER,task_name TEXT,status TEXT,progress INTEGER DEFAULT 0,
                    current_shot TEXT DEFAULT '',total_shots INTEGER DEFAULT 0,output_path TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',created_at TEXT,updated_at TEXT,finished_at TEXT DEFAULT '',
                    current_stage TEXT DEFAULT '',failed_stage TEXT DEFAULT '',current_step TEXT DEFAULT '',current_item TEXT DEFAULT '',
                    error_code TEXT DEFAULT '',technical_log_id TEXT DEFAULT '',log_path TEXT DEFAULT '',diagnostic_log TEXT DEFAULT '',
                    last_heartbeat_at TEXT DEFAULT '',worker_id TEXT DEFAULT '',process_id INTEGER DEFAULT 0,
                    process_name TEXT DEFAULT '',ffmpeg_pid INTEGER DEFAULT 0,ffprobe_pid INTEGER DEFAULT 0,
                    retry_of_task_id INTEGER
                );
                CREATE TABLE batch_tasks(
                    id INTEGER PRIMARY KEY,name TEXT,template_id INTEGER,total_count INTEGER DEFAULT 0,
                    completed_count INTEGER DEFAULT 0,failed_count INTEGER DEFAULT 0,status TEXT,
                    created_at TEXT,updated_at TEXT,finished_at TEXT DEFAULT ''
                );
                CREATE TABLE batch_task_items(
                    id INTEGER PRIMARY KEY,batch_id INTEGER,task_id INTEGER,index_no INTEGER,status TEXT DEFAULT 'pending',
                    FOREIGN KEY(batch_id) REFERENCES batch_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES generation_tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE generation_shot_materials(id INTEGER PRIMARY KEY,task_id INTEGER);
                INSERT INTO edit_templates(id,name,source_template_id) VALUES(1,'测试项目',NULL);
                """
            )
        self.connect_patch = patch.object(commercial_api, "connect", self.connect)
        self.connect_patch.start()
        self.runtime_connect_patch = patch.object(task_runtime, "connect", self.connect)
        self.runtime_connect_patch.start()

    def tearDown(self):
        self.connect_patch.stop()
        self.runtime_connect_patch.stop()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def stamp(days=0):
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def insert_batch(self, batch_id, status="succeeded", days=0, total=3, completed=3, failed=0):
        stamp = self.stamp(days)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO batch_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, f"测试批次{batch_id}", 1, total, completed, failed, status, stamp, stamp, stamp),
            )

    def test_current_sort_recent_limit_and_history_pagination(self):
        for index in range(1, 13):
            self.insert_batch(index, days=1)
        for index in range(13, 26):
            self.insert_batch(index, days=40)
        self.insert_batch(30, "queued", completed=0)
        self.insert_batch(31, "rendering", completed=0)
        self.insert_batch(32, "failed", completed=0, failed=1)
        self.insert_batch(33, "needs_action", completed=0)
        stamp = self.stamp()
        with self.connect() as conn:
            for task_id, batch_id, task_status in (
                (30, 30, "queued"), (31, 31, "rendering"),
                (32, 32, "failed"), (33, 33, "stalled"),
            ):
                conn.execute(
                    "INSERT INTO generation_tasks(id,template_id,task_name,status,progress,created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                    (task_id, 1, f"task-{task_id}", task_status, stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO batch_task_items(id,batch_id,task_id,index_no,status) VALUES(?,?,?,?,?)",
                    (task_id, batch_id, task_id, 1, task_status),
                )

        current = commercial_api.task_center_list("current")
        self.assertEqual([item["status"] for item in current["items"][:2]], ["needs_action", "failed"])
        self.assertEqual(current["summary"]["running_count"], 1)
        self.assertEqual(current["summary"]["queued_count"], 1)
        self.assertEqual(current["summary"]["failed_count"], 1)
        self.assertEqual(current["summary"]["needs_action_count"], 1)

        recent = commercial_api.task_center_list("recent")
        self.assertEqual(recent["total"], 12)
        self.assertEqual(len(recent["items"]), commercial_api.RECENT_COMPLETED_LIMIT)

        first_page = commercial_api.task_center_list("history", page=1, page_size=20)
        second_page = commercial_api.task_center_list("history", page=2, page_size=20)
        self.assertEqual(first_page["total"], 29)
        self.assertEqual(len(first_page["items"]), 20)
        self.assertEqual(len(second_page["items"]), 9)

    def test_children_are_loaded_separately_and_delete_keeps_output_by_default(self):
        self.insert_batch(1, "succeeded", total=2, completed=2)
        stamp = self.stamp()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO generation_tasks(id,template_id,task_name,status,progress,current_shot,total_shots,output_path,error_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (1,1,"001/002","succeeded",100,"",2,str(self.output),"",stamp,stamp),
                    (2,1,"002/002","succeeded",100,"",2,"","",stamp,stamp),
                ],
            )
            conn.executemany(
                "INSERT INTO batch_task_items(id,batch_id,task_id,index_no,status) VALUES(?,?,?,?,?)",
                [(1,1,1,1,"succeeded"),(2,1,2,2,"succeeded")],
            )
        listing = commercial_api.task_center_list("recent")
        self.assertNotIn("task_name", listing["items"][0])
        children = commercial_api.task_center_items("batch", 1)
        self.assertEqual([item["index_no"] for item in children], [1,2])

        result = commercial_api.delete_task_center_records([{"record_type":"task","record_id":1}], False)
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(self.output.is_file())
        with self.connect() as conn:
            batch = conn.execute("SELECT total_count,completed_count FROM batch_tasks WHERE id=1").fetchone()
        self.assertEqual((batch["total_count"], batch["completed_count"]), (1,1))

    def test_failed_batch_progress_uses_child_progress_not_terminal_count(self):
        self.insert_batch(8, "needs_action", total=1, completed=0, failed=1)
        stamp = self.stamp()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO generation_tasks(id,template_id,task_name,status,progress,created_at,updated_at) VALUES(8,1,'001/001','failed',9,?,?)",
                (stamp, stamp),
            )
            conn.execute("INSERT INTO batch_task_items(id,batch_id,task_id,index_no,status) VALUES(8,8,8,1,'failed')")
        listing = commercial_api.task_center_list("current")
        item = next(value for value in listing["items"] if value["record_id"] == 8)
        self.assertEqual(int(item["progress_percent"]), 9)

    def test_log_details_returns_explicit_missing_file_state(self):
        stamp = self.stamp()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO generation_tasks(id,template_id,task_name,status,progress,error_message,created_at,updated_at,
                   failed_stage,error_code,technical_log_id,diagnostic_log) VALUES(9,1,'日志测试','failed',9,'配音失败',?,?,
                   'tts','TTS_NO_AUDIO','MIX-TEST-9','[time] TTS failed')""", (stamp, stamp)
            )
        details = commercial_api.task_log_details(9)
        self.assertFalse(details["log_exists"])
        self.assertEqual(details["error_code"], "TTS_NO_AUDIO")
        self.assertIn("TTS failed", details["recent_logs"])


if __name__ == "__main__":
    unittest.main()
